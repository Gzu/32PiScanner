/* 32PiScanner control GUI — vanilla JS, no frameworks, no external resources.
   Talks to gui.py: REST under /api/*, live state via SSE /api/events.
   Sections: state / audio / api / sse / render / overlays / wiring. */
'use strict';

/* ─── state ─────────────────────────────────────────────────────────────── */

const state = {
  fleet: null,       // {checked_at, expected, pis: [...]}
  verdict: null,     // {state, reasons, counts}
  config: null,      // persisted gui.py config
  current_op: null,  // name of op holding the rig lock, or null
  sessions: [],      // newest-first
  ticker: [],        // ring of {ts, level, text}
};

const ui = {
  form: { exp: 2000, gain: 4.0, awbr: 1.8, awbb: 1.6, q: 95 },
  formTouched: false,
  lead: 2.0,
  leadTouched: false,
  motionSafe: true,
  override: false,
  subject: null,
  localSubjects: [],       // NEW-chip subjects not yet known to the backend config
  lastApply: null,         // {at, acks}
  lastSpread: null,        // {ms, ok}
  lastFire: null,          // {subject, test}
  sseUp: false,
};

const STEP_DEFS = {
  exp:  { step: 100,  min: 100, max: 100000, dp: 0 },
  gain: { step: 0.1,  min: 1.0, max: 16.0,   dp: 1 },
  awbr: { step: 0.05, min: 0.5, max: 4.0,    dp: 2 },
  awbb: { step: 0.05, min: 0.5, max: 4.0,    dp: 2 },
  q:    { step: 1,    min: 50,  max: 100,    dp: 0 },
  lead: { step: 0.5,  min: 1.0, max: 10.0,   dp: 1 },
};

function $(id) { return document.getElementById(id); }

function h(tag, attrs) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const k of Object.keys(attrs)) {
      const v = attrs[k];
      if (v === null || v === undefined) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
  }
  for (let i = 2; i < arguments.length; i++) {
    const kid = arguments[i];
    if (kid !== null && kid !== undefined) el.append(kid);
  }
  return el;
}

function fmtClock(unixS) {
  const d = new Date(unixS * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function cfgNum(key, dflt) {
  const v = state.config && state.config[key];
  return (typeof v === 'number') ? v : dflt;
}

function expectedPis() {
  if (state.fleet && typeof state.fleet.expected === 'number') return state.fleet.expected;
  return cfgNum('expected_pis', 32);
}

function sanitizeSubject(s) {
  const out = String(s || '').toLowerCase().replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return out || null;
}

function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

function subjects() {
  const fromCfg = (state.config && Array.isArray(state.config.subjects))
    ? state.config.subjects : [];
  const merged = [];
  for (const s of fromCfg.concat(ui.localSubjects)) {
    if (s && merged.indexOf(s) < 0) merged.push(s);
  }
  return merged;
}

function nextSessionName(subject) {
  const sub = sanitizeSubject(subject);
  if (!sub) return null;
  const date = todayStr();
  const re = new RegExp('^' + date + '_' + sub + '_take(\\d+)$');
  let maxN = 0;
  for (const s of state.sessions) {
    const m = re.exec(s.session || '');
    if (m) maxN = Math.max(maxN, parseInt(m[1], 10));
  }
  return date + '_' + sub + '_take' + String(maxN + 1).padStart(2, '0');
}

// Client-side ticker entries (busy warnings etc.) share the same tape.
function localTicker(level, text) {
  state.ticker.push({ ts: Date.now() / 1000, level: level, text: text });
  if (state.ticker.length > 200) state.ticker.shift();
  renderTicker();
}

/* ─── audio ─────────────────────────────────────────────────────────────── */

const audio = {
  ctx: null,
  muted: localStorage.getItem('pi32.mute') === '1',
  ensure() {
    if (!this.ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) { try { this.ctx = new AC(); } catch (e) { /* no audio */ } }
    }
    if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume();
  },
  beep(freq, ms) {
    if (this.muted || !this.ctx) return;
    const t = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();
    osc.type = 'square';
    osc.frequency.value = freq;
    osc.connect(gain);
    gain.connect(this.ctx.destination);
    gain.gain.setValueAtTime(0.07, t);
    gain.gain.exponentialRampToValueAtTime(0.0005, t + ms / 1000);
    osc.start(t);
    osc.stop(t + ms / 1000 + 0.02);
  },
  toggle() {
    this.muted = !this.muted;
    localStorage.setItem('pi32.mute', this.muted ? '1' : '0');
    renderTopbar();
  },
};

/* ─── api ───────────────────────────────────────────────────────────────── */

async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON error body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || ('HTTP ' + res.status));
    err.status = res.status;
    err.data = data || {};
    throw err;
  }
  return data;
}

// Standard failure path: 409 busy → ticker warn; anything else → ticker fail.
function apiFail(err, what) {
  if (err.status === 409 && err.data && err.data.error === 'busy') {
    localTicker('warn', 'BUSY · ' + String(err.data.op || 'op').toUpperCase()
      + ' in progress — ' + what + ' skipped');
  } else {
    localTicker('fail', what + ' failed · ' + err.message);
  }
  render();
}

async function refreshState() {
  try {
    const res = await fetch('/api/state');
    if (res.ok) seed(await res.json());
  } catch (e) { /* SSE will resupply */ }
  render();
}

// Sweep payloads arrive as {fleet, verdict} (SSE 'fleet' events and POST /api/ping).
function absorbSweep(d) {
  if (!d) return;
  if (d.fleet && d.fleet.pis) state.fleet = d.fleet;
  else if (d.pis) state.fleet = d;
  if (d.verdict) state.verdict = d.verdict;
}

function seed(snap) {
  if (!snap) return;
  if (typeof snap.sim === 'boolean') state.sim = snap.sim;
  if (snap.fleet) state.fleet = snap.fleet;
  if (snap.verdict) state.verdict = snap.verdict;
  if (snap.config) state.config = snap.config;
  state.current_op = snap.current_op || null;
  if (Array.isArray(snap.sessions)) state.sessions = snap.sessions;
  if (Array.isArray(snap.ticker)) state.ticker = snap.ticker.slice(-200);

  const cfg = state.config || {};
  if (!ui.leadTouched && typeof cfg.leadtime_s === 'number') ui.lead = cfg.leadtime_s;
  if (!ui.formTouched && cfg.last_configure) {
    const lc = cfg.last_configure;
    if (typeof lc.exposure_us === 'number') ui.form.exp = lc.exposure_us;
    if (typeof lc.analogue_gain === 'number') ui.form.gain = lc.analogue_gain;
    if (Array.isArray(lc.awb_gains)) {
      ui.form.awbr = lc.awb_gains[0];
      ui.form.awbb = lc.awb_gains[1];
    }
    if (typeof lc.jpeg_quality === 'number') ui.form.q = lc.jpeg_quality;
    if (Array.isArray(lc.resolution)) {
      $('sel-res').value = lc.resolution[0] + 'x' + lc.resolution[1];
    }
  }
  if (!ui.subject) {
    const subs = subjects();
    if (subs.length) ui.subject = subs[0];
  }
}

/* ─── sse ───────────────────────────────────────────────────────────────── */

let es = null;

function connectSSE() {
  if (es) { try { es.close(); } catch (e) { /* already dead */ } }
  es = new EventSource('/api/events');

  es.addEventListener('snapshot', (e) => {
    ui.sseUp = true;
    seed(JSON.parse(e.data));
    render();
  });
  es.addEventListener('fleet', (e) => {
    absorbSweep(JSON.parse(e.data));
    render();
  });
  es.addEventListener('ticker', (e) => {
    state.ticker.push(JSON.parse(e.data));
    if (state.ticker.length > 200) state.ticker.shift();
    renderTicker();
  });
  es.addEventListener('op', (e) => {
    const m = JSON.parse(e.data);
    // Step/line-scoped events (take steps, preflight checks, update-fleet
    // output) carry status too — only op-level events may move current_op,
    // otherwise "capture done" would re-arm every control mid-take.
    const opLevel = !m.step && m.line === undefined && m.status !== 'census';
    if (opLevel && m.status === 'start') state.current_op = m.op;
    if (opLevel && (m.status === 'done' || m.status === 'error')
        && state.current_op === m.op) {
      state.current_op = null;
    }
    routeToOverlays(m);
    render();
  });
  es.addEventListener('sessions', (e) => {
    const d = JSON.parse(e.data);
    state.sessions = Array.isArray(d) ? d : (d.sessions || []);
    render();
  });
  es.onopen = () => { ui.sseUp = true; renderTopbar(); };
  es.onerror = () => {
    ui.sseUp = false;
    renderTopbar();
    try { es.close(); } catch (e2) { /* noop */ }
    setTimeout(connectSSE, 3000);   // auto-reconnect
  };
}

function routeToOverlays(msg) {
  for (const o of overlayStack) {
    if (o.onOp) o.onOp(msg);
  }
}

/* ─── render ────────────────────────────────────────────────────────────── */

function render() {
  renderTopbar();
  renderRig();
  renderTrigger();
  renderExposure();
  renderSession();
  renderStore();
  renderSessions();
  renderTicker();
}

function renderTopbar() {
  const v = state.verdict;
  const block = $('verdict-block');
  const st = v ? v.state : null;
  block.className = st === 'GO' ? 'v-go'
    : st === 'DEGRADED' ? 'v-degraded'
    : st === 'NO-GO' ? 'v-nogo' : 'v-unknown';
  $('verdict-state').textContent = st || '····';
  let reason = (v && v.reasons && v.reasons.length) ? v.reasons.join(' · ') : '';
  if (!reason && v && (st === 'GO' || st === 'DEGRADED')) {
    // All-clear summary, mockup-style: 32/32 · cam ✓ · NTP ≤1.8 ms · SMB ✓ · ~214 takes
    const c = v.counts || {};
    const pis = (state.fleet && state.fleet.pis) || [];
    let maxOff = 0, minFree = Infinity;
    for (const p of pis) {
      const o = p.ntp && Math.abs(p.ntp.offset_ms);
      if (typeof o === 'number' && o > maxOff) maxOff = o;
      if (typeof p.free_mb === 'number' && p.free_mb < minFree) minFree = p.free_mb;
    }
    const parts = [(c.replied || pis.length) + '/' + (c.expected || expectedPis()),
      'cam ✓', 'NTP ≤' + maxOff.toFixed(1) + ' ms', 'SMB ✓'];
    if (isFinite(minFree)) parts.push('~' + Math.floor(minFree / 4) + ' takes');
    reason = parts.join(' · ');
  }
  $('verdict-reason').textContent = reason;

  // sim badge — backend may flag sim mode at top level or in config
  const sim = !!(state.sim || (state.config &&
    (state.config.sim || state.config.sim_mode)));
  $('sim-badge').hidden = !sim;

  $('link-dot').classList.toggle('down', !ui.sseUp);
  const mb = $('mute-btn');
  mb.textContent = audio.muted ? '♪ OFF' : '♪ ON';
  mb.classList.toggle('muted', audio.muted);
}

// Worst-first per-pi problems, short labels for the status line.
function piProblems(p) {
  const out = [];
  const ntp = p.ntp || {};
  const smb = p.smb || {};
  const off = (typeof ntp.offset_ms === 'number') ? Math.abs(ntp.offset_ms) : 0;
  if (p.camera_ok === false) out.push('CAM✗');
  if (ntp.synced === false) out.push('NTP✗');
  else if (off > 5.0) out.push((ntp.offset_ms > 0 ? '+' : '') + ntp.offset_ms.toFixed(1) + 'MS');
  if (smb.reachable === false) out.push('SMB✗');
  if (typeof p.free_mb === 'number' && p.free_mb < 100) out.push('DISK ' + p.free_mb + 'MB');
  if (p.stale) out.push('v' + (p.version || '?'));
  if (!out.length && off > cfgNum('go_max_offset_ms', 2.5)) {
    out.push((ntp.offset_ms > 0 ? '+' : '') + ntp.offset_ms.toFixed(1) + 'MS');
  }
  return out;
}

function piClass(p) {
  const ntp = p.ntp || {};
  const smb = p.smb || {};
  const off = (typeof ntp.offset_ms === 'number') ? Math.abs(ntp.offset_ms) : 0;
  if (p.camera_ok === false || ntp.synced === false || off > 5.0 ||
      smb.reachable === false ||
      (typeof p.free_mb === 'number' && p.free_mb < 100)) return 'fail';
  if (p.stale || off > cfgNum('go_max_offset_ms', 2.5)) return 'warn';
  return 'ok';
}

// Index-ordered union of seen + replied pis, padded to expected.
function rigList() {
  const seen = (state.config && Array.isArray(state.config.seen_pis))
    ? state.config.seen_pis : [];
  const replied = new Map();
  if (state.fleet && Array.isArray(state.fleet.pis)) {
    for (const p of state.fleet.pis) replied.set(p.pi, p);
  }
  const ids = [];
  for (const id of seen.concat(Array.from(replied.keys()))) {
    if (ids.indexOf(id) < 0) ids.push(id);
  }
  ids.sort();
  const out = ids.map((id) => ({ id: id, p: replied.get(id) || null }));
  while (out.length < expectedPis()) out.push({ id: null, p: null });
  return out;
}

// Squares are created once and mutated in place: a full rebuild every sweep
// would swallow any tap that straddles the 5 s re-render (mousedown on the old
// node, mouseup on its replacement → no click). Handlers resolve the pi lazily.
let rigCache = [];

function renderRig() {
  const grid = $('rig-grid');
  const list = rigList();
  rigCache = list;
  const offenders = [];
  let okCount = 0;

  if (grid.childElementCount !== list.length) {
    grid.textContent = '';
    for (let i = 0; i < list.length; i++) {
      const sq = h('button', { class: 'sq unknown', type: 'button' });
      sq.addEventListener('click', () => {
        const e = rigCache[i];
        if (e && e.p) openPiDetail(e.p);
      });
      grid.append(sq);
    }
  }

  for (let i = 0; i < list.length; i++) {
    const entry = list[i];
    let cls, title;
    if (!entry.id) {
      cls = 'unknown';
      title = 'never seen';
    } else if (!entry.p) {
      cls = 'silent';
      title = entry.id + ' · SILENT';
      offenders.push(entry.id + ' SILENT');
    } else {
      cls = piClass(entry.p);
      const probs = piProblems(entry.p);
      title = entry.id + (probs.length ? ' · ' + probs.join(' ') : ' · ok');
      if (cls === 'ok') okCount++;
      else offenders.push(entry.id + ' ' + probs.join(' '));
    }
    const sq = grid.children[i];
    sq.className = 'sq ' + cls;
    sq.title = title;
  }

  const n = state.fleet && state.fleet.pis ? state.fleet.pis.length : 0;
  const exp = expectedPis();
  let line = n + '/' + exp + ' replied';
  if (state.fleet && state.fleet.checked_at) {
    line += ' · swept ' + fmtClock(state.fleet.checked_at);
  }
  if (offenders.length) line += ' · ' + offenders.slice(0, 3).join(' · ');
  else if (n === exp && n > 0) line = exp + '/' + exp + ' OK · swept '
    + (state.fleet.checked_at ? fmtClock(state.fleet.checked_at) : '—');
  $('rig-status').textContent = line;
}

function fireArmed() {
  const st = state.verdict && state.verdict.state;
  if (st === 'GO' || st === 'DEGRADED') return true;
  if (st === 'NO-GO' && ui.override) return true;
  return false;
}

function renderTrigger() {
  const btn = $('btn-fire');
  const st = state.verdict && state.verdict.state;
  const busy = !!state.current_op;
  btn.disabled = !fireArmed() || busy;
  btn.classList.toggle('nogo', st === 'NO-GO' && !ui.override);
  btn.classList.toggle('override', st === 'NO-GO' && ui.override);
  btn.textContent = busy ? String(state.current_op).toUpperCase() + '…' : 'FIRE';

  $('override-row').hidden = st !== 'NO-GO';
  $('out-lead').textContent = ui.lead.toFixed(1);

  const sp = $('spread-read');
  if (ui.lastSpread) {
    sp.textContent = ui.lastSpread.ms.toFixed(1) + ' MS / '
      + cfgNum('spread_budget_ms', 5.0).toFixed(1) + ' BUDGET '
      + (ui.lastSpread.ok ? '✓' : '✗');
    sp.style.color = ui.lastSpread.ok ? 'var(--ok)' : 'var(--fail)';
  } else {
    sp.textContent = '— / ' + cfgNum('spread_budget_ms', 5.0).toFixed(1) + ' BUDGET';
    sp.style.color = '';
  }
}

function renderExposure() {
  $('out-exp').textContent = String(Math.round(ui.form.exp));
  $('out-gain').textContent = ui.form.gain.toFixed(1);
  $('out-awbr').textContent = ui.form.awbr.toFixed(2);
  $('out-awbb').textContent = ui.form.awbb.toFixed(2);
  $('out-q').textContent = String(Math.round(ui.form.q));
  $('chk-motion').checked = ui.motionSafe;

  const stat = $('apply-status');
  if (ui.lastApply) {
    stat.textContent = 'applied ' + fmtClock(ui.lastApply.at) + ' · '
      + (ui.lastApply.acks === null ? '?' : ui.lastApply.acks) + '/'
      + expectedPis() + ' ack';
  } else if (state.config && state.config.last_configure_at) {
    stat.textContent = 'last applied ' + fmtClock(state.config.last_configure_at);
  } else {
    stat.textContent = 'not applied this session';
  }

  // rebuild preset select only when the name list changed
  const sel = $('sel-preset');
  const names = (state.config && state.config.presets)
    ? Object.keys(state.config.presets).sort() : [];
  const sig = names.join('|');
  if (sel.dataset.sig !== sig) {
    const prev = sel.value;
    sel.textContent = '';
    sel.append(h('option', { value: '', text: '—' }));
    for (const nm of names) sel.append(h('option', { value: nm, text: nm }));
    sel.value = names.indexOf(prev) >= 0 ? prev : '';
    sel.dataset.sig = sig;
  }
  const busy = !!state.current_op;
  $('btn-apply').disabled = busy;
  $('btn-meter').disabled = busy;
}

// Rebuild-only-on-change signatures: these lists re-render every sweep (~5 s),
// and replacing the nodes mid-tap swallows the click (same failure the rig
// grid had). Skip the teardown when nothing visible changed.
let sessionSig = '';
let sessionsSig = '';

function renderSession() {
  const box = $('subject-chips');
  const subs = subjects();
  const sig = JSON.stringify([subs, ui.subject, nextSessionName(ui.subject),
    !!state.current_op,
    state.sessions.map((s) => [s.session, s.verified, s.test])]);
  if (sig === sessionSig) return;
  sessionSig = sig;
  box.textContent = '';
  if (!subs.length) {
    box.append(h('span', { class: 'statusline', text: 'no subjects — add one' }));
  }
  for (const s of subs) {
    const chip = h('button', {
      class: 'chip' + (s === ui.subject ? ' sel' : ''),
      type: 'button',
      text: s,
      onclick: () => { ui.subject = s; render(); },
    });
    box.append(chip);
  }
  const nn = nextSessionName(ui.subject);
  $('next-name').textContent = nn || '— pick a subject —';
  $('btn-del-subject').disabled = !ui.subject || !!state.current_op;

  // today's tally — real takes only, test frames don't count
  const today = todayStr();
  let takes = 0, good = 0, bad = 0;
  for (const s of state.sessions) {
    if (s.test || !s.session || s.session.indexOf(today) !== 0) continue;
    takes++;
    if (s.verified) good++; else bad++;
  }
  $('day-line').textContent = takes
    ? takes + (takes === 1 ? ' take · ' : ' takes · ') + good + ' ✓ · ' + bad + ' ✗'
    : 'no takes yet';
}

function renderStore() {
  const pis = (state.fleet && state.fleet.pis) || [];
  const n = pis.length;

  // DEST — mode server/share across the fleet + reachability count
  let server = null, share = '';
  let reach = 0;
  for (const p of pis) {
    const smb = p.smb || {};
    if (!server && smb.server) { server = smb.server; share = smb.share || ''; }
    if (smb.reachable) reach++;
  }
  $('dest-val').textContent = server ? '//' + server + '/' + share : '— not set —';
  const dc = $('dest-count');
  dc.textContent = n ? reach + '/' + n + ' reach' : '';
  dc.className = 'cnt ' + (n && reach === n ? 'good' : (n ? 'bad' : ''));

  // NTP
  let ntpServer = null, synced = 0;
  for (const p of pis) {
    const ntp = p.ntp || {};
    if (!ntpServer && ntp.server) ntpServer = ntp.server;
    if (ntp.synced) synced++;
  }
  $('ntp-val').textContent = ntpServer || '— not set —';
  const nc = $('ntp-count');
  nc.textContent = n ? synced + '/' + n + ' synced' : '';
  nc.className = 'cnt ' + (n && synced === n ? 'good' : (n ? 'bad' : ''));

  // FREE — fleet min free_mb → rough take capacity (≈4 MB per full-res frame)
  let minFree = null;
  for (const p of pis) {
    if (typeof p.free_mb === 'number') {
      minFree = (minFree === null) ? p.free_mb : Math.min(minFree, p.free_mb);
    }
  }
  let freeTxt = '—';
  if (minFree !== null) {
    freeTxt = 'min ' + minFree + ' MB ≈ ' + Math.floor(minFree / 4) + ' takes';
  }
  const counts = (state.verdict && state.verdict.counts) || {};
  const shareFree = counts.share_free_mb || counts.laptop_free_mb || counts.scans_free_mb;
  if (typeof shareFree === 'number') {
    freeTxt += ' · share ' + shareFree + ' MB';
  }
  $('free-val').textContent = freeTxt;

  const busy = !!state.current_op;
  $('btn-update').disabled = busy;
  $('btn-preflight').disabled = busy;
  $('btn-clearall').disabled = busy;
  $('btn-diag-pis').disabled = busy;
  $('btn-diag-smb').disabled = busy;
  $('btn-reboot').disabled = busy;
  $('btn-halt').disabled = busy;
}

function renderSessions() {
  const box = $('session-list');
  const list = state.sessions.slice(0, 8);
  const sig = JSON.stringify(list.map((s) =>
    [s.session, s.files, s.verified, s.spread_ms]));
  if (sig === sessionsSig) return;
  sessionsSig = sig;
  box.textContent = '';
  if (!list.length) {
    box.append(h('div', { class: 'statusline', text: 'no sessions yet' }));
    return;
  }
  for (const s of list) {
    const files = Array.isArray(s.files) ? s.files.length : (s.files || 0);
    const exp = (typeof s.expected === 'number') ? s.expected : expectedPis();
    const row = h('button', { class: 'sess-row', type: 'button' },
      h('span', { class: 'name', text: s.session }),
      h('span', { class: 'dim', text: files + '/' + exp }),
      h('span', {
        class: s.verified ? 'mk-ok' : 'mk-bad',
        text: s.verified ? '✓' : '✗',
      }),
      h('span', {
        class: 'dim',
        text: (typeof s.spread_ms === 'number') ? s.spread_ms.toFixed(1) + 'ms' : '—',
      }));
    row.addEventListener('click', () => openSessionDetail(s));
    box.append(row);
  }
}

function renderTicker() {
  const ol = $('ticker-list');
  ol.textContent = '';
  const recent = state.ticker.slice(-5).reverse();   // newest on top
  for (const t of recent) ol.append(tickerLine(t));
}

function tickerLine(t) {
  return h('li', { class: 'tk-' + (t.level || 'info') },
    h('span', { class: 'tk-ts', text: fmtClock(t.ts) }),
    document.createTextNode(t.text || ''));
}

/* ─── overlays ──────────────────────────────────────────────────────────── */

const overlayStack = [];   // [{name, el, onClose?, onOp?}]

function openOverlay(o) {
  overlayStack.push(o);
  $('overlay-stack').append(o.el);
  $('overlay-root').hidden = false;
}

function closeOverlay() {
  const o = overlayStack.pop();
  if (!o) return;
  if (o.onClose) o.onClose();
  o.el.remove();
  if (!overlayStack.length) $('overlay-root').hidden = true;
}

function closeAllOverlays() {
  while (overlayStack.length) closeOverlay();
}

// Close only the named overlay (wherever it sits in the stack) — take
// resolution must not tear down unrelated overlays the user opened meanwhile.
function closeOverlayByName(name) {
  const i = overlayStack.findIndex((o) => o.name === name);
  if (i < 0) return;
  const o = overlayStack.splice(i, 1)[0];
  if (o.onClose) o.onClose();
  o.el.remove();
  if (!overlayStack.length) $('overlay-root').hidden = true;
}

// Standard overlay chrome: title bar + close + body.
function panel(title) {
  const body = h('div', { class: 'ov-body' });
  for (let i = 1; i < arguments.length; i++) body.append(arguments[i]);
  const el = h('div', { class: 'ov-panel' },
    h('div', { class: 'ov-title' },
      h('span', { text: title }),
      h('button', { class: 'ov-close', type: 'button', text: 'ESC ×', onclick: closeOverlay })),
    body);
  return el;
}

/* ── pi detail ── */

function openPiDetail(p) {
  const ntp = p.ntp || {};
  const smb = p.smb || {};
  const g = h('div', { class: 'pi-grid' });
  const row = (k, v, cls) => {
    g.append(h('span', { class: 'k', text: k }),
             h('span', { class: cls || '', text: v }));
  };
  row('CAMERA', p.camera_ok ? '✓ ok' : '✗ FAIL', p.camera_ok ? 'good' : 'bad');
  row('CLOCK OFFSET', (typeof p.clock_offset_ms === 'number')
    ? p.clock_offset_ms.toFixed(2) + ' ms' : '—');
  row('NTP', (ntp.synced ? '✓ synced' : '✗ UNSYNCED') + ' · ' + (ntp.server || '?'),
      ntp.synced ? 'good' : 'bad');
  row('NTP OFFSET', (typeof ntp.offset_ms === 'number')
    ? ((ntp.offset_ms > 0 ? '+' : '') + ntp.offset_ms.toFixed(2) + ' ms') : '—',
      Math.abs(ntp.offset_ms || 0) > 5 ? 'bad' : '');
  row('STRATUM', String(ntp.stratum !== undefined ? ntp.stratum : '—'));
  row('SMB', (smb.reachable ? '✓ ' : '✗ ')
    + '//' + (smb.server || '—') + '/' + (smb.share || ''),
      smb.reachable ? 'good' : 'bad');
  if (smb.last_error) row('SMB ERROR', smb.last_error, 'bad');
  if (typeof smb.last_check_age_s === 'number') {
    row('SMB CHECKED', smb.last_check_age_s + ' s ago');
  }
  row('CREDENTIALS', smb.credentials_ref || '—');
  row('FREE', (p.free_mb !== undefined ? p.free_mb + ' MB' : '—'),
      (p.free_mb < 100) ? 'bad' : '');
  row('UPTIME', (typeof p.uptime_s === 'number')
    ? Math.floor(p.uptime_s / 3600) + 'h ' + Math.floor((p.uptime_s % 3600) / 60) + 'm'
    : '—');
  row('VERSION', (p.version || '?') + (p.stale ? ' · STALE' : ''), p.stale ? 'bad' : '');
  openOverlay({ name: 'pi', el: panel('PI · ' + p.pi, g) });
}

/* ── fire / countdown / take result ── */

async function fire(test) {
  if (state.current_op) {
    localTicker('warn', 'BUSY · ' + String(state.current_op).toUpperCase()
      + ' in progress — fire skipped');
    return;
  }
  if (!fireArmed()) {
    localTicker('warn', 'NOT ARMED · verdict '
      + ((state.verdict && state.verdict.state) || '?')
      + ' — tick OVERRIDE to fire anyway');
    return;
  }
  const subject = test ? 'test' : sanitizeSubject(ui.subject);
  if (!subject) {
    localTicker('warn', 'NO SUBJECT — add one in SESSION first');
    return;
  }
  audio.ensure();
  ui.lastFire = { subject: subject, test: !!test };
  const body = {
    subject: subject,
    leadtime_s: ui.lead,
    test: !!test,
    override: !!(state.verdict && state.verdict.state === 'NO-GO' && ui.override),
  };
  openCountdown(ui.lead, subject, !!test);
  try {
    const report = await api('/api/take', body);
    closeOverlayByName('countdown');
    if (typeof report.spread_ms === 'number') {
      ui.lastSpread = { ms: report.spread_ms, ok: !!report.spread_ok };
    }
    openTakeResult(report);
    render();
  } catch (err) {
    closeOverlayByName('countdown');
    if (err.status === 409) {
      const d = err.data || {};
      if (d.error === 'busy') {
        localTicker('warn', 'BUSY · ' + String(d.op || 'op').toUpperCase()
          + ' in progress — fire skipped');
      } else {
        localTicker('fail', 'TAKE BLOCKED · '
          + ((d.reasons && d.reasons.length) ? d.reasons.join(' · ')
            : (d.error || 'NO-GO')));
      }
    } else {
      localTicker('fail', 'TAKE failed · ' + err.message);
    }
    render();
  }
}

function openCountdown(lead, subject, test) {
  // The server runs a gating PING sweep (up to 2.5 s) BEFORE computing the
  // trigger time, so a countdown started at click time would beep T0 early and
  // the subject would relax before the real shutter. Show ARMING until the
  // backend's capture-run event delivers the authoritative remaining lead,
  // then count against that deadline.
  const num = h('div', { class: 'count-num', text: '·' });
  const sub = h('div', {
    class: 'count-sub',
    text: (test ? 'TEST FRAME' : subject) + ' · ARMING — sweeping rig…',
  });
  const steps = {};
  const prog = h('div', { class: 'take-prog', hidden: '' });
  for (const nm of ['capture', 'upload', 'verify']) {
    const st = h('span', { class: 'tp-st', text: '—' });
    steps[nm] = st;
    prog.append(h('div', { class: 'tp-row' },
      h('span', { class: 'tp-name', text: nm.toUpperCase() }), st));
  }
  const box = h('div', { class: 'count-box' }, num, sub, prog);

  let iv = null;
  let lastShown = null;
  const startCount = (remainingS) => {
    if (iv) return;
    const deadline = performance.now() + remainingS * 1000;
    sub.textContent = (test ? 'TEST FRAME' : subject)
      + ' · T−' + remainingS.toFixed(1) + ' S';
    const step = () => {
      const left = deadline - performance.now();
      if (left <= 0) {
        clearInterval(iv);
        num.textContent = '0';
        num.classList.add('fired');
        audio.beep(440, 400);          // long beep at T0 = the real shutter
        sub.textContent = 'FIRED · collecting…';
        prog.hidden = false;
        return;
      }
      const n = Math.ceil(left / 1000);
      if (n !== lastShown) {
        lastShown = n;
        num.textContent = String(n);
        audio.beep(880, 80);
      }
    };
    step();
    iv = setInterval(step, 100);
  };

  openOverlay({
    name: 'countdown',
    el: panel(test ? 'TEST FRAME' : 'TAKE · ' + subject, box),
    onClose: () => { if (iv) clearInterval(iv); },
    onOp: (m) => {
      if (m.op !== 'take' || !m.step) return;
      if (m.step === 'capture' && m.status === 'run'
          && typeof m.lead_remaining_s === 'number') {
        startCount(m.lead_remaining_s);
      }
      const st = steps[m.step];
      if (!st) return;
      let txt = String(m.status || 'run').toUpperCase();
      if (m.detail) txt += ' · ' + m.detail;
      else {
        const bits = [];
        for (const k of ['captured', 'uploaded', 'verified', 'ok', 'missing', 'errors']) {
          if (m[k] !== undefined && typeof m[k] !== 'object') bits.push(k + '=' + m[k]);
        }
        if (bits.length) txt += ' · ' + bits.join(' ');
      }
      st.textContent = txt;
    },
  });
}

function spreadStrip(report) {
  const budget = cfgNum('spread_budget_ms', 5.0);
  const spread = (typeof report.spread_ms === 'number') ? report.spread_ms : 0;
  const axisMax = Math.max(budget * 1.5, spread * 1.15, 1);
  const pct = (ms) => Math.min(100, (ms / axisMax) * 100);

  const strip = h('div', { class: 'spread-strip' });
  strip.append(h('div', { class: 'spread-budget', style: 'width:' + pct(budget) + '%' }));
  strip.append(h('span', {
    class: 'spread-lbl', style: 'left:' + pct(budget) + '%;margin-left:4px',
    text: 'BUDGET ' + budget.toFixed(1),
  }));

  // per-pi dots if the report carries them (dt from earliest, ms); else a bar
  const perPi = report.actuals || report.per_pi_ms || report.offsets_ms || null;
  if (perPi && typeof perPi === 'object') {
    const entries = Object.keys(perPi).map((pi) => ({ pi: pi, ms: perPi[pi] }));
    if (entries.length) {
      const minMs = Math.min.apply(null, entries.map((e) => e.ms));
      let laggard = entries[0];
      for (const e of entries) {
        e.dt = e.ms - minMs;
        if (e.dt > laggard.dt) laggard = e;
      }
      for (const e of entries) {
        strip.append(h('div', {
          class: 'spread-dot' + (e === laggard && e.dt > budget ? ' laggard' : ''),
          style: 'left:' + pct(e.dt) + '%',
          title: e.pi + ' +' + e.dt.toFixed(2) + 'ms',
        }));
      }
      strip.append(h('span', {
        class: 'spread-lbl laggard',
        style: 'left:' + Math.min(80, pct(laggard.dt)) + '%;margin-left:6px',
        text: laggard.pi + ' +' + laggard.dt.toFixed(1),
      }));
      return strip;
    }
  }
  strip.append(h('div', { class: 'spread-bar', style: 'left:0;width:' + pct(spread) + '%' }));
  strip.append(h('span', {
    class: 'spread-lbl', style: 'left:2px', text: 'SPREAD ' + spread.toFixed(1) + ' MS',
  }));
  return strip;
}

function openTakeResult(report) {
  const exp = expectedPis();
  const cap = report.captured || 0;
  const spreadOk = !!report.spread_ok;
  const allOk = report.triage === 'ok';

  const verdictLine = h('div', {
    class: 'tr-verdict ' + (allOk ? 'ok' : 'bad'),
    text: cap + '/' + exp + ' · SPREAD '
      + ((typeof report.spread_ms === 'number') ? report.spread_ms.toFixed(1) : '—')
      + ' MS ' + (spreadOk ? '✓' : '✗'),
  });

  const lines = [];
  if (report.missing && report.missing.length) {
    lines.push(h('div', { class: 'tr-line' },
      h('span', { class: 'bad', text: 'MISSING ' }),
      document.createTextNode(report.missing.join(' '))));
  }
  if (report.capture_errors && report.capture_errors.length) {
    lines.push(h('div', { class: 'tr-line' },
      h('span', { class: 'bad', text: 'CAPTURE ERRORS ' }),
      document.createTextNode(report.capture_errors
        .map((e) => e.pi + ':' + e.reason).join(' '))));
  }
  lines.push(h('div', { class: 'tr-line' },
    h('span', { class: 'dim', text: 'UPLOADED ' }),
    document.createTextNode((report.uploaded || 0) + '/' + cap
      + ((report.upload_errors && report.upload_errors.length)
        ? ' · errors: ' + report.upload_errors
            .map((e) => (e.pi || e) + (e.reason ? ':' + e.reason : '')).join(' ')
        : ''))));
  const vf = report.verify || {};
  lines.push(h('div', { class: 'tr-line' },
    h('span', { class: 'dim', text: 'VERIFY ' }),
    document.createTextNode(
      (report.verified ? '✓ all files on share' : '✗ incomplete')
      + ((vf.missing && vf.missing.length) ? ' · missing ' + vf.missing.join(' ') : '')
      + ((vf.bad && vf.bad.length) ? ' · bad ' + vf.bad.join(' ') : ''))));
  lines.push(h('div', { class: 'tr-line' },
    h('span', { class: 'dim', text: 'TRIAGE ' }),
    document.createTextNode(String(report.triage || '?').toUpperCase())));

  // Distinct pis needing re-upload — verify.missing and upload_errors usually
  // name the same nodes, so a plain sum would double-count.
  const retrySet = {};
  (vf.missing || []).forEach((pi) => { retrySet[pi] = 1; });
  (vf.bad || []).forEach((pi) => { retrySet[pi] = 1; });
  (report.upload_errors || []).forEach((e) => { retrySet[e.pi || e] = 1; });
  const nRetry = Object.keys(retrySet).length;

  const btns = h('div', { class: 'btnrow' });
  if (report.triage === 'retry_upload' || nRetry > 0) {
    btns.append(h('button', {
      class: 'act', type: 'button',
      text: 'RETRY UPLOAD' + (nRetry ? ' (' + nRetry + ')' : ''),
      onclick: async (e) => {
        e.target.disabled = true;
        try {
          const updated = await api('/api/upload-retry', { session: report.session });
          closeOverlay();
          openTakeResult(Object.assign({}, report, updated));
        } catch (err) { e.target.disabled = false; apiFail(err, 'RETRY UPLOAD'); }
      },
    }));
  }
  btns.append(h('button', {
    class: 'act', type: 'button', text: 'RETAKE',
    onclick: () => {
      closeAllOverlays();
      if (ui.lastFire) fire(ui.lastFire.test);
    },
  }));
  const clearBtn = h('button', {
    class: 'danger', type: 'button', text: 'CLEAR ON PIS',
    onclick: async (e) => {
      e.target.disabled = true;
      try {
        await api('/api/clear', { session: report.session });
        e.target.textContent = 'CLEARED';
      } catch (err) { e.target.disabled = false; apiFail(err, 'CLEAR'); }
    },
  });
  if (!report.verified) clearBtn.disabled = true;
  clearBtn.title = report.verified ? '' : 'gated until verified';
  btns.append(clearBtn);
  btns.append(h('button', {
    type: 'button', text: 'REVIEW',
    onclick: () => openReview({
      session: report.session,
      expected: exp,
      verified: report.verified,
      missing: report.missing,
    }),
  }));
  btns.append(h('button', { type: 'button', text: 'CLOSE', onclick: closeOverlay }));

  const body = [verdictLine, spreadStrip(report)].concat(lines, [btns]);
  const el = panel.apply(null, ['TAKE RESULT · ' + (report.session || '?')].concat(body));
  openOverlay({ name: 'take-result', el: el });
}

/* ── review / contact sheet / lightbox ── */

function sheetPiIds(entry) {
  if (Array.isArray(entry.pis)) return entry.pis.slice().sort();
  if (Array.isArray(entry.files)) {
    return entry.files.map((f) => String(f).replace(/\.jpg$/i, '')).sort();
  }
  // fall back to known fleet ids, index-ordered
  return rigList().map((e) => e.id).filter((id) => !!id);
}

async function getJSON(path) {
  const res = await fetch(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || ('HTTP ' + res.status));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/* ── session detail (manifest facts + management actions) ── */

async function openSessionDetail(entry) {
  let manifest = null;
  try {
    const d = await getJSON('/api/session/' + encodeURIComponent(entry.session));
    if (d.summary) entry = Object.assign({}, entry, d.summary);
    manifest = d.manifest;
  } catch (err) {
    localTicker('warn', 'SESSION detail unavailable · ' + err.message);
  }
  const m = manifest || {};
  const facts = h('div', { class: 'pi-grid' });
  const row = (k, v, cls) => {
    facts.append(h('span', { class: 'k', text: k }),
      h('span', { class: cls || '', text: v }));
  };
  const exp = (typeof entry.expected === 'number') ? entry.expected : expectedPis();
  row('CREATED', entry.created_at ? fmtClock(entry.created_at) : '—');
  row('SUBJECT', (entry.subject || '—')
    + (entry.take ? ' · take ' + entry.take : '')
    + (entry.test ? ' · TEST' : ''));
  const st = m.settings;
  row('SETTINGS', st
    ? st.exposure_us + ' µs · g' + st.analogue_gain + ' · awb ['
      + (st.awb_gains || []).join(', ') + '] · Q' + st.jpeg_quality
    : '—');
  row('CAPTURED', (m.captured ? m.captured.length : '?') + '/' + exp
    + ((m.missing && m.missing.length) ? ' · missing ' + m.missing.join(' ') : '')
    + (m.shortfall ? ' · shortfall ' + m.shortfall : ''));
  row('SPREAD', (typeof entry.spread_ms === 'number')
    ? entry.spread_ms.toFixed(1) + ' ms ' + (m.spread_ok ? '✓' : '✗')
    : '—', m.spread_ok ? 'good' : '');
  row('FILES', entry.files + '/' + exp + ' on share');
  row('VERIFIED', entry.verified ? '✓ yes' : '✗ no',
    entry.verified ? 'good' : 'bad');
  row('PI COPIES', entry.cleared_on_pis
    ? 'cleared — share holds the ONLY copy' : 'still on the Pis',
    entry.cleared_on_pis ? 'bad' : 'good');
  if (entry.triage && entry.triage !== 'ok') {
    row('TRIAGE', String(entry.triage).toUpperCase());
  }

  const btns = h('div', { class: 'btnrow' },
    h('button', {
      class: 'act', type: 'button', text: 'CONTACT SHEET',
      onclick: () => openReview(entry),
    }));
  if (!entry.verified) {
    btns.append(h('button', {
      type: 'button', text: 'RETRY UPLOAD',
      onclick: async (e) => {
        e.target.disabled = true;
        try {
          await api('/api/upload-retry', { session: entry.session });
          closeOverlay();
          openSessionDetail({ session: entry.session });
        } catch (err) { e.target.disabled = false; apiFail(err, 'RETRY UPLOAD'); }
      },
    }));
  }
  if (entry.verified && !entry.cleared_on_pis) {
    btns.append(h('button', {
      class: 'danger', type: 'button', text: 'CLEAR ON PIS',
      onclick: async (e) => {
        e.target.disabled = true;
        try {
          await api('/api/clear', { session: entry.session });
          closeOverlay();
          openSessionDetail({ session: entry.session });
        } catch (err) { e.target.disabled = false; apiFail(err, 'CLEAR'); }
      },
    }));
  }
  btns.append(
    h('button', {
      class: 'danger', type: 'button', text: 'DELETE FROM SHARE',
      onclick: () => openSessionDelete(entry),
    }),
    h('button', { type: 'button', text: 'CLOSE', onclick: closeOverlay }));

  openOverlay({
    name: 'session-detail',
    el: panel('SESSION · ' + entry.session, facts, btns),
  });
}

function openSessionDelete(entry) {
  // Mirrors the backend rule: once the Pi copies are cleared (or provenance is
  // unknown — no manifest), the share holds the only copy and the confirm word
  // escalates from DELETE to the full session name.
  const onlyCopy = !!entry.cleared_on_pis || entry.has_manifest === false;
  const required = onlyCopy ? entry.session : 'DELETE';
  const warn = onlyCopy
    ? 'the Pi-side copies are gone — this is the ONLY copy of these images. '
      + 'deleting is permanent. type the full session name to arm.'
    : 'removes this session from the laptop share only; the Pis still hold '
      + 'their copies (re-uploadable). type DELETE to arm.';
  const inp = h('input', { type: 'text', placeholder: 'type ' + required, autocomplete: 'off' });
  const result = h('div', { class: 'ov-result' });
  const btn = h('button', {
    class: 'danger', type: 'button', text: 'DELETE ' + entry.session, disabled: '',
    onclick: async () => {
      btn.disabled = true;
      try {
        const r = await api('/api/session-delete',
          { session: entry.session, confirm: inp.value });
        closeAllOverlays();     // detail overlay underneath is now stale too
        localTicker('warn', 'DELETED ' + entry.session + ' · '
          + r.files_removed + ' files');
      } catch (err) {
        result.className = 'ov-result fail';
        result.textContent = 'delete failed · ' + err.message;
        btn.disabled = inp.value !== required;
      }
    },
  });
  inp.addEventListener('input', () => { btn.disabled = inp.value !== required; });
  openOverlay({
    name: 'session-delete',
    el: panel('DELETE · ' + entry.session,
      h('div', { class: 'ov-note', text: warn }),
      inp, result,
      h('div', { class: 'btnrow' }, btn,
        h('button', { type: 'button', text: 'CANCEL', onclick: closeOverlay }))),
  });
  inp.focus();
}

function openReview(entry) {
  const exp = (typeof entry.expected === 'number') ? entry.expected : expectedPis();
  const ids = sheetPiIds(entry).slice(0, Math.max(exp, 1));
  const missing = new Set(entry.missing || []);
  const sheet = h('div', { class: 'sheet' });

  for (const pi of ids) {
    const cell = h('button', { class: 'cell', type: 'button', title: pi });
    if (missing.has(pi)) {
      cell.classList.add('missing');
      cell.append(h('span', { class: 'fb', text: pi }));
      cell.disabled = true;
    } else {
      const img = h('img', {
        src: '/api/thumb/' + encodeURIComponent(entry.session) + '/'
          + encodeURIComponent(pi) + '.jpg',
        alt: pi, loading: 'lazy',
      });
      // decode failure or 404 → dark fallback tile with the pi id
      img.addEventListener('error', () => {
        img.remove();
        cell.append(h('span', { class: 'fb', text: pi }));
      });
      cell.append(img);
      cell.addEventListener('click', () => openLightbox(entry.session, pi));
    }
    sheet.append(cell);
  }
  // pad to expected with red hairline empty slots
  for (let i = ids.length; i < exp; i++) {
    sheet.append(h('div', { class: 'cell missing' }, h('span', { class: 'fb', text: '—' })));
  }

  const btns = h('div', { class: 'btnrow' },
    h('button', {
      class: 'act', type: 'button', text: 'RETRY UPLOAD',
      onclick: async (e) => {
        e.target.disabled = true;
        try {
          await api('/api/upload-retry', { session: entry.session });
          localTicker('info', 'UPLOAD retry queued · ' + entry.session);
          e.target.disabled = false;
        } catch (err) { e.target.disabled = false; apiFail(err, 'RETRY UPLOAD'); }
      },
    }),
    (() => {
      const b = h('button', {
        class: 'danger', type: 'button', text: 'CLEAR ON PIS',
        onclick: async (e) => {
          e.target.disabled = true;
          try {
            await api('/api/clear', { session: entry.session });
            e.target.textContent = 'CLEARED';
          } catch (err) { e.target.disabled = false; apiFail(err, 'CLEAR'); }
        },
      });
      if (!entry.verified) { b.disabled = true; b.title = 'gated until verified'; }
      return b;
    })(),
    h('button', { type: 'button', text: 'CLOSE', onclick: closeOverlay }));

  openOverlay({
    name: 'review',
    el: panel('REVIEW · ' + entry.session, sheet, btns),
  });
}

function openLightbox(session, pi) {
  const img = h('img', {
    src: '/api/image/' + encodeURIComponent(session) + '/' + encodeURIComponent(pi) + '.jpg',
    alt: pi,
  });
  img.addEventListener('error', () => {
    img.replaceWith(h('div', { class: 'ov-note', text: 'image did not decode — ' + pi }));
  });
  const box = h('div', { class: 'lightbox', onclick: closeOverlay },
    img, h('div', { class: 'cap', text: session + ' / ' + pi + '.jpg' }));
  openOverlay({ name: 'lightbox', el: panel(pi.toUpperCase(), box) });
}

/* ── autoconfigure ── */

function openAutoconfigure() {
  const status = h('div', { class: 'ov-note', text: 'metering all cameras on auto (2.0 s settle)…' });
  const grid = h('div', { class: 'meter-grid' });
  const btns = h('div', { class: 'btnrow' },
    h('button', { type: 'button', text: 'CLOSE', onclick: closeOverlay }));
  openOverlay({
    name: 'autoconfigure',
    el: panel('METER · AUTOCONFIGURE', status, grid, btns),
  });

  api('/api/autoconfigure', { settle_s: 2.0, motion_safe: ui.motionSafe })
    .then((r) => {
      const m = r.metered || {};
      const row = (k, v) => grid.append(
        h('span', { class: 'k', text: k }), h('span', { text: v }));
      status.textContent = 'metered ' + (m.n !== undefined ? m.n : '?')
        + ' camera(s) · averaged settings applied to the rig · '
        + (r.acks !== undefined ? r.acks : '?') + '/' + expectedPis() + ' ack';
      const ex = m.exposure || {};
      const gn = m.gain || {};
      row('EXPOSURE µS', (ex.avg !== undefined
        ? 'avg ' + Math.round(ex.avg) + ' · range ' + Math.round(ex.min) + '–' + Math.round(ex.max)
        : '—'));
      row('GAIN', (gn.avg !== undefined
        ? 'avg ' + gn.avg.toFixed(2) + ' · range ' + gn.min.toFixed(2) + '–' + gn.max.toFixed(2)
        : '—'));
      row('AWB [R,B]', (Array.isArray(m.awb)
        ? '[' + m.awb[0].toFixed(2) + ', ' + m.awb[1].toFixed(2) + ']' : '—'));
      const ap = r.applied || {};
      row('APPLIED', (ap.exposure_us !== undefined
        ? ap.exposure_us + ' µs · gain ' + ap.analogue_gain : '—'));
      if (r.clamped) {
        grid.after(h('div', {
          class: 'clamp-note',
          text: '⚠ averaged exposure exceeded ' + cfgNum('motion_cap_us', 2000)
            + ' µs — clamped for motion (rolling-shutter blur risk); '
            + 'add light or raise gain to compensate',
        }));
      }
      btns.prepend(h('button', {
        class: 'act', type: 'button', text: 'APPLY AVERAGE TO PANEL',
        onclick: () => {
          if (ap.exposure_us !== undefined) ui.form.exp = ap.exposure_us;
          if (ap.analogue_gain !== undefined) ui.form.gain = ap.analogue_gain;
          if (Array.isArray(ap.awb_gains)) {
            ui.form.awbr = ap.awb_gains[0];
            ui.form.awbb = ap.awb_gains[1];
          }
          if (typeof ap.jpeg_quality === 'number') ui.form.q = ap.jpeg_quality;
          ui.formTouched = true;
          closeOverlay();
          render();
        },
      }));
      refreshState();
    })
    .catch((err) => {
      status.textContent = '';
      status.className = 'ov-result fail';
      status.textContent = (err.status === 409 && err.data && err.data.error === 'busy')
        ? 'BUSY · ' + String(err.data.op || 'op').toUpperCase() + ' in progress'
        : 'autoconfigure failed · ' + err.message;
    });
}

/* ── set-smb / set-ntp ── */

function openSetSmb() {
  const pis = (state.fleet && state.fleet.pis) || [];
  const smb0 = (pis[0] && pis[0].smb) || {};
  const inServer = h('input', { type: 'text', value: smb0.server || '' });
  const inShare = h('input', { type: 'text', value: smb0.share || '' });
  const inUser = h('input', { type: 'text', value: '' });
  const inPass = h('input', { type: 'password', value: '' });
  const inDomain = h('input', { type: 'text', value: 'WORKGROUP' });
  const result = h('div', { class: 'ov-result' });
  const form = h('div', { class: 'ov-form' },
    h('span', { class: 's-lbl', text: 'SERVER' }), inServer,
    h('span', { class: 's-lbl', text: 'SHARE' }), inShare,
    h('span', { class: 's-lbl', text: 'USERNAME' }), inUser,
    h('span', { class: 's-lbl', text: 'PASSWORD' }), inPass,
    h('span', { class: 's-lbl', text: 'DOMAIN' }), inDomain);
  const btn = h('button', {
    class: 'act', type: 'button', text: 'SET + PROBE',
    onclick: async () => {
      btn.disabled = true;
      result.className = 'ov-result';
      result.textContent = 'broadcasting SET_SMB…';
      try {
        const r = await api('/api/set-smb', {
          server: inServer.value.trim(),
          share: inShare.value.trim(),
          username: inUser.value.trim(),
          password: inPass.value,
          domain: inDomain.value.trim() || 'WORKGROUP',
        });
        inPass.value = '';                         // never echo the password back
        const results = r.results || r.replies || [];
        const total = (typeof r.total === 'number') ? r.total
          : (results.length || expectedPis());
        const reach = (typeof r.reachable === 'number') ? r.reachable
          : results.filter((x) => x.reachable).length;
        result.className = 'ov-result ' + (reach === total && total > 0 ? 'ok' : 'fail');
        result.textContent = reach + '/' + total + ' Pis can reach //'
          + inServer.value.trim() + '/' + inShare.value.trim() + ' on port 445';
      } catch (err) {
        inPass.value = '';
        result.className = 'ov-result fail';
        result.textContent = 'SET_SMB failed · ' + err.message;
      }
      btn.disabled = false;
    },
  });
  openOverlay({
    name: 'set-smb',
    el: panel('SET SMB DESTINATION', form, result,
      h('div', { class: 'btnrow' }, btn,
        h('button', { type: 'button', text: 'CLOSE', onclick: closeOverlay }))),
  });
  inServer.focus();
}

function openSetNtp() {
  const pis = (state.fleet && state.fleet.pis) || [];
  const cur = (pis[0] && pis[0].ntp && pis[0].ntp.server) || '';
  const inServer = h('input', { type: 'text', value: cur });
  const result = h('div', { class: 'ov-result' });
  const btn = h('button', {
    class: 'act', type: 'button', text: 'SET NTP',
    onclick: async () => {
      btn.disabled = true;
      result.className = 'ov-result';
      result.textContent = 'broadcasting SET_NTP…';
      try {
        const r = await api('/api/set-ntp', { server: inServer.value.trim() });
        const results = r.results || r.replies || [];
        const acks = (typeof r.acks === 'number') ? r.acks : results.length;
        result.className = 'ov-result ok';
        result.textContent = 'NTP_SET · ' + acks + '/' + expectedPis() + ' ack';
      } catch (err) {
        result.className = 'ov-result fail';
        result.textContent = 'SET_NTP failed · ' + err.message;
      }
      btn.disabled = false;
    },
  });
  openOverlay({
    name: 'set-ntp',
    el: panel('SET NTP SERVER',
      h('div', { class: 'ov-form' },
        h('span', { class: 's-lbl', text: 'SERVER' }), inServer),
      h('div', { class: 'ov-note', text: '~30 s to re-sync — watch RIG offsets, then SWEEP' }),
      result,
      h('div', { class: 'btnrow' }, btn,
        h('button', { type: 'button', text: 'CLOSE', onclick: closeOverlay }))),
  });
  inServer.focus();
}

/* ── update fleet ── */

function openUpdateFleet() {
  const log = h('div', { class: 'upd-log', text: '' });
  const status = h('div', { class: 'ov-note', text: 'running provision/update-pis.sh…' });
  openOverlay({
    name: 'update',
    el: panel('UPDATE FLEET', status, log,
      h('div', { class: 'btnrow' },
        h('button', { type: 'button', text: 'CLOSE', onclick: closeOverlay }))),
    onOp: (m) => {
      if (m.op !== 'update') return;
      if (m.line !== undefined) {
        log.append(document.createTextNode(m.line + '\n'));
        log.scrollTop = log.scrollHeight;
      }
      if (m.status === 'done') status.textContent = 'update complete · re-pinging for version census';
      if (m.status === 'error') status.textContent = 'update failed · ' + (m.detail || '');
    },
  });
  api('/api/update-fleet', {})
    .then((r) => {
      if (r && r.census && typeof r.census === 'object') {
        const lines = Object.keys(r.census).sort()
          .map((v) => 'v' + v + ' × ' + r.census[v]);
        log.append(document.createTextNode('\nVERSION CENSUS: ' + lines.join(' · ') + '\n'));
        log.scrollTop = log.scrollHeight;
      }
      status.textContent = 'done';
    })
    .catch((err) => {
      status.textContent = (err.status === 409 && err.data && err.data.error === 'busy')
        ? 'BUSY · ' + String(err.data.op || 'op').toUpperCase() + ' in progress'
        : 'update failed · ' + err.message;
    });
}

/* ── preflight ── */

function openPreflight() {
  const stepNames = ['ping', 'ntp', 'smb', 'disk', 'autoconfigure'];
  const rows = {};
  const list = h('div');
  for (const s of stepNames) {
    const st = h('span', { class: 'pf-st', text: '—' });
    rows[s] = st;
    list.append(h('div', { class: 'pf-row' },
      h('span', { class: 'pf-name', text: s.toUpperCase() }), st));
  }
  openOverlay({
    name: 'preflight',
    el: panel('PREFLIGHT', list,
      h('div', { class: 'btnrow' },
        h('button', { type: 'button', text: 'CLOSE', onclick: closeOverlay }))),
    onOp: (m) => {
      if (m.op !== 'preflight' || !m.step) return;
      const st = rows[m.step];
      if (!st) return;
      st.className = 'pf-st ' + (m.status || '');
      st.textContent = String(m.status || '').toUpperCase()
        + (m.detail ? ' · ' + m.detail : '');
    },
  });
  api('/api/preflight', { motion_safe: ui.motionSafe })
    .catch((err) => apiFail(err, 'PREFLIGHT'));
}

/* ── clear all ── */

function openClearAll() {
  const inp = h('input', { type: 'text', placeholder: 'type CLEAR ALL', autocomplete: 'off' });
  const result = h('div', { class: 'ov-result' });
  const btn = h('button', {
    class: 'danger', type: 'button', text: 'CLEAR EVERY PI', disabled: '',
    onclick: async () => {
      btn.disabled = true;
      try {
        await api('/api/clear-all', { confirm: inp.value });
        result.className = 'ov-result ok';
        result.textContent = 'CLEAR broadcast sent — see ticker for counts';
      } catch (err) {
        result.className = 'ov-result fail';
        result.textContent = 'CLEAR ALL failed · ' + err.message;
        btn.disabled = inp.value !== 'CLEAR ALL';
      }
    },
  });
  inp.addEventListener('input', () => { btn.disabled = inp.value !== 'CLEAR ALL'; });
  openOverlay({
    name: 'clear-all',
    el: panel('CLEAR ALL — EVERY SESSION ON EVERY PI',
      h('div', { class: 'ov-note', text: 'deletes every captured session on every Pi. type CLEAR ALL to arm.' }),
      inp, result,
      h('div', { class: 'btnrow' }, btn,
        h('button', { type: 'button', text: 'CANCEL', onclick: closeOverlay }))),
  });
  inp.focus();
}

/* ── fleet power (REBOOT / HALT) ── */

function openPower(action) {
  const verb = action.toUpperCase();               // REBOOT / HALT
  const warn = action === 'halt'
    ? 'HALT powers every Pi OFF. a Pi 3B has no soft power-on — the fleet '
      + 'needs a PHYSICAL power-cycle to return. type HALT to arm.'
    : 'REBOOT restarts every Pi — the rig goes silent for ~60 s, then the '
      + 'grid refills as nodes come back. type REBOOT to arm.';
  const inp = h('input', { type: 'text', placeholder: 'type ' + verb, autocomplete: 'off' });
  const result = h('div', { class: 'ov-result' });
  const btn = h('button', {
    class: 'danger', type: 'button', disabled: '',
    text: verb + ' EVERY PI',
    onclick: async () => {
      btn.disabled = true;
      try {
        const r = await api('/api/power', { action: action, confirm: inp.value });
        result.className = 'ov-result ok';
        result.textContent = r.acks + '/' + r.expected + ' ' + verb
          + ' ack · ' + (r.note || '');
      } catch (err) {
        result.className = 'ov-result fail';
        result.textContent = verb + ' failed · ' + err.message;
        btn.disabled = inp.value !== verb;
      }
    },
  });
  inp.addEventListener('input', () => { btn.disabled = inp.value !== verb; });
  openOverlay({
    name: 'power',
    el: panel(verb + ' FLEET',
      h('div', { class: 'ov-note', text: warn }),
      inp, result,
      h('div', { class: 'btnrow' }, btn,
        h('button', { type: 'button', text: 'CANCEL', onclick: closeOverlay }))),
  });
  inp.focus();
}

/* ── diagnose (streams provision/diagnose-*.sh) ── */

function openDiagnose(target) {
  const title = target === 'pis'
    ? 'DIAGNOSE PIS — find nodes silent on ping'
    : 'DIAGNOSE SMB — laptop-side share health';
  const log = h('div', { class: 'upd-log', text: '' });
  const status = h('div', {
    class: 'ov-note',
    text: 'running provision/diagnose-' + target + '.sh…',
  });
  openOverlay({
    name: 'diagnose',
    el: panel(title, status, log,
      h('div', { class: 'btnrow' },
        h('button', { type: 'button', text: 'CLOSE', onclick: closeOverlay }))),
    onOp: (m) => {
      if (m.op !== 'diagnose' || m.line === undefined) return;
      log.append(document.createTextNode(m.line + '\n'));
      log.scrollTop = log.scrollHeight;
    },
  });
  api('/api/diagnose', { target: target })
    .then((r) => {
      status.textContent = r.ok ? 'done — no problems found'
        : 'done — problems found (exit ' + r.exit_code + '), see output';
    })
    .catch((err) => {
      status.textContent = (err.status === 409 && err.data && err.data.error === 'busy')
        ? 'BUSY · ' + String(err.data.op || 'op').toUpperCase() + ' in progress'
        : 'diagnose failed · ' + err.message;
    });
}

/* ── preset save ── */

function openPresetSave() {
  const inp = h('input', { type: 'text', placeholder: 'preset-name', maxlength: '32' });
  const result = h('div', { class: 'ov-result' });
  const btn = h('button', {
    class: 'act', type: 'button', text: 'SAVE',
    onclick: async () => {
      const name = inp.value.trim();
      if (!name) return;
      btn.disabled = true;
      try {
        await api('/api/preset-save', { name: name });
        await refreshState();
        localTicker('ok', 'PRESET saved · ' + name);
        closeOverlay();
      } catch (err) {
        btn.disabled = false;
        result.className = 'ov-result fail';
        result.textContent = 'save failed · ' + err.message;
      }
    },
  });
  openOverlay({
    name: 'preset-save',
    el: panel('SAVE PRESET',
      h('div', { class: 'ov-note', text: 'saves the last APPLIED settings under a name' }),
      inp, result,
      h('div', { class: 'btnrow' }, btn,
        h('button', { type: 'button', text: 'CANCEL', onclick: closeOverlay }))),
  });
  inp.focus();
}

/* ── ticker log ── */

function openTickerLog() {
  const ol = h('ol', { class: 'log-list' });
  for (const t of state.ticker.slice().reverse()) ol.append(tickerLine(t));
  openOverlay({ name: 'log', el: panel('LOG · ' + state.ticker.length + ' LINES', ol) });
}

/* ─── wiring ────────────────────────────────────────────────────────────── */

function stepField(f, dir) {
  const def = STEP_DEFS[f];
  if (!def) return;
  if (f === 'lead') {
    ui.lead = Math.min(def.max, Math.max(def.min, ui.lead + dir * def.step));
    ui.leadTouched = true;
  } else {
    ui.form[f] = Math.min(def.max, Math.max(def.min, ui.form[f] + dir * def.step));
    // kill float drift on the decimal steppers
    ui.form[f] = parseFloat(ui.form[f].toFixed(def.dp + 1));
    ui.formTouched = true;
  }
  render();
}

async function applyConfigure() {
  const res = $('sel-res').value.split('x').map(Number);
  const body = {
    exposure_us: Math.round(ui.form.exp),
    analogue_gain: parseFloat(ui.form.gain.toFixed(2)),
    awb_r: parseFloat(ui.form.awbr.toFixed(2)),
    awb_b: parseFloat(ui.form.awbb.toFixed(2)),
    width: res[0],
    height: res[1],
    jpeg_quality: Math.round(ui.form.q),
  };
  const btn = $('btn-apply');
  btn.disabled = true;
  try {
    const r = await api('/api/configure', body);
    let acks = null;
    if (typeof r.acks === 'number') acks = r.acks;
    else if (Array.isArray(r.results)) acks = r.results.length;
    else if (Array.isArray(r.replies)) acks = r.replies.length;
    ui.lastApply = { at: Date.now() / 1000, acks: acks };
  } catch (err) {
    apiFail(err, 'CONFIGURE');
  }
  btn.disabled = false;
  render();
}

function wire() {
  // topbar
  $('mute-btn').addEventListener('click', () => { audio.ensure(); audio.toggle(); });
  setInterval(() => { $('clock').textContent = fmtClock(Date.now() / 1000); }, 1000);
  $('clock').textContent = fmtClock(Date.now() / 1000);

  // steppers (delegated)
  document.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('.stp');
    if (b) stepField(b.dataset.f, parseInt(b.dataset.d, 10));
  });

  // rig
  $('btn-sweep').addEventListener('click', async () => {
    try {
      absorbSweep(await api('/api/ping', {}));
      render();
    } catch (err) { apiFail(err, 'SWEEP'); }
  });

  // trigger
  $('btn-fire').addEventListener('click', () => fire(false));
  $('chk-override').addEventListener('change', (e) => {
    ui.override = e.target.checked;
    render();
  });

  // exposure
  $('btn-apply').addEventListener('click', applyConfigure);
  $('btn-meter').addEventListener('click', openAutoconfigure);
  $('chk-motion').addEventListener('change', (e) => { ui.motionSafe = e.target.checked; });
  $('sel-res').addEventListener('change', () => { ui.formTouched = true; });
  $('sel-preset').addEventListener('change', async (e) => {
    const name = e.target.value;
    if (!name) return;
    try {
      await api('/api/preset-apply', { name: name });
      ui.formTouched = false;              // let the preset settings flow into the panel
      await refreshState();
      localTicker('ok', 'PRESET applied · ' + name);
    } catch (err) { apiFail(err, 'PRESET APPLY'); }
  });
  $('btn-preset-save').addEventListener('click', openPresetSave);
  $('btn-preset-del').addEventListener('click', async () => {
    const name = $('sel-preset').value;
    if (!name) { localTicker('warn', 'no preset selected'); return; }
    try {
      await api('/api/preset-delete', { name: name });
      await refreshState();
      localTicker('info', 'PRESET deleted · ' + name);
    } catch (err) { apiFail(err, 'PRESET DELETE'); }
  });

  // session
  $('btn-new-subject').addEventListener('click', () => {
    const inp = $('inp-new-subject');
    inp.hidden = false;
    inp.focus();
  });
  $('inp-new-subject').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const s = sanitizeSubject(e.target.value);
      if (s) {
        if (ui.localSubjects.indexOf(s) < 0) ui.localSubjects.push(s);
        localStorage.setItem('pi32.subjects', JSON.stringify(ui.localSubjects));
        ui.subject = s;
        // Persist to the backend so every browser on the LAN sees the chip;
        // localStorage above is only the fallback if this POST fails.
        const all = subjects();
        api('/api/config', { subjects: all }).then((r) => {
          if (r && r.config) state.config = r.config;
          ui.localSubjects = [];
          localStorage.setItem('pi32.subjects', '[]');
          render();
        }).catch(() => { /* offline — chip stays local */ });
      }
      e.target.value = '';
      e.target.hidden = true;
      render();
    } else if (e.key === 'Escape') {
      e.stopPropagation();
      e.target.value = '';
      e.target.hidden = true;
    }
  });
  $('btn-del-subject').addEventListener('click', async () => {
    const gone = ui.subject;
    if (!gone) return;
    // Chip removal only — takes already on the share keep their names.
    const rest = subjects().filter((s) => s !== gone);
    ui.localSubjects = ui.localSubjects.filter((s) => s !== gone);
    localStorage.setItem('pi32.subjects', JSON.stringify(ui.localSubjects));
    ui.subject = rest[0] || '';
    try {
      const r = await api('/api/config', { subjects: rest });
      if (r && r.config) state.config = r.config;
      localTicker('info', 'SUBJECT chip removed · ' + gone);
    } catch (err) { apiFail(err, 'REMOVE SUBJECT'); }
    render();
  });

  // store
  $('btn-edit-smb').addEventListener('click', openSetSmb);
  $('btn-edit-ntp').addEventListener('click', openSetNtp);
  $('btn-update').addEventListener('click', openUpdateFleet);
  $('btn-diag-pis').addEventListener('click', () => openDiagnose('pis'));
  $('btn-diag-smb').addEventListener('click', () => openDiagnose('smb'));
  $('btn-reboot').addEventListener('click', () => openPower('reboot'));
  $('btn-halt').addEventListener('click', () => openPower('halt'));
  $('btn-preflight').addEventListener('click', openPreflight);
  $('btn-clearall').addEventListener('click', openClearAll);

  // ticker → full log
  $('ticker').addEventListener('click', openTickerLog);

  // overlays
  $('overlay-backdrop').addEventListener('click', closeOverlay);

  // keyboard: Space=fire (when armed, no overlay, not typing), Esc=close overlay
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toUpperCase();
    const typing = tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';
    if (e.key === 'Escape') {
      if (overlayStack.length) { e.preventDefault(); closeOverlay(); }
      return;
    }
    if (e.code === 'Space' && !typing && tag !== 'BUTTON' && !overlayStack.length) {
      e.preventDefault();
      if (fireArmed() && !state.current_op) fire(false);
    }
  });

  // restore local subjects
  try {
    const stored = JSON.parse(localStorage.getItem('pi32.subjects') || '[]');
    if (Array.isArray(stored)) ui.localSubjects = stored.filter((s) => typeof s === 'string');
  } catch (e) { /* corrupt — ignore */ }

  renderTopbar();
  connectSSE();
  refreshState();
}

document.addEventListener('DOMContentLoaded', wire);
