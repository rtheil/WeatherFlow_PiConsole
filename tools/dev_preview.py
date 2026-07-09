""" Local design-preview server for the WeatherFlow PiConsole web UI.

DEV TOOL ONLY -- this is NOT part of the console runtime. It exists so the web
UI (web/) can be designed against real station data on a machine that cannot
run the full Kivy console (e.g. a Windows dev box). It pulls live observations
straight from the WeatherFlow REST API and serves them to the same front-end,
in the same snapshot shape the production bridge (server/bridge.py) produces.

It uses only the Python standard library plus `requests` -- no Kivy, no
FastAPI. Run it, then browse to http://localhost:8080.

Credentials are read (in order) from:
  1. Environment: WEATHERFLOW_TOKEN and WEATHERFLOW_STATION_ID
  2. tools/dev_credentials.json  (git-ignored): {"token": "...", "station_id": 12345}

Usage:
    python tools/dev_preview.py [--port 8080]
"""

import os
import sys
import json
import time
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT     = Path(__file__).resolve().parent.parent
WEB_DIR  = ROOT / 'web'
FONT_DIR = ROOT / 'fonts'
CRED_FILE = ROOT / 'tools' / 'dev_credentials.json'

REST = 'https://swd.weatherflow.com/swd/rest'
CACHE_TTL = 8  # seconds; be gentle with the API while iterating

# TEMPORARY build stamp — keep in sync with WEB_UI_BUILD in server/bridge.py
# and BUILD in web/app.js.
WEB_UI_BUILD = '112'

MIME = {
    '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8', '.json': 'application/json',
    '.ttf': 'font/ttf', '.png': 'image/png', '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
}


# --------------------------------------------------------------------------- #
# Credentials                                                                 #
# --------------------------------------------------------------------------- #
def load_credentials():
    token = os.environ.get('WEATHERFLOW_TOKEN')
    station = os.environ.get('WEATHERFLOW_STATION_ID')
    if token and station:
        return token, str(station)
    if CRED_FILE.is_file():
        data = json.loads(CRED_FILE.read_text())
        return data.get('token'), str(data.get('station_id'))
    return None, None


# --------------------------------------------------------------------------- #
# Settings store: mirrors the console's Units + FeelsLike config so the        #
# settings page can be exercised locally. Persisted to a git-ignored file.     #
# The production bridge reads/writes wfpiconsole.ini instead.                   #
# --------------------------------------------------------------------------- #
SETTINGS_FILE = ROOT / 'tools' / 'dev_settings.json'
DEFAULT_SETTINGS = {
    'Units': {'Temp': 'f', 'Pressure': 'inhg', 'Wind': 'mph', 'Direction': 'cardinal',
              'Precip': 'in', 'Distance': 'mi', 'Other': 'imperial'},
    # FeelsLike thresholds are stored in the current temperature unit (°F here)
    'FeelsLike': {'ExtremelyCold': '-4', 'FreezingCold': '14', 'VeryCold': '23', 'Cold': '32',
                  'Mild': '50', 'Warm': '68', 'Hot': '86', 'VeryHot': '104'},
    'Display': {'TimeFormat': '12 hr', 'DateFormat': 'Mon, 01 Jan 0000', 'UpdateNotification': '1'},
    'System': {'Connection': 'Websocket', 'rest_api': '1', 'nc_rain': '0',
               'stats_endpoint': '0', 'SagerInterval': '6'},
}


def load_settings():
    s = {k: dict(v) for k, v in DEFAULT_SETTINGS.items()}
    if SETTINGS_FILE.is_file():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            for section in s:
                s[section].update(saved.get(section, {}))
        except Exception:
            pass
    return s


_settings = load_settings()


def save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps(_settings, indent=2))
    except Exception as error:
        print(f'  ! settings save failed: {error}', file=sys.stderr)


def convert_feelslike(new_temp_unit):
    """ When the temperature unit flips, convert the stored FeelsLike thresholds
    to match (mirrors the console's on_config_change behaviour). """
    fl = _settings['FeelsLike']
    for key, val in fl.items():
        try:
            v = float(val)
        except ValueError:
            continue
        if new_temp_unit == 'c':
            fl[key] = str(round((v - 32) * 5 / 9))
        else:
            fl[key] = str(round(v * 9 / 5 + 32))


def config_schema():
    """ The settings surface the web UI edits (Phase 1: Units + FeelsLike).
    The production bridge returns the same shape, sourced from wfpiconsole.ini. """
    U, FL = _settings['Units'], _settings['FeelsLike']
    D, S = _settings['Display'], _settings['System']
    tunit = '°F' if U['Temp'] == 'f' else '°C'
    return {'sections': [
        {'name': 'Units', 'title': 'Units', 'fields': [
            {'section': 'Units', 'key': 'Temp', 'title': 'Temperature', 'type': 'options',
             'options': ['c', 'f'], 'labels': {'c': '°C', 'f': '°F'}, 'value': U['Temp']},
            {'section': 'Units', 'key': 'Pressure', 'title': 'Pressure', 'type': 'options',
             'options': ['inhg', 'mmhg', 'hpa', 'mb'],
             'labels': {'inhg': 'inHg', 'mmhg': 'mmHg', 'hpa': 'hPa', 'mb': 'mb'}, 'value': U['Pressure']},
            {'section': 'Units', 'key': 'Wind', 'title': 'Wind speed', 'type': 'options',
             'options': ['mph', 'kph', 'kts', 'bft', 'mps', 'lfm'],
             'labels': {'kph': 'km/h', 'mps': 'm/s', 'bft': 'Beaufort', 'lfm': 'ft/min'}, 'value': U['Wind']},
            {'section': 'Units', 'key': 'Direction', 'title': 'Wind direction', 'type': 'options',
             'options': ['degrees', 'cardinal'], 'labels': {'degrees': 'Degrees', 'cardinal': 'Compass'}, 'value': U['Direction']},
            {'section': 'Units', 'key': 'Precip', 'title': 'Rainfall', 'type': 'options',
             'options': ['in', 'cm', 'mm'], 'value': U['Precip']},
            {'section': 'Units', 'key': 'Distance', 'title': 'Distance', 'type': 'options',
             'options': ['km', 'mi'], 'labels': {'mi': 'miles'}, 'value': U['Distance']},
        ]},
        {'name': 'FeelsLike', 'title': 'Feels Like', 'desc': f'Maximum {tunit} for each level',
         'fields': [
            {'section': 'FeelsLike', 'key': k, 'title': t, 'type': 'stepper',
             'value': FL.get(k, ''), 'unit': tunit}
            for k, t in FEELS_LEVELS
         ]},
        {'name': 'Display', 'title': 'Display', 'fields': [
            {'section': 'Display', 'key': 'TimeFormat', 'title': 'Time format', 'type': 'options',
             'options': ['24 hr', '12 hr'], 'value': D['TimeFormat']},
            {'section': 'Display', 'key': 'DateFormat', 'title': 'Date format', 'type': 'options',
             'options': ['Mon, 01 Jan 0000', 'Mon, Jan 01 0000', 'Monday, 01 Jan 0000', 'Monday, Jan 01 0000'],
             'value': D['DateFormat']},
            {'section': 'Display', 'key': 'UpdateNotification', 'title': 'Update notifications', 'type': 'toggle',
             'value': D['UpdateNotification']},
        ]},
        {'name': 'System', 'title': 'System', 'fields': [
            {'section': 'System', 'key': 'Connection', 'title': 'Connection', 'type': 'options',
             'options': ['Websocket', 'UDP'], 'value': S['Connection']},
            {'section': 'System', 'key': 'rest_api', 'title': 'REST API', 'type': 'toggle', 'value': S['rest_api']},
            {'section': 'System', 'key': 'nc_rain', 'title': 'NC rain accumulation', 'type': 'toggle', 'value': S['nc_rain']},
            {'section': 'System', 'key': 'stats_endpoint', 'title': 'Statistics API endpoint', 'type': 'toggle', 'value': S['stats_endpoint']},
            {'section': 'System', 'key': 'SagerInterval', 'title': 'Sager forecast interval', 'type': 'stepper',
             'value': S['SagerInterval'], 'unit': 'hr'},
        ]},
    ]}


# --------------------------------------------------------------------------- #
# Unit conversion (REST returns SI; convert to the configured display units)   #
# --------------------------------------------------------------------------- #
CARDINALS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
             'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']


def cardinal(deg):
    return CARDINALS[int((deg % 360) / 22.5 + 0.5) % 16]


def _dash(unit):
    return ['--', unit]


def _f(v, fmt, unit):
    """ Format a plain numeric value into [value, unit] (no unit conversion). """
    if v is None:
        return ['-', unit]
    return [fmt.format(v), unit]


def cv_temp(v_c, dp=1):
    unit = '°F' if _settings['Units']['Temp'] == 'f' else '°C'
    if v_c is None:
        return _dash(unit)
    v = v_c * 9 / 5 + 32 if _settings['Units']['Temp'] == 'f' else v_c
    return ['{:.{}f}'.format(v, dp), unit]


def fmt_temp(v, dp=0):
    """ Format a temperature that is ALREADY in the display unit (e.g. the
    BetterForecast API returns it directly when units_temp is requested), so
    WeatherFlow does the conversion/rounding — matching the Tempest app. """
    unit = '°F' if _settings['Units']['Temp'] == 'f' else '°C'
    if v is None:
        return _dash(unit)
    return ['{:.{}f}'.format(v, dp), unit]


def _beaufort(v_mps):
    for i, limit in enumerate([0.5, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]):
        if v_mps < limit:
            return i
    return 12


def cv_wind(v_mps):
    U = _settings['Units']['Wind']
    if U == 'bft':
        return _dash('bft') if v_mps is None else [str(_beaufort(v_mps)), 'bft']
    factor, unit = {'mph': (2.2369362920544, 'mph'), 'kph': (3.6, 'km/h'), 'kts': (1.9438445, 'kts'),
                    'mps': (1.0, 'm/s'), 'lfm': (196.8503937, 'ft/min')}.get(U, (2.2369362920544, 'mph'))
    if v_mps is None:
        return _dash(unit)
    v = v_mps * factor
    return ['{:.1f}'.format(v) if v < 10 else '{:.0f}'.format(v), unit]


def cv_pres(v_mb):
    factor, unit, dp = {'inhg': (0.0295301, ' inHg', 3), 'mmhg': (0.750063, ' mmHg', 2),
                        'hpa': (1.0, ' hPa', 1), 'mb': (1.0, ' mb', 1)}.get(
                            _settings['Units']['Pressure'], (0.0295301, ' inHg', 3))
    if v_mb is None:
        return _dash(unit)
    return ['{:.{}f}'.format(v_mb * factor, dp), unit]


def cv_rain(v_mm, rate=False):
    factor, unit, runit, dp = {'in': (0.0393701, '"', ' in/hr', 2), 'cm': (0.1, ' cm', ' cm/hr', 2),
                               'mm': (1.0, ' mm', ' mm/hr', 1)}.get(
                                   _settings['Units']['Precip'], (0.0393701, '"', ' in/hr', 2))
    u = runit if rate else unit
    if v_mm is None:
        return _dash(u)
    return ['{:.{}f}'.format(v_mm * factor, dp), u]


def cv_dist(v_km):
    if _settings['Units']['Distance'] == 'mi':
        return _dash('miles') if v_km is None else ['{:.0f}'.format(v_km * 0.62137), 'miles']
    return _dash('km') if v_km is None else ['{:.0f}'.format(v_km), 'km']


def cv_dir(deg):
    if deg is None:
        return ['-', '']
    if _settings['Units']['Direction'] == 'cardinal':
        return [cardinal(deg), '']
    return ['{:.0f}'.format(deg), '°']


FEELS_LEVELS = [('ExtremelyCold', 'Extremely cold'), ('FreezingCold', 'Freezing cold'),
                ('VeryCold', 'Very cold'), ('Cold', 'Cold'), ('Mild', 'Mild'),
                ('Warm', 'Warm'), ('Hot', 'Hot'), ('VeryHot', 'Very hot')]


def feels_desc(v_c):
    """ Map the feels-like temperature to a description using the configured
    thresholds (each is the max temp for that level, in the display unit). """
    if v_c is None:
        return ''
    feel = v_c * 9 / 5 + 32 if _settings['Units']['Temp'] == 'f' else v_c
    for key, label in FEELS_LEVELS:
        try:
            if feel <= float(_settings['FeelsLike'][key]):
                return label
        except (ValueError, KeyError):
            continue
    return 'Extremely hot'


def _hhmm(epoch, tz_offset_hint=None):
    # Mirror the console's Astro shape: the DISPLAY time lives at index [1]
    # (index [0] is a raw datetime on the real console).
    if not epoch:
        return ['-', '-', 0]
    lt = time.localtime(epoch)
    if _settings['Display']['TimeFormat'] == '24 hr':
        s = '{:02d}:{:02d}'.format(lt.tm_hour, lt.tm_min)
    else:
        h = lt.tm_hour % 12 or 12
        s = '{}:{:02d} {}'.format(h, lt.tm_min, 'am' if lt.tm_hour < 12 else 'pm')
    return ['-', s, epoch]


# --------------------------------------------------------------------------- #
# Moon phase (the console computes this with ephem; here a standard synodic    #
# approximation is plenty for design work)                                    #
# --------------------------------------------------------------------------- #
import math

SYNODIC = 29.530588853
KNOWN_NEW_MOON = 947182440  # 2000-01-06 18:14 UTC, in epoch seconds


def moon_phase(now):
    age = ((now - KNOWN_NEW_MOON) / 86400.0) % SYNODIC
    illum = (1 - math.cos(2 * math.pi * age / SYNODIC)) / 2  # 0..1
    p = age / SYNODIC
    if p < 0.02 or p > 0.98:   text = 'New Moon'
    elif p < 0.24:             text = 'Waxing Crescent'
    elif p < 0.26:             text = 'First Quarter'
    elif p < 0.49:             text = 'Waxing Gibbous'
    elif p < 0.51:             text = 'Full Moon'
    elif p < 0.74:             text = 'Waning Gibbous'
    elif p < 0.76:             text = 'Last Quarter'
    else:                      text = 'Waning Crescent'
    days_to_full = ((SYNODIC / 2 - age) % SYNODIC)
    days_to_new  = ((SYNODIC - age) % SYNODIC)
    return {
        'illum': illum * 100, 'text': text,
        'days_to_full': days_to_full, 'days_to_new': days_to_new,
    }


# --------------------------------------------------------------------------- #
# 24-hour temperature difference (via device observation history)             #
# --------------------------------------------------------------------------- #
_device = {}          # cached {id, type}
_temp24 = {'t': 0, 'value': None}


def get_device(token, station_id):
    if _device:
        return _device.get('id'), _device.get('type')
    try:
        r = requests.get(f'{REST}/stations', params={'token': token}, timeout=10)
        r.raise_for_status()
        for st in r.json().get('stations', []):
            if st.get('station_id') == int(station_id):
                devices = st.get('devices', [])
                for want in ('ST', 'AR'):  # prefer Tempest, then Air
                    for d in devices:
                        if d.get('device_type') == want:
                            _device.update(id=d['device_id'], type=want)
                            return _device['id'], _device['type']
    except Exception as error:
        print(f'  ! device lookup failed: {error}', file=sys.stderr)
    _device.update(id=None, type=None)
    return None, None


def fmt_clock(epoch):
    lt = time.localtime(epoch)
    h = lt.tm_hour % 12 or 12
    ap = 'AM' if lt.tm_hour < 12 else 'PM'
    return '{}:{:02d} {}'.format(h, lt.tm_min, ap)


_today = {'t': 0, 'high': None, 'low': None}


def today_high_low(token, station_id, now):
    """ Actual observed high/low so far today (local day), from device history. """
    if _today['t'] and now - _today['t'] < 300:
        return _today
    dev, dtype = get_device(token, station_id)
    if not dev:
        return _today
    idx = 7 if dtype == 'ST' else 2
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    try:
        r = requests.get(f'{REST}/observations/device/{dev}',
                         params={'token': token, 'time_start': int(midnight),
                                 'time_end': int(now)}, timeout=12)
        r.raise_for_status()
        rows = r.json().get('obs') or []
        hi = lo = None
        for row in rows:
            if len(row) > idx and row[idx] is not None:
                t, v = row[0], row[idx]
                if hi is None or v > hi[0]:
                    hi = (v, t)
                if lo is None or v < lo[0]:
                    lo = (v, t)
        _today.update(t=now, high=hi, low=lo)
    except Exception as error:
        print(f'  ! today high/low fetch failed: {error}', file=sys.stderr)
    return _today


def temp_24h_ago_c(token, station_id, now):
    # Cache for 10 min -- yesterday's temperature moves slowly.
    if _temp24['value'] is not None and now - _temp24['t'] < 600:
        return _temp24['value']
    dev, dtype = get_device(token, station_id)
    if not dev:
        return None
    idx = 7 if dtype == 'ST' else 2  # air_temperature index in device obs arrays
    try:
        r = requests.get(f'{REST}/observations/device/{dev}',
                         params={'token': token,
                                 'time_start': int(now - 86400 - 3600),
                                 'time_end':   int(now - 86400 + 3600)}, timeout=12)
        r.raise_for_status()
        rows = r.json().get('obs') or []
        target, best, best_dt = now - 86400, None, 1e12
        for row in rows:
            if len(row) > idx and row[idx] is not None:
                dt = abs(row[0] - target)
                if dt < best_dt:
                    best_dt, best = dt, row[idx]
        _temp24.update(t=now, value=best)
        return best
    except Exception as error:
        print(f'  ! 24h history fetch failed: {error}', file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# Snapshot builder                                                            #
# --------------------------------------------------------------------------- #
def build_snapshot(token, station_id, version):
    """ Fetch live data and map it to the front-end snapshot shape. Each field
    is defensive so partial API data still renders. """
    obs, meta = {}, {}
    o = {}
    try:
        r = requests.get(f'{REST}/observations/station/{station_id}',
                         params={'token': token}, timeout=10)
        r.raise_for_status()
        j = r.json()
        o = (j.get('obs') or [{}])[0] or {}
        meta = {
            'name':      j.get('station_name', ''),
            'latitude':  j.get('latitude', ''),
            'longitude': j.get('longitude', ''),
            'elevation': round(j['elevation']) if j.get('elevation') is not None else '',
        }
    except Exception as error:
        print(f'  ! station obs fetch failed: {error}', file=sys.stderr)

    def g(k):
        return o.get(k)

    obs = {
        'outTemp':     cv_temp(g('air_temperature'), 1),
        'FeelsLike':   cv_temp(g('feels_like'), 0) + [feels_desc(g('feels_like')), ''],
        'DewPoint':    cv_temp(g('dew_point'), 0),
        'Humidity':    _f(g('relative_humidity'), '{:.0f}', '%'),
        'outTempMax':  cv_temp(g('air_temp_high_today'), 1) + [''],
        'outTempMin':  cv_temp(g('air_temp_low_today'), 1) + [''],
        'outTempTrend': ['--', '', '-'],
        'WindSpd':     cv_wind(g('wind_avg')),
        'WindGust':    cv_wind(g('wind_gust')),
        'AvgWind':     cv_wind(g('wind_avg')),
        'MaxGust':     cv_wind(g('wind_gust')),
        'WindDir':     cv_dir(g('wind_direction')),
        'SLP':         cv_pres(g('sea_level_pressure') or g('barometric_pressure')),
        'SLPMin':      _dash(cv_pres(1013)[1]),
        'SLPMax':      _dash(cv_pres(1013)[1]),
        'RainRate':    cv_rain((g('precip') or 0) * 60, rate=True),
        'TodayRain':   cv_rain(g('precip_accum_local_day')),
        'YesterdayRain': cv_rain(g('precip_accum_local_yesterday')),
        'MonthRain':   _dash(cv_rain(0)[1]),
        'YearRain':    _dash(cv_rain(0)[1]),
        'UVIndex':     _f(g('uv'), '{:.0f}', 'index'),
        'Radiation':   _f(g('solar_radiation'), '{:.0f}', ' W/m²'),
        'peakSun':     ['--', 'hrs'],
        'Strikes3hr':  [str(g('lightning_strike_count_last_3hr'))
                        if g('lightning_strike_count_last_3hr') is not None else '-'],
        'StrikeDist':  cv_dist(g('lightning_strike_last_distance')),
        'StrikesToday': [str(g('lightning_strike_count')) if g('lightning_strike_count') is not None else '-'],
        'StrikesMonth': ['--'],
    }

    # Pressure trend -> pseudo rate so the UI can show a rising/falling arrow
    trend = (g('pressure_trend') or '').lower()
    runit = ' ' + cv_pres(1013)[1].strip() + '/hr'
    obs['SLPTrend'] = {'rising': ['+0.03', runit],
                       'falling': ['-0.03', runit]}.get(trend, ['0.00', runit])

    # Last strike "n ago"
    last = g('lightning_strike_last_epoch')
    if last:
        mins = max(0, int((time.time() - last) / 60))
        if mins < 60:
            obs['StrikeDeltaT'] = [str(mins), 'mins', '-', '-', '#fff']
        else:
            obs['StrikeDeltaT'] = [str(mins // 60), 'hrs', str(mins % 60), 'mins', '#fff']
    else:
        obs['StrikeDeltaT'] = ['-', '-', '-', '-', '#fff']

    # 24-hour temperature difference
    now = time.time()
    cur_c = g('air_temperature')
    prev_c = temp_24h_ago_c(token, station_id, now) if cur_c is not None else None
    tunit = '°F' if _settings['Units']['Temp'] == 'f' else '°C'
    if cur_c is not None and prev_c is not None:
        d_c = cur_c - prev_c
        d_disp = d_c * 9 / 5 if _settings['Units']['Temp'] == 'f' else d_c
        word = 'warmer' if d_c > 0.05 else 'colder' if d_c < -0.05 else 'same'
        obs['outTempDiff'] = ['{:.1f}'.format(abs(d_disp)), tunit, word]
    else:
        obs['outTempDiff'] = ['--', tunit, '-']

    # Actual observed high/low so far today, with the time each occurred
    hl = today_high_low(token, station_id, now)
    if hl.get('high'):
        obs['outTempMax'] = cv_temp(hl['high'][0], 1) + [fmt_clock(hl['high'][1])]
    if hl.get('low'):
        obs['outTempMin'] = cv_temp(hl['low'][0], 1) + [fmt_clock(hl['low'][1])]

    # Moon phase (approximation; the console uses ephem on the Pi)
    mp = moon_phase(now)

    def _days(d):
        d = round(d)
        return 'Tonight' if d <= 0 else ('Tomorrow' if d == 1 else f'in {d} days')

    # Forecast + sun times from the better_forecast endpoint
    astro, met, sager, forecast = {}, {}, {}, []
    astro['Phase'] = ['-', mp['text'], '{:.0f}'.format(mp['illum']), 0]
    astro['FullMoon'] = [_days(mp['days_to_full'])]
    astro['NewMoon'] = [_days(mp['days_to_new'])]
    astro['Moonrise'] = ['--', '']
    astro['Moonset'] = ['--', '']
    try:
        r = requests.get(f'{REST}/better_forecast',
                         params={'station_id': station_id, 'token': token,
                                 'units_temp': _settings['Units']['Temp']}, timeout=10)
        r.raise_for_status()
        fj = r.json()
        daily_all = fj.get('forecast', {}).get('daily') or []
        daily = (daily_all[0] if daily_all else {}) or {}
        cc = fj.get('current_conditions', {})
        astro['Sunrise'] = _hhmm(daily.get('sunrise'))
        astro['Sunset'] = _hhmm(daily.get('sunset'))
        met = {
            'Conditions':   [daily.get('conditions', cc.get('conditions', ''))],
            'Icon':         [daily.get('icon', '')],
            'highTemp':     fmt_temp(daily.get('air_temp_high')),
            'lowTemp':      fmt_temp(daily.get('air_temp_low')),
            'PrecipPercnt': _f(daily.get('precip_probability'), '{:.0f}', '%'),
        }
        # 5-day outlook: one entry per day with weekday, icon, high, low.
        for i, d in enumerate(daily_all[:5]):
            ts = d.get('day_start_local')
            dow = 'Today' if i == 0 else (time.strftime('%a', time.localtime(ts)) if ts else '')
            forecast.append({
                'day':    dow,
                'icon':   d.get('icon', ''),
                'high':   fmt_temp(d.get('air_temp_high')),
                'low':    fmt_temp(d.get('air_temp_low')),
                'precip': _f(d.get('precip_probability'), '{:.0f}', '%'),
            })
        sager = {'Forecast': [cc.get('conditions', '') or daily.get('conditions', '')
                              or 'Live forecast from WeatherFlow.']}
    except Exception as error:
        print(f'  ! forecast fetch failed: {error}', file=sys.stderr)

    # Update notification. Simulated here so the badge/popover can be designed;
    # the production bridge (server/bridge.py) fills this from a real GitHub check.
    update = {
        'available': True,
        'notify': _settings['Display']['UpdateNotification'] == '1',
        'current': 'v26.4.2',
        'latest': 'v26.5.0',
        'url': 'https://github.com/peted-davis/WeatherFlow_PiConsole/releases/latest',
    }

    display = {'TimeFormat': _settings['Display']['TimeFormat'],
               'DateFormat': _settings['Display']['DateFormat']}

    return {
        'build': WEB_UI_BUILD,
        'version': version, 'meta': meta, 'obs': obs,
        'astro': astro, 'met': met, 'sager': sager, 'forecast': forecast,
        'status': {}, 'system': {}, 'update': update, 'display': display,
    }


# --------------------------------------------------------------------------- #
# HTTP handler                                                                #
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):

    token = station_id = None
    _cache = {'t': 0, 'data': None, 'version': 0}

    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self):
        now = time.time()
        c = Handler._cache
        if not c['data'] or now - c['t'] > CACHE_TTL:
            c['version'] += 1
            c['data'] = build_snapshot(self.token, self.station_id, c['version'])
            c['t'] = now
        return c['data']

    def do_GET(self):
        path = self.path.split('?', 1)[0]

        if path == '/api/health':
            return self._send(200, json.dumps({'status': 'ok', 'build': WEB_UI_BUILD}).encode('utf-8'), 'application/json')
        if path == '/api/snapshot':
            body = json.dumps(self._snapshot()).encode('utf-8')
            return self._send(200, body, 'application/json')
        if path == '/api/config':
            return self._send(200, json.dumps(config_schema()).encode('utf-8'), 'application/json')

        # Static files: web/ at '/', fonts/ at '/fonts'
        if path == '/':
            fpath = WEB_DIR / 'index.html'
        elif path.startswith('/fonts/'):
            fpath = FONT_DIR / path[len('/fonts/'):]
        else:
            fpath = WEB_DIR / path.lstrip('/')

        try:
            fpath = fpath.resolve()
            if not (str(fpath).startswith(str(WEB_DIR.resolve()))
                    or str(fpath).startswith(str(FONT_DIR.resolve()))):
                return self._send(403, b'forbidden', 'text/plain')
            body = fpath.read_bytes()
            return self._send(200, body, MIME.get(fpath.suffix, 'application/octet-stream'))
        except FileNotFoundError:
            return self._send(404, b'not found', 'text/plain')

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        if path not in ('/api/config', '/api/system'):
            return self._send(404, b'not found', 'text/plain')
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            return self._send(400, b'{"ok":false,"error":"bad json"}', 'application/json')

        # System power actions are SIMULATED here -- never shut down the dev box.
        if path == '/api/system':
            action = body.get('action')
            print(f'  [dev] system action "{action}" (simulated, no-op)', file=sys.stderr)
            return self._send(200, b'{"ok":true,"simulated":true}', 'application/json')

        section, key, value = body.get('section'), body.get('key'), str(body.get('value'))
        if section in _settings and key in _settings[section]:
            if section == 'Units' and key == 'Temp' and value != _settings['Units']['Temp']:
                convert_feelslike(value)         # keep FeelsLike thresholds in the new unit
            _settings[section][key] = value
            save_settings()
            Handler._cache['t'] = 0              # bust cache so the next poll reflects it
            return self._send(200, b'{"ok":true}', 'application/json')
        return self._send(400, b'{"ok":false,"error":"unknown key"}', 'application/json')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8080)
    args = ap.parse_args()

    token, station_id = load_credentials()
    if not token or not station_id or station_id == 'None':
        print('ERROR: no credentials. Set WEATHERFLOW_TOKEN + WEATHERFLOW_STATION_ID,\n'
              f'       or create {CRED_FILE} with {{"token": "...", "station_id": 12345}}',
              file=sys.stderr)
        sys.exit(1)

    Handler.token = token
    Handler.station_id = station_id

    server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    print(f'  ===== WEB UI BUILD {WEB_UI_BUILD} =====')
    print(f'  Dev preview serving station {station_id} on http://localhost:{args.port}')
    print('  (Ctrl+C to stop)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == '__main__':
    main()
