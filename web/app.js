/* WeatherFlow PiConsole - web UI client.
   Connects to the bridge (server/bridge.py) over a WebSocket, receives a full
   state snapshot whenever the console's data changes, and renders it. Values
   arrive as the console stores them: usually [value, unit, ...] lists, but
   sometimes bare strings, so every read goes through the tolerant helpers. */

'use strict';

// TEMPORARY build stamp — must match WEB_UI_BUILD in server/bridge.py.
// Lets us confirm the browser loaded fresh JS (not a cached copy).
const BUILD = '113';

// ---- tiny DOM + data helpers ------------------------------------------- //
const $ = (id) => document.getElementById(id);
// Only touch the DOM when the value actually changes — avoids needless repaints
// (which flicker badly on the Pi's software renderer).
function set(id, text) { const el = $(id); if (el && el.textContent !== String(text)) el.textContent = String(text); }
function setHTML(id, html) { const el = $(id); if (el && el.innerHTML !== html) el.innerHTML = html; }
function toArr(v) { return Array.isArray(v) ? v : [v]; }

// Strip Kivy markup like [color=ff8837ff]…[/color] the console embeds in some
// text fields, leaving plain text.
function stripMarkup(s) { return (s == null ? '' : String(s)).replace(/\[\/?[^\]]*\]/g, '').trim(); }

// Normalize the feels-like descriptor. The console emits "Feeling hot"; the dev
// preview emits "Hot". Strip any leading "Feeling " and drop placeholders so both
// render the same short word (e.g. "Hot").
function feelsWord(s) {
  s = stripMarkup(s).replace(/^feeling\s+/i, '').trim();
  if (!s || s === '-') return '';
  return s[0].toUpperCase() + s.slice(1);
}

// Weather icons as inline SVG (no emoji font needed — the Pi has none, and
// SVG matches the rest of the vector UI). Small building blocks + a mapper.
const WI = {
  sun: '<circle cx="12" cy="12" r="5" fill="#ffb454"/><g stroke="#ffb454" stroke-width="2" stroke-linecap="round"><line x1="12" y1="1.5" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22.5"/><line x1="1.5" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22.5" y2="12"/><line x1="4.2" y1="4.2" x2="6" y2="6"/><line x1="18" y1="18" x2="19.8" y2="19.8"/><line x1="4.2" y1="19.8" x2="6" y2="18"/><line x1="18" y1="6" x2="19.8" y2="4.2"/></g>',
  moon: '<path d="M15.5 2.5A9 9 0 1 0 21.5 16 7 7 0 0 1 15.5 2.5z" fill="#c9d2ff"/>',
  cloud: '<path d="M7 18h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6 9.5 4.5 4.5 0 0 0 7 18z" fill="#9aa7c2"/>',
  partly: '<circle cx="8" cy="8" r="3.3" fill="#ffb454"/><g stroke="#ffb454" stroke-width="1.6" stroke-linecap="round"><line x1="8" y1="1.6" x2="8" y2="3.2"/><line x1="1.6" y1="8" x2="3.2" y2="8"/><line x1="3.4" y1="3.4" x2="4.6" y2="4.6"/></g><path d="M9 19h8a3.6 3.6 0 0 0 .4-7.16A5.1 5.1 0 0 0 8 11.3 4 4 0 0 0 9 19z" fill="#9aa7c2"/>',
  partlyNight: '<path d="M9 6.5A5 5 0 1 0 13 12 4 4 0 0 1 9 6.5z" fill="#c9d2ff"/><path d="M9 19h8a3.6 3.6 0 0 0 .4-7.16A5.1 5.1 0 0 0 8 11.3 4 4 0 0 0 9 19z" fill="#9aa7c2"/>',
  rain: '<path d="M7 14.5h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6 6 4.5 4.5 0 0 0 7 14.5z" fill="#9aa7c2"/><g stroke="#5ad1e6" stroke-width="2" stroke-linecap="round"><line x1="8" y1="17.5" x2="7" y2="21"/><line x1="12" y1="17.5" x2="11" y2="21"/><line x1="16" y1="17.5" x2="15" y2="21"/></g>',
  thunder: '<path d="M7 14h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6 5.5 4.5 4.5 0 0 0 7 14z" fill="#9aa7c2"/><path d="M12.5 13.5l-4 5.5h2.7L9.5 23l5-6.5h-2.9z" fill="#ffb454"/>',
  snow: '<path d="M7 14h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6 5.5 4.5 4.5 0 0 0 7 14z" fill="#9aa7c2"/><g fill="#dbe4ff"><circle cx="8" cy="18.5" r="1.3"/><circle cx="12" cy="20.5" r="1.3"/><circle cx="16" cy="18.5" r="1.3"/></g>',
  fog: '<path d="M7 12.5h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6 4 4.5 4.5 0 0 0 7 12.5z" fill="#9aa7c2"/><g stroke="#9aa7c2" stroke-width="2" stroke-linecap="round"><line x1="4.5" y1="16.5" x2="19.5" y2="16.5"/><line x1="6.5" y1="20" x2="17.5" y2="20"/></g>',
};

function weatherIconSVG(token) {
  const t = (token || '').toLowerCase();
  let inner;
  if (t.includes('thunder')) inner = WI.thunder;
  else if (t.includes('snow') || t.includes('sleet') || t.includes('flurr')) inner = WI.snow;
  else if (t.includes('rain') || t.includes('drizzle') || t.includes('shower')) inner = WI.rain;
  else if (t.includes('fog') || t.includes('haze') || t.includes('mist')) inner = WI.fog;
  else if (t.includes('partly') || t.includes('mostly-clear')) inner = t.includes('night') ? WI.partlyNight : WI.partly;
  else if (t.includes('cloud') || t.includes('overcast')) inner = WI.cloud;
  else if (t.includes('clear') || t.includes('sunny') || t.includes('fair')) inner = t.includes('night') ? WI.moon : WI.sun;
  else inner = WI.cloud;
  return `<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">${inner}</svg>`;
}

// Update the header weather icon only when the token changes (no per-render repaint).
function setWeatherIcon(token) {
  const el = $('condIcon');
  if (!el || el.dataset.icon === token) return;
  el.dataset.icon = token;
  el.innerHTML = weatherIconSVG(token);
}

// 5-day outlook in the hero pane: weekday, icon, high (red), low (blue).
function renderForecast(list) {
  const el = $('forecast5');
  if (!el) return;
  const days = Array.isArray(list) ? list : [];
  const drop = '<svg class="fc-drop" viewBox="0 0 24 24" aria-hidden="true">'
    + '<path d="M12 3s6 7 6 11a6 6 0 0 1-12 0c0-4 6-11 6-11z" fill="currentColor"/></svg>';
  const html = days.map(function (d) {
    const dow  = stripMarkup(d.day || '');
    const icon = weatherIconSVG(d.icon || d.conditions || '');
    const precip = drop + vu(d.precip, '0%');
    return '<div class="fc-day">'
      + '<div class="fc-dow">' + dow + '</div>'
      + '<div class="fc-icon">' + icon + '</div>'
      + '<div class="fc-temps"><span class="fc-hi">' + vu(d.high) + '</span>'
      + '<span class="fc-lo">' + vu(d.low) + '</span></div>'
      + '<div class="fc-precip">' + precip + '</div>'
      + '</div>';
  }).join('');
  if (el.innerHTML !== html) el.innerHTML = html;
}

// Draw the illuminated portion of the moon into #moonLit (viewBox 100x100).
function drawMoon(I, waxing) {
  const el = $('moonLit');
  if (!el) return;
  const cx = 50, cy = 50, r = 46;
  I = Math.max(0, Math.min(1, I));
  const t = Math.cos(Math.PI * I);          // I: 0→1 (new), .5→0 (quarter), 1→-1 (full)
  const rx = Math.abs(t) * r;
  const outer = waxing ? 1 : 0;             // lit limb: right if waxing, left if waning
  const inner = waxing ? (t > 0 ? 0 : 1) : (t > 0 ? 1 : 0);
  el.setAttribute('d', `M ${cx} ${cy - r} A ${r} ${r} 0 0 ${outer} ${cx} ${cy + r} A ${rx} ${r} 0 0 ${inner} ${cx} ${cy - r} Z`);
}

// A "missing" value is null, or the console's dash placeholders ('-', '--'...)
function missing(x) {
  return x === null || x === undefined || (typeof x === 'string' && /^-*$/.test(x.trim()) && x.trim() !== '');
}
// Read element i of an observation, with a fallback for missing/empty values.
function part(v, i = 0, dflt = '--') {
  const x = toArr(v)[i];
  if (x === null || x === undefined || x === '' || x === '-') return dflt;
  return x;
}
// value + unit, e.g. "75.2" + "°F" -> "75.2°F"
function vu(v, dflt = '--') {
  const a = toArr(v);
  const val = a[0];
  if (val === null || val === undefined || val === '' || val === '-') return dflt;
  const unit = (a[1] === null || a[1] === undefined || a[1] === '-') ? '' : a[1];
  return `${val}${unit}`;
}

// ---- domain helpers ---------------------------------------------------- //
const CARDINALS = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
function cardinal(deg) {
  if (isNaN(deg)) return '';
  return CARDINALS[Math.round(((deg % 360) / 22.5)) % 16];
}

// Numeric degrees for the compass needle, whether WindDir is "248" or "WSW".
function windDegrees(w) {
  const v = part(w, 0, '');
  const n = parseFloat(v);
  if (!isNaN(n)) return n;
  const i = CARDINALS.indexOf(String(v).toUpperCase());
  return i >= 0 ? i * 22.5 : NaN;
}

// Barometer display range per unit (matches the console's own scale bands).
function baroRange(unit) {
  const u = (unit || '').trim().toLowerCase();
  if (u.includes('inhg')) return [27.5, 31.5, 2];
  if (u.includes('mmhg')) return [699, 800, 0];
  return [950, 1050, 0]; // hPa / mb
}

function uvLabel(v) {
  if (v <= 2) return ['Low', 'var(--good)'];
  if (v <= 5) return ['Moderate', 'var(--good)'];
  if (v <= 7) return ['High', 'var(--warn)'];
  if (v <= 10) return ['Very High', 'var(--crit)'];
  return ['Extreme', 'var(--violet)'];
}

// ---- render ------------------------------------------------------------ //
let baroLen = 0;

function render(s) {
  const obs = s.obs || {}, astro = s.astro || {}, met = s.met || {},
        sager = s.sager || {}, meta = s.meta || {}, upd = s.update || {};
  if (s.display) displayPrefs = s.display;

  // TEMPORARY: surface the UI build vs the server build so a stale cached
  // app.js is obvious (red tag + console warning).
  const tag = $('buildTag');
  if (tag) {
    const srv = s.build || '?';
    tag.textContent = `ui ${BUILD} · srv ${srv}`;
    tag.classList.toggle('mismatch', !!s.build && s.build !== BUILD);
  }
  // Auto-reload the kiosk once when a NEW server build appears. Keyed on the
  // server build value (not a timer) so it reloads at most once per distinct
  // build — never loops, even if this app.js is newer than the server reports.
  if (s.build && s.build !== BUILD && sessionStorage.getItem('wfReloadedFor') !== s.build) {
    sessionStorage.setItem('wfReloadedFor', s.build);
    location.reload();
  }

  // Header
  if (meta.name) set('station', meta.name);
  const loc = [meta.latitude && meta.longitude ? `${(+meta.latitude).toFixed(3)}, ${(+meta.longitude).toFixed(3)}` : '',
               meta.elevation ? `${Math.round(+meta.elevation * 3.28084).toLocaleString()} ft` : ''].filter(Boolean).join(' · ');
  if (loc) set('place', loc);

  // Temperature
  set('outTemp', vu(obs.outTemp));
  set('tempMin', vu(obs.outTempMin));       // actual observed low
  set('tempMax', vu(obs.outTempMax));       // actual observed high
  set('tempMinTime', part(obs.outTempMin, 2, '--'));
  set('tempMaxTime', part(obs.outTempMax, 2, '--'));
  set('feelsLike', ' ' + vu(obs.FeelsLike));
  set('feelsDesc', feelsWord(part(obs.FeelsLike, 2, '')));
  renderForecast(s.forecast);
  set('dewPoint', vu(obs.DewPoint));
  set('humidity', vu(obs.Humidity));
  set('tempTrend', vu(obs.outTempTrend));

  // 24-hour difference headline: "3.2°F warmer than yesterday"
  const diffVal = part(obs.outTempDiff, 0, '--');
  const diffWord = stripMarkup(part(obs.outTempDiff, 2, '')).toLowerCase();
  const diffEl = $('tempDiff');
  if (diffEl) {
    if (diffVal === '--' || !diffWord || diffWord === 'same' || diffWord === '-') {
      diffEl.innerHTML = diffVal === '--' ? '' : 'About the same as 24h ago';
      diffEl.style.color = 'var(--muted)';
    } else {
      const arrow = /warm/.test(diffWord) ? '↑' : '↓';
      diffEl.innerHTML = `${arrow} <b>${diffVal}${part(obs.outTempDiff, 1, '')}</b> ${diffWord} than 24h ago`;
      diffEl.style.color = /warm/.test(diffWord) ? 'var(--amber-soft)' : 'var(--cyan)';
    }
  }
  const trendColor = part(obs.outTempTrend, 2, '');
  if ($('tempTrend') && trendColor && trendColor.startsWith('#')) $('tempTrend').style.color = trendColor;

  // Wind — WindDir arrives formatted per the Direction unit ("248"+"°" or "WSW"),
  // so display it as-is and derive the needle angle (reverse-mapping cardinals).
  set('windSpd', part(obs.WindSpd, 0));
  const wdir = toArr(obs.WindDir);
  const dirVal = part(obs.WindDir, 0, '');
  const dirSuffix = (wdir[1] && wdir[1] !== '-') ? wdir[1] : '';
  const dirDisplay = (dirVal && dirVal !== '--') ? dirVal + dirSuffix : '';
  set('windDir', `${part(obs.WindSpd, 1, '')} ${dirDisplay}`.trim());
  const deg = windDegrees(obs.WindDir);
  const needle = $('needle');
  if (needle && !isNaN(deg)) needle.setAttribute('transform', `rotate(${deg} 100 100)`);
  set('windGust', vu(obs.WindGust));
  set('avgWind', vu(obs.AvgWind));
  set('maxGust', vu(obs.MaxGust));

  // Pressure
  set('slp', part(obs.SLP, 0));
  set('slpUnit', part(obs.SLP, 1, ''));
  set('slpMin', vu(obs.SLPMin));
  set('slpMax', vu(obs.SLPMax));
  const trendVal = parseFloat(part(obs.SLPTrend, 0, 'NaN'));
  if ($('slpTrend')) {
    if (isNaN(trendVal)) { set('slpTrend', ''); }
    else {
      const arrow = trendVal > 0.02 ? '▲ Rising' : trendVal < -0.02 ? '▼ Falling' : '▬ Steady';
      set('slpTrend', `${arrow}`);
      $('slpTrend').style.color = trendVal > 0.02 ? 'var(--good)' : trendVal < -0.02 ? 'var(--crit)' : 'var(--muted)';
    }
  }
  const slpVal = parseFloat(part(obs.SLP, 0, 'NaN'));
  const [bmin, bmax, bdp] = baroRange(part(obs.SLP, 1, ''));
  set('baroMinLbl', bmin.toFixed(bdp)); set('baroMaxLbl', bmax.toFixed(bdp));
  const baro = $('baroArc');
  if (baro && baroLen && !isNaN(slpVal)) {
    const frac = Math.max(0, Math.min(1, (slpVal - bmin) / (bmax - bmin)));
    baro.style.strokeDashoffset = baroLen * (1 - frac);
  }

  // Rainfall
  set('todayRain', vu(obs.TodayRain));
  set('rainRate', vu(obs.RainRate));
  set('yesterdayRain', vu(obs.YesterdayRain));
  set('monthRain', vu(obs.MonthRain));
  set('yearRain', vu(obs.YearRain));

  // Sun & UV
  const uv = parseFloat(part(obs.UVIndex, 0, 'NaN'));
  set('uvIndex', isNaN(uv) ? '--' : uv.toFixed(0));
  if (!isNaN(uv)) {
    const [lbl, col] = uvLabel(uv);
    set('uvLabel', `UV · ${lbl}`);
    if ($('uvLabel')) $('uvLabel').style.color = col;
    if ($('uvMarker')) $('uvMarker').style.left = `${Math.min(100, (uv / 11) * 100)}%`;
  }
  set('radiation', vu(obs.Radiation));
  set('peakSun', vu(obs.peakSun));
  set('sunrise', stripMarkup(part(astro.Sunrise, 1, '--')));
  set('sunset', stripMarkup(part(astro.Sunset, 1, '--')));

  // Lightning
  set('strikes3hr', part(obs.Strikes3hr, 0));
  const dt = toArr(obs.StrikeDeltaT);
  if (dt.length >= 4 && !missing(dt[0])) {
    set('strikeDeltaT', `${part(dt, 0)} ${part(dt, 1, '')} ago`);
  } else { set('strikeDeltaT', 'none'); }
  set('strikeDist', vu(obs.StrikeDist));
  set('strikesToday', part(obs.StrikesToday, 0));
  set('strikesMonth', part(obs.StrikesMonth, 0));

  // Header "now" summary (today's forecast at a glance)
  set('condText', stripMarkup(part(met.Conditions, 0, '—')));
  setWeatherIcon(part(met.Icon, 0, ''));
  set('condHi', vu(met.highTemp));
  set('condLo', vu(met.lowTemp));
  set('condPrecip', vu(met.PrecipPercnt));

  // Forecast narrative in the hero pane (Sager on the console; conditions text here)
  const forecast = stripMarkup(part(sager.Forecast, 0, ''));
  if (forecast && forecast !== '-') set('sager', forecast);

  // Moon
  const phaseText = stripMarkup(part(astro.Phase, 1, '—'));
  set('moonPhase', phaseText);
  const illum = parseFloat(part(astro.Phase, 2, 'NaN'));
  set('moonIllum', isNaN(illum) ? '--' : `${illum.toFixed(0)}%`);
  if (!isNaN(illum)) drawMoon(illum / 100, /wax|first|new/i.test(phaseText));
  set('moonrise', stripMarkup(part(astro.Moonrise, 1, '--')));
  set('moonset', stripMarkup(part(astro.Moonset, 1, '--')));
  set('fullMoon', stripMarkup(part(astro.FullMoon, 0, '--')));
  set('newMoon', stripMarkup(part(astro.NewMoon, 0, '--')));

  // Update notification badge (popover interactions wired in init)
  const badge = $('updateBadge');
  if (badge) {
    if (upd.available && upd.notify !== false) {
      badge.hidden = false;
      set('verCurrent', upd.current || '--');
      set('verLatest', upd.latest || '--');
      const link = $('verLink');
      if (link) link.href = upd.url || '#';
    } else {
      badge.hidden = true;   // popover is closed by user interaction only, not render
    }
  }
}

// ---- connection -------------------------------------------------------- //
let lastUpdate = 0, connected = false;

function setLive(state, text) {
  const el = $('live');
  if (el) el.dataset.state = state;
  set('liveText', text);
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try { ws = new WebSocket(`${proto}://${location.host}/ws`); }
  catch (err) { return; }
  ws.onopen = () => { connected = true; setLive('ok', 'Live'); };
  ws.onmessage = (e) => {
    lastUpdate = Date.now();
    try { render(JSON.parse(e.data)); } catch (err) { console.error('render error', err); }
  };
  ws.onclose = () => { connected = false; setTimeout(connect, 2000); };
  ws.onerror = () => ws.close();
}

// Polling fallback. Dormant when the WebSocket is connected (production on the
// Pi); keeps the UI live when only a plain HTTP server is available (the local
// dev-preview server, which has no WebSocket).
async function poll() {
  if (connected) return;
  try {
    const r = await fetch('/api/snapshot', { cache: 'no-store' });
    const s = await r.json();
    if (s && s.version !== undefined) { lastUpdate = Date.now(); render(s); setLive('ok', 'Live'); }
  } catch (err) {
    setLive('down', 'Offline');
  }
}
setInterval(poll, 4000);

// Flag stale data (connected but no fresh snapshot for a while)
setInterval(() => {
  if (Date.now() - lastUpdate > 15000) { if (connected) setLive('stale', 'Stale'); }
  else setLive('ok', 'Live');
}, 3000);

// Force an immediate snapshot fetch (used right after a settings change)
function refreshNow() {
  fetch('/api/snapshot', { cache: 'no-store' })
    .then(r => r.json())
    .then(s => { if (s && s.version !== undefined) { lastUpdate = Date.now(); render(s); } })
    .catch(() => {});
}

// ---- settings drawer ---------------------------------------------------- //
let settingsLoaded = false;

async function loadSettings() {
  const body = $('settingsBody');
  try {
    const cfg = await (await fetch('/api/config', { cache: 'no-store' })).json();
    renderSettings(cfg);
    settingsLoaded = true;
  } catch (err) {
    if (body) body.innerHTML = '<p style="color:var(--muted);padding:16px 0">Settings are unavailable.</p>';
  }
}

function postConfig(section, key, value, reload) {
  fetch('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ section, key, value }),
  }).then(() => {
    setTimeout(refreshNow, 300);   // let the console apply the change, then refresh data
    if (reload) loadSettings();    // relabel/convert dependent fields (e.g. Feels-Like unit)
  }).catch(() => {});
}

function segControl(f) {
  const seg = document.createElement('div');
  seg.className = 'seg';
  (f.options || []).forEach(opt => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = (f.labels && f.labels[opt]) || opt;
    b.setAttribute('aria-pressed', String(opt === f.value));
    b.addEventListener('click', () => { if (opt !== f.value) postConfig(f.section, f.key, opt, true); });
    seg.appendChild(b);
  });
  return seg;
}

function toggleControl(f) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'toggle';
  let on = f.value === '1' || f.value === 1 || f.value === true;
  btn.setAttribute('aria-pressed', String(on));
  btn.addEventListener('click', () => {
    on = !on;
    btn.setAttribute('aria-pressed', String(on));
    postConfig(f.section, f.key, on ? '1' : '0', false);
  });
  return btn;
}

function stepperControl(f) {
  const wrap = document.createElement('div');
  wrap.className = 'stepper';
  let v = parseInt(f.value, 10);
  if (isNaN(v)) v = 0;
  const val = document.createElement('span');
  val.className = 'val';
  const show = () => { val.textContent = v + (f.unit ? ` ${f.unit}` : ''); };
  show();
  let timer;
  const commit = () => { clearTimeout(timer); timer = setTimeout(() => postConfig(f.section, f.key, String(v), false), 400); };
  const minus = document.createElement('button');
  minus.type = 'button'; minus.textContent = '−';
  minus.addEventListener('click', () => { v -= 1; show(); commit(); });
  const plus = document.createElement('button');
  plus.type = 'button'; plus.textContent = '+';
  plus.addEventListener('click', () => { v += 1; show(); commit(); });
  wrap.append(minus, val, plus);
  return wrap;
}

function renderSettings(cfg) {
  const body = $('settingsBody');
  if (!body) return;
  body.innerHTML = '';
  (cfg.sections || []).forEach(sec => {
    const wrap = document.createElement('div');
    wrap.className = 'set-section';
    const h = document.createElement('h3');
    h.textContent = sec.title || sec.name;
    wrap.appendChild(h);
    if (sec.desc) {
      const d = document.createElement('div');
      d.className = 'sec-desc';
      d.textContent = sec.desc;
      wrap.appendChild(d);
    }
    (sec.fields || []).forEach(f => {
      const row = document.createElement('div');
      row.className = 'set-row';
      const label = document.createElement('div');
      label.className = 'set-label';
      label.textContent = f.title;
      row.appendChild(label);
      const control = f.type === 'stepper' ? stepperControl(f)
        : f.type === 'toggle' ? toggleControl(f)
        : segControl(f);
      row.appendChild(control);
      wrap.appendChild(row);
    });
    body.appendChild(wrap);
  });
}

function openSettings(open) {
  const drawer = $('settingsDrawer'), scrim = $('settingsScrim');
  if (!drawer || !scrim) return;
  drawer.classList.toggle('open', open);
  scrim.classList.toggle('open', open);
  if (open && !settingsLoaded) loadSettings();
}

// ---- system power actions ---------------------------------------------- //
const POWER = {
  exit: { title: 'Exit console?', body: 'The console app will close. Restart it from the Pi to resume.',
          confirm: 'Exit', danger: false, overlay: 'Console has exited.' },
  reboot: { title: 'Reboot console?', body: 'The Raspberry Pi will restart. This display reconnects automatically once it is back.',
            confirm: 'Reboot', danger: true, overlay: 'Rebooting… the console will reconnect when it’s back.' },
  shutdown: { title: 'Shut down console?', body: 'The Raspberry Pi will power off. You’ll need to power it back on manually.',
              confirm: 'Shut down', danger: true, overlay: 'Shutting down… it is now safe to remove power.' },
};

function confirmDialog(opts, onConfirm) {
  const scrim = document.createElement('div');
  scrim.className = 'confirm-scrim';
  scrim.innerHTML = '<div class="confirm-card" role="dialog" aria-modal="true">'
    + '<h4></h4><p></p><div class="confirm-row">'
    + '<button class="confirm-cancel" type="button">Cancel</button>'
    + `<button class="confirm-ok${opts.danger ? '' : ' neutral'}" type="button"></button>`
    + '</div></div>';
  scrim.querySelector('h4').textContent = opts.title;
  scrim.querySelector('p').textContent = opts.body;
  scrim.querySelector('.confirm-ok').textContent = opts.confirm;
  const close = () => scrim.remove();
  scrim.querySelector('.confirm-cancel').addEventListener('click', close);
  scrim.addEventListener('click', (e) => { if (e.target === scrim) close(); });
  scrim.querySelector('.confirm-ok').addEventListener('click', () => { close(); onConfirm(); });
  document.body.appendChild(scrim);
}

function systemAction(action) {
  const cfg = POWER[action];
  if (!cfg) return;
  confirmDialog(cfg, () => {
    fetch('/api/system', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    }).catch(() => {});
    const ov = document.createElement('div');
    ov.className = 'sys-overlay';
    ov.innerHTML = `<div class="spin"></div><p>${cfg.overlay}</p>`;
    document.body.appendChild(ov);
  });
}

// ---- clock (honors Display time/date format settings) ------------------ //
let displayPrefs = {};
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DAYS_LONG = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MONS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const p2 = (n) => String(n).padStart(2, '0');

function formatDate(d, fmt) {
  fmt = fmt || 'Mon, 01 Jan 0000';
  const weekday = fmt.indexOf('Monday') === 0 ? DAYS_LONG[d.getDay()] : DAYS[d.getDay()];
  const mon = MONS[d.getMonth()], dd = p2(d.getDate());
  const md = /Jan 01/.test(fmt) ? `${mon} ${dd}` : `${dd} ${mon}`;
  return `${weekday}, ${md} ${d.getFullYear()}`;
}

function tick() {
  const n = new Date();
  if (displayPrefs.TimeFormat === '24 hr') {
    set('time', `${p2(n.getHours())}:${p2(n.getMinutes())}:${p2(n.getSeconds())}`);
  } else {
    const ap = n.getHours() >= 12 ? 'PM' : 'AM';
    set('time', `${n.getHours() % 12 || 12}:${p2(n.getMinutes())}:${p2(n.getSeconds())} ${ap}`);
  }
  set('date', formatDate(n, displayPrefs.DateFormat));
}

// ---- boot -------------------------------------------------------------- //
(function init() {
  console.log(`%c[wfpiconsole-web] build ${BUILD}`, 'color:#5ad1e6;font-weight:bold');
  if ($('buildTag')) $('buildTag').textContent = `ui ${BUILD} · srv …`;

  // Compass tick marks
  const g = $('ticks');
  if (g) {
    for (let i = 0; i < 72; i++) {
      const major = i % 6 === 0;
      const a = (i / 72) * 2 * Math.PI;
      const r1 = 88, r2 = major ? 79 : 83;
      const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      l.setAttribute('x1', 100 + r1 * Math.sin(a)); l.setAttribute('y1', 100 - r1 * Math.cos(a));
      l.setAttribute('x2', 100 + r2 * Math.sin(a)); l.setAttribute('y2', 100 - r2 * Math.cos(a));
      l.setAttribute('stroke-width', major ? 1.8 : 0.9);
      l.setAttribute('opacity', major ? 1 : 0.6);
      g.appendChild(l);
    }
  }
  // Prep barometer arc for smooth fills
  const baro = $('baroArc');
  if (baro) {
    baroLen = baro.getTotalLength();
    baro.style.strokeDasharray = baroLen;
    baro.style.strokeDashoffset = baroLen;
    baro.style.transition = 'stroke-dashoffset 1.2s cubic-bezier(.2,.9,.25,1)';
  }
  const nd = $('needle');
  if (nd) nd.style.transition = 'transform 1.2s cubic-bezier(.2,.9,.25,1)';

  // Update popover: tap badge to toggle, dismiss on close / outside click / Esc
  const badge = $('updateBadge'), pop = $('updatePopover'), closeBtn = $('updateClose');
  function onDocClickPopover(e) {
    if (!pop.contains(e.target) && !badge.contains(e.target)) showPopover(false);
  }
  function showPopover(open) {
    if (!pop || !badge) return;
    pop.hidden = !open;
    badge.setAttribute('aria-expanded', open ? 'true' : 'false');
    // Attach the outside-click listener only while open, on the next tick so
    // the click that opened it can't immediately close it.
    if (open) setTimeout(() => document.addEventListener('click', onDocClickPopover), 0);
    else document.removeEventListener('click', onDocClickPopover);
  }
  if (badge) badge.addEventListener('click', () => showPopover(pop.hidden));
  if (closeBtn) closeBtn.addEventListener('click', () => showPopover(false));

  // Settings drawer
  const settingsBtn = $('settingsBtn'), settingsClose = $('settingsClose'), scrim = $('settingsScrim');
  if (settingsBtn) settingsBtn.addEventListener('click', () => openSettings(true));
  if (settingsClose) settingsClose.addEventListener('click', () => openSettings(false));
  if (scrim) scrim.addEventListener('click', () => openSettings(false));

  // Power actions
  document.querySelectorAll('.power-btn').forEach(b =>
    b.addEventListener('click', () => systemAction(b.dataset.action)));

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      showPopover(false);
      const cs = document.querySelector('.confirm-scrim');
      if (cs) cs.remove(); else openSettings(false);
    }
  });

  tick(); setInterval(tick, 1000);
  connect();

  // Also grab an immediate snapshot in case the socket is slow to open.
  fetch('/api/snapshot').then(r => r.json()).then(render).catch(() => {});
})();
