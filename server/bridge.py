""" Web UI bridge for the WeatherFlow PiConsole.

Exposes the console's live data to a modern browser front-end without touching
the existing data pipeline. The bridge:

  1. Runs a FastAPI + uvicorn server in a daemon thread, alongside the Kivy app.
  2. On the Kivy main thread, snapshots the six CurrentConditions data dicts
     (Obs, Astro, Met, Sager, Status, System) plus a little station metadata
     into a plain, JSON-safe dict a couple of times per second.
  3. Serves the front-end (web/) and streams each new snapshot to every
     connected browser over a WebSocket at /ws.

Nothing here writes back into the console; it is a read-only view of state
that already exists. If FastAPI/uvicorn are not installed, the bridge logs a
warning and the console runs exactly as before.

Copyright (C) 2018-2025 Peter Davis
Web UI bridge (C) 2026 Ricky Theil

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.
"""

import os
import json
import math
import time
import threading

from pathlib import Path

from kivy.logger import Logger
from kivy.clock  import Clock


# Directory that holds the front-end (index.html, styles.css, app.js)
WEB_DIR = Path(__file__).resolve().parent.parent / 'web'

# Bundled Inter fonts, referenced by styles.css as /fonts/Inter-*.ttf
FONT_DIR = Path(__file__).resolve().parent.parent / 'fonts'

# TEMPORARY build stamp — bump every deploy to confirm the Pi is running the
# new code (see startup log, /api/health, and the web UI's browser console).
# Must match BUILD in web/app.js. Remove once we're done verifying deploys.
WEB_UI_BUILD = '110'

# Defaults; overridable via environment for advanced users / kiosk setups
DEFAULT_HOST = os.environ.get('WFPICONSOLE_WEB_HOST', '0.0.0.0')
DEFAULT_PORT = int(os.environ.get('WFPICONSOLE_WEB_PORT', '8000'))

# How often (seconds) the Kivy main thread refreshes the shared snapshot
SNAPSHOT_INTERVAL = 0.5


def _plain(value):
    """ Recursively coerce Kivy/observable containers into plain, JSON-safe
    Python objects. """
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    # Non-finite floats (NaN/Infinity) are not valid JSON: json.dumps emits the
    # bare tokens NaN/Infinity (which browsers' JSON.parse rejects) and Starlette's
    # JSONResponse raises outright. Coerce them to null so every consumer is safe.
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


class WebBridge:

    def __init__(self, app, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.app     = app
        self.host    = host
        self.port    = port
        self._lock   = threading.Lock()
        self._state  = {'version': 0}
        self._version = 0
        self._server = None
        self._thread = None
        self._update_info = {'available': False, 'current': '', 'latest': '', 'url': ''}
        self._forecast = []   # 5-day outlook, refreshed on its own daemon thread

    # ------------------------------------------------------------------ #
    # Snapshotting (runs on the Kivy main thread)                        #
    # ------------------------------------------------------------------ #
    def _station_meta(self):
        """ Read a little station/config metadata for the header. """
        meta = {}
        try:
            station = self.app.config['Station']
            meta = {
                'name':      station.get('Name', ''),
                'latitude':  station.get('Latitude', ''),
                'longitude': station.get('Longitude', ''),
                'elevation': station.get('Elevation', ''),
                'timezone':  station.get('Timezone', ''),
            }
        except Exception:
            pass
        try:
            meta['hardware']   = self.app.config['System'].get('Hardware', '')
            meta['connection'] = self.app.config['System'].get('Connection', '')
        except Exception:
            pass
        return meta

    def _snapshot(self, *args):
        """ Build a JSON-safe snapshot of current state and store it under the
        lock. Scheduled on the Kivy Clock, so all reads happen on the main
        thread where the console mutates these dicts. """
        cc = getattr(self.app, 'CurrentConditions', None)
        if cc is None:
            return
        try:
            update = dict(self._update_info)
            try:
                update['notify'] = self.app.config['Display'].get('UpdateNotification', '1') == '1'
                display = {'TimeFormat': self.app.config['Display'].get('TimeFormat', '12 hr'),
                           'DateFormat': self.app.config['Display'].get('DateFormat', 'Mon, 01 Jan 0000')}
            except Exception:
                update['notify'] = True
                display = {}
            snapshot = {
                'build':   WEB_UI_BUILD,
                'meta':    self._station_meta(),
                'obs':     _plain(dict(cc.Obs)),
                'astro':   _plain(dict(cc.Astro)),
                'met':     _plain(dict(cc.Met)),
                'sager':   _plain(dict(cc.Sager)),
                'status':  _plain(dict(cc.Status)),
                'system':  _plain(dict(cc.System)),
                'forecast': list(self._forecast),
                'update':  update,
                'display': display,
            }
        except Exception as error:
            Logger.warning(f'web_bridge: snapshot failed ({error})')
            return

        with self._lock:
            self._version += 1
            snapshot['version'] = self._version
            self._state = snapshot

    def get_state(self):
        with self._lock:
            return self._state

    # ------------------------------------------------------------------ #
    # Update check (own daemon thread; result surfaced in each snapshot) #
    # ------------------------------------------------------------------ #
    def _check_update(self):
        """ Compare the installed version against the latest GitHub release.
        Runs off the main thread; network calls never block the console. """
        try:
            import requests
            from packaging import version

            current = self.app.config['System']['Version']
            resp = requests.get(
                'https://api.github.com/repos/rtheil/WeatherFlow_PiConsole/releases/latest',
                headers={'Accept': 'application/vnd.github.v3+json'}, timeout=15)
            data = resp.json()
            latest = data.get('tag_name')
            url = data.get('html_url') or 'https://github.com/rtheil/WeatherFlow_PiConsole/releases'
            if latest:
                available = version.parse(current) < version.parse(latest)
                self._update_info = {'available': available, 'current': current,
                                     'latest': latest, 'url': url}
        except Exception as error:
            Logger.warning(f'web_bridge: update check failed ({error})')

    def _update_loop(self):
        while True:
            self._check_update()
            time.sleep(6 * 60 * 60)  # re-check every 6 hours

    # ------------------------------------------------------------------ #
    # 5-day outlook (own daemon thread; surfaced in each snapshot)        #
    # ------------------------------------------------------------------ #
    def _fetch_forecast(self):
        """ Fetch the 5-day daily outlook from the WeatherFlow BetterForecast
        API. The console itself only keeps the current day in cc.Met, so the
        bridge fetches the daily array directly from the same endpoint the
        console uses (read-only). Temps are converted to the configured unit to
        match the rest of the UI. Runs off the main thread. """
        try:
            import requests
            cfg = self.app.config
            is_f = cfg['Units'].get('Temp') == 'f'
            unit = '°F' if is_f else '°C'
            resp = requests.get(
                'https://swd.weatherflow.com/swd/rest/better_forecast',
                params={'token': cfg['Keys']['WeatherFlow'],
                        'station_id': cfg['Station']['StationID'],
                        # Ask WeatherFlow for the display unit so it does the
                        # conversion/rounding — matches the Tempest app instead
                        # of drifting ~1 degree from a local C->F + round.
                        'units_temp': 'f' if is_f else 'c'}, timeout=15)
            daily = ((resp.json().get('forecast') or {}).get('daily')) or []

            def temp(v):
                if v is None:
                    return ['--', unit]
                return ['{:.0f}'.format(v), unit]

            out = []
            for i, day in enumerate(daily[:5]):
                ts = day.get('day_start_local')
                dow = 'Today' if i == 0 else (time.strftime('%a', time.localtime(ts)) if ts else '')
                pp = day.get('precip_probability')
                out.append({
                    'day':    dow,
                    'icon':   day.get('icon', ''),
                    'high':   temp(day.get('air_temp_high')),
                    'low':    temp(day.get('air_temp_low')),
                    'precip': ['{:.0f}'.format(pp) if pp is not None else '0', '%'],
                })
            self._forecast = out
        except Exception as error:
            Logger.warning(f'web_bridge: forecast fetch failed ({error})')

    def _forecast_loop(self):
        while True:
            self._fetch_forecast()
            time.sleep(30 * 60)  # refresh every 30 minutes

    # ------------------------------------------------------------------ #
    # Settings (read wfpiconsole.ini; writes go through on_config_change) #
    # ------------------------------------------------------------------ #
    FEELS_LEVELS = [('ExtremelyCold', 'Extremely Cold'), ('FreezingCold', 'Freezing Cold'),
                    ('VeryCold', 'Very Cold'), ('Cold', 'Cold'), ('Mild', 'Mild'),
                    ('Warm', 'Warm'), ('Hot', 'Hot'), ('VeryHot', 'Very Hot')]

    def _config_schema(self):
        """ The settings the web UI edits (Phase 1: Units + Feels Like), sourced
        from the live console config. Same shape the dev-preview server serves. """
        cfg = self.app.config
        U, FL, D, S = cfg['Units'], cfg['FeelsLike'], cfg['Display'], cfg['System']
        tunit = '°F' if U.get('Temp') == 'f' else '°C'
        return {'sections': [
            {'name': 'Units', 'title': 'Units', 'fields': [
                {'section': 'Units', 'key': 'Temp', 'title': 'Temperature', 'type': 'options',
                 'options': ['c', 'f'], 'labels': {'c': '°C', 'f': '°F'}, 'value': U.get('Temp')},
                {'section': 'Units', 'key': 'Pressure', 'title': 'Pressure', 'type': 'options',
                 'options': ['inhg', 'mmhg', 'hpa', 'mb'],
                 'labels': {'inhg': 'inHg', 'mmhg': 'mmHg', 'hpa': 'hPa', 'mb': 'mb'}, 'value': U.get('Pressure')},
                {'section': 'Units', 'key': 'Wind', 'title': 'Wind speed', 'type': 'options',
                 'options': ['mph', 'kph', 'kts', 'bft', 'mps', 'lfm'],
                 'labels': {'kph': 'km/h', 'mps': 'm/s', 'bft': 'Beaufort', 'lfm': 'ft/min'}, 'value': U.get('Wind')},
                {'section': 'Units', 'key': 'Direction', 'title': 'Wind direction', 'type': 'options',
                 'options': ['degrees', 'cardinal'], 'labels': {'degrees': 'Degrees', 'cardinal': 'Compass'}, 'value': U.get('Direction')},
                {'section': 'Units', 'key': 'Precip', 'title': 'Rainfall', 'type': 'options',
                 'options': ['in', 'cm', 'mm'], 'value': U.get('Precip')},
                {'section': 'Units', 'key': 'Distance', 'title': 'Distance', 'type': 'options',
                 'options': ['km', 'mi'], 'labels': {'mi': 'miles'}, 'value': U.get('Distance')},
            ]},
            {'name': 'FeelsLike', 'title': 'Feels Like', 'desc': f'Maximum {tunit} for each level',
             'fields': [
                {'section': 'FeelsLike', 'key': k, 'title': t, 'type': 'stepper',
                 'value': FL.get(k, ''), 'unit': tunit}
                for k, t in self.FEELS_LEVELS
             ]},
            {'name': 'Display', 'title': 'Display', 'fields': [
                {'section': 'Display', 'key': 'TimeFormat', 'title': 'Time format', 'type': 'options',
                 'options': ['24 hr', '12 hr'], 'value': D.get('TimeFormat')},
                {'section': 'Display', 'key': 'DateFormat', 'title': 'Date format', 'type': 'options',
                 'options': ['Mon, 01 Jan 0000', 'Mon, Jan 01 0000', 'Monday, 01 Jan 0000', 'Monday, Jan 01 0000'],
                 'value': D.get('DateFormat')},
                {'section': 'Display', 'key': 'UpdateNotification', 'title': 'Update notifications', 'type': 'toggle',
                 'value': D.get('UpdateNotification')},
            ]},
            {'name': 'System', 'title': 'System', 'fields': [
                {'section': 'System', 'key': 'Connection', 'title': 'Connection', 'type': 'options',
                 'options': ['Websocket', 'UDP'], 'value': S.get('Connection')},
                {'section': 'System', 'key': 'rest_api', 'title': 'REST API', 'type': 'toggle', 'value': S.get('rest_api')},
                {'section': 'System', 'key': 'nc_rain', 'title': 'NC rain accumulation', 'type': 'toggle', 'value': S.get('nc_rain')},
                {'section': 'System', 'key': 'stats_endpoint', 'title': 'Statistics API endpoint', 'type': 'toggle', 'value': S.get('stats_endpoint')},
                {'section': 'System', 'key': 'SagerInterval', 'title': 'Sager forecast interval', 'type': 'stepper',
                 'value': S.get('SagerInterval'), 'unit': 'hr'},
            ]},
        ]}

    def _apply_config(self, section, key, value):
        """ Runs on the Kivy main thread: persist the change and let the console
        react through its own on_config_change (which converts Feels-Like
        thresholds on unit change, reparses the forecast, etc.). """
        try:
            self.app.config.set(section, key, value)
            self.app.config.write()
            try:
                self.app.on_config_change(self.app.config, section, key, value)
            except Exception as error:
                Logger.warning(f'web_bridge: on_config_change ({section}/{key}) {error}')
            if section == 'Units':
                # Reformat existing observations immediately in the new units
                try:
                    self.app.obsParser.reset_display()
                except Exception:
                    pass
        except Exception as error:
            Logger.warning(f'web_bridge: config write failed ({error})')

    # ------------------------------------------------------------------ #
    # Server (runs on a daemon thread with its own asyncio loop)         #
    # ------------------------------------------------------------------ #
    def _build_app(self):
        import asyncio

        from fastapi                  import FastAPI, WebSocket, WebSocketDisconnect, Request
        from fastapi.responses        import JSONResponse
        from fastapi.staticfiles      import StaticFiles

        api = FastAPI(title='WeatherFlow PiConsole Web UI')

        # Never let the browser cache the UI assets, so a redeploy always takes
        # effect on the next reload (no stale app.js/styles.css).
        @api.middleware('http')
        async def no_store(request, call_next):
            response = await call_next(request)
            response.headers['Cache-Control'] = 'no-store'
            return response

        @api.get('/api/snapshot')
        def snapshot():
            return JSONResponse(self.get_state())

        @api.get('/api/health')
        def health():
            return {'status': 'ok', 'build': WEB_UI_BUILD, 'version': self.get_state().get('version', 0)}

        @api.get('/api/config')
        def get_config():
            return JSONResponse(self._config_schema())

        @api.post('/api/config')
        async def set_config(request: Request):
            data = await request.json()
            section, key = data.get('section'), data.get('key')
            value = str(data.get('value'))
            if section in ('Units', 'FeelsLike', 'Display', 'System') and key:
                # Apply on the Kivy main thread; return immediately.
                Clock.schedule_once(lambda dt: self._apply_config(section, key, value), 0)
                return {'ok': True}
            return JSONResponse({'ok': False, 'error': 'unknown key'}, status_code=400)

        @api.post('/api/system')
        async def system_action(request: Request):
            data = await request.json()
            action = data.get('action')
            handlers = {
                'exit':     lambda: self.app.stop(),
                'reboot':   lambda: self.app.reboot_system(),
                'shutdown': lambda: self.app.shutdown_system(),
            }
            if action in handlers:
                # Run on the Kivy main thread, after this response is sent.
                Clock.schedule_once(lambda dt: handlers[action](), 0.3)
                return {'ok': True}
            return JSONResponse({'ok': False, 'error': 'unknown action'}, status_code=400)

        @api.websocket('/ws')
        async def ws(socket: WebSocket):
            await socket.accept()
            last = -1
            try:
                while True:
                    state = self.get_state()
                    version = state.get('version', 0)
                    if version != last:
                        await socket.send_text(json.dumps(state))
                        last = version
                    await asyncio.sleep(SNAPSHOT_INTERVAL)
            except WebSocketDisconnect:
                pass
            except Exception as error:
                Logger.warning(f'web_bridge: websocket closed ({error})')

        # Serve the bundled Inter fonts referenced by styles.css.
        if FONT_DIR.is_dir():
            api.mount('/fonts', StaticFiles(directory=str(FONT_DIR)), name='fonts')

        # Serve the front-end. Mounting at '/' with html=True serves
        # index.html for '/' automatically. Registered last so the API,
        # WebSocket, and /fonts routes above take precedence.
        if WEB_DIR.is_dir():
            api.mount('/', StaticFiles(directory=str(WEB_DIR), html=True), name='web')
        else:
            Logger.warning(f'web_bridge: web directory not found at {WEB_DIR}')

        return api

    def _run(self):
        import uvicorn
        api = self._build_app()
        config = uvicorn.Config(api, host=self.host, port=self.port,
                                log_level='warning', loop='asyncio')
        self._server = uvicorn.Server(config)
        self._server.run()

    def start(self):
        # Prime an initial snapshot and schedule periodic refreshes on the
        # Kivy main thread.
        Clock.schedule_once(self._snapshot, 0)
        Clock.schedule_interval(self._snapshot, SNAPSHOT_INTERVAL)

        # Launch the web server on a daemon thread so it never blocks shutdown.
        self._thread = threading.Thread(target=self._run, name='wfpiconsole-web',
                                        daemon=True)
        self._thread.start()

        # Periodically check GitHub for a newer release (own daemon thread).
        threading.Thread(target=self._update_loop, name='wfpiconsole-web-update',
                         daemon=True).start()

        # Periodically refresh the 5-day outlook (own daemon thread).
        threading.Thread(target=self._forecast_loop, name='wfpiconsole-web-forecast',
                         daemon=True).start()

        Logger.info(f'web_bridge: ===== WEB UI BUILD {WEB_UI_BUILD} =====')
        Logger.info(f'web_bridge: serving UI on http://{self.host}:{self.port}')


def start_web_bridge(app, host=DEFAULT_HOST, port=DEFAULT_PORT):
    """ Entry point called from main.py. Never raises into the console: if the
    optional web dependencies are missing, it logs and returns None. """
    try:
        import fastapi   # noqa: F401
        import uvicorn   # noqa: F401
    except ImportError:
        Logger.warning('web_bridge: fastapi/uvicorn not installed - web UI '
                        'disabled. Install with: pip install fastapi uvicorn '
                        '(or pip install -r requirements-web.txt)')
        return None

    bridge = WebBridge(app, host=host, port=port)
    bridge.start()
    app.web_bridge = bridge
    return bridge
