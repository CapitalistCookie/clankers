let liveWs = null, liveSession = null;
let statusInterval = null, waitingSessions = [];
let _lastStatusText = null, _lastStatusChangeAt = 0, _lastStatusSessions = null;   // A5: change-detect + cadence state

let livePanelEl = null;
// Place Live Sessions: on mobile, right above the Projects panel (operator wants
// it near the top); on desktop, in its original spot above the first 3-col grid.
function placeLivePanel() {
  if (!livePanelEl) return;
  const projectsPanel = document.querySelector('.panel[data-label="PROJECTS"]');
  const projectsGrid = projectsPanel ? projectsPanel.closest('.grid') : null;
  const grid3 = document.querySelectorAll('.grid-3')[0];
  if (isMobileView() && projectsGrid && projectsGrid.parentNode) {
    if (livePanelEl.nextElementSibling !== projectsGrid)
      projectsGrid.parentNode.insertBefore(livePanelEl, projectsGrid);
  } else if (grid3 && grid3.parentNode) {
    if (livePanelEl.nextElementSibling !== grid3)
      grid3.parentNode.insertBefore(livePanelEl, grid3);
  }
}

(function injectLivePanel() {
  const grid3 = document.querySelectorAll('.grid-3')[0];
  if (!grid3) return;
  const livePanel = document.createElement('div');
  livePanel.className = 'grid live-panel';
  livePanel.style.cssText = 'grid-template-columns:1fr; margin-bottom:2px;';
  livePanel.innerHTML = '<div class="panel" data-label="LIVE" style="animation-delay:0.5s"><h2>Live Sessions</h2><div id="live-sessions"></div></div>';
  livePanelEl = livePanel;
  grid3.parentNode.insertBefore(livePanel, grid3);
  placeLivePanel();
  // Re-place if the viewport crosses the mobile breakpoint (rotate / resize).
  if (window.matchMedia) {
    const mq = window.matchMedia('(max-width: 768px)');
    (mq.addEventListener ? mq.addEventListener.bind(mq, 'change') : mq.addListener.bind(mq))(placeLivePanel);
  }
  fetchStatus().finally(() => scheduleStatus());   // A5: initial paint, then adaptive self-rescheduling poll
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
})();

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    if (r.status === 401) { window.location = '/auth/login'; return; }
    const text = await r.text();
    // A5: identical tick — the common idle case re-sends byte-identical JSON;
    // skip the full list re-render + notification pass entirely.
    if (text === _lastStatusText) return;
    _lastStatusText = text;
    _lastStatusChangeAt = Date.now();
    let data; try { data = JSON.parse(text); } catch (e) { return; }
    _lastStatusSessions = data.sessions;
    renderLiveSessions(data.sessions);
    updateNotifications(data.sessions);
  } catch (e) {}
}

// A5: self-rescheduling status poll (replaced the fixed 5s setInterval). Fast
// (5s) while a terminal overlay is open OR the payload changed in the last 60s
// (an active fleet); slow (20s) once idle. Hidden tabs skip the fetch but keep
// the timer, so cadence resumes on return. The handle lives in `statusInterval`
// so any external clear/reset still works (clearTimeout & clearInterval both
// accept a timeout id).
function scheduleStatus() {
  if (statusInterval) clearTimeout(statusInterval);
  const termOpen = document.getElementById('terminal-overlay').classList.contains('open');
  const recentlyChanged = Date.now() - _lastStatusChangeAt < 60000;
  const delay = (termOpen || recentlyChanged) ? 5000 : 20000;
  statusInterval = setTimeout(async () => {
    if (!document.hidden) await fetchStatus();
    scheduleStatus();
  }, delay);
}

// ─── Favourites (per-browser, localStorage) — favourited sessions sort to the top ───
let favSessions = new Set(JSON.parse(localStorage.getItem('clanker_fav_sessions') || '[]'));
function saveFavs() { localStorage.setItem('clanker_fav_sessions', JSON.stringify([...favSessions])); }
function toggleFav(name, ev) {
  if (ev) ev.stopPropagation();
  favSessions.has(name) ? favSessions.delete(name) : favSessions.add(name);
  saveFavs();
  // Re-render from the cached sessions so the new fav sort shows at once — a
  // refetch would dedup-skip (favourites are client-side; the payload is unchanged).
  if (_lastStatusSessions) renderLiveSessions(_lastStatusSessions);
  else fetchStatus();
}
const STATE_RANK = { waiting: 0, working: 1, idle: 2 };
function favStateSort(a, b) {
  const fa = favSessions.has(a.session) ? 0 : 1, fb = favSessions.has(b.session) ? 0 : 1;
  if (fa !== fb) return fa - fb;                                  // favourites first
  return (STATE_RANK[a.state] ?? 3) - (STATE_RANK[b.state] ?? 3); // then by activity
}
function favStar(name) {
  const on = favSessions.has(name);
  return `<span class="fav-star ${on ? 'on' : ''}" title="Favourite" onclick="toggleFav('${name}', event)">${on ? '★' : '☆'}</span>`;
}

function renderLiveSessions(sessions) {
  const el = document.getElementById('live-sessions');
  if (!el) return;
  const sorted = sessions.filter(s => s.command === 'claude').sort(favStateSort);
  el.innerHTML = sorted.map(s => `
    <div class="session-card" onclick="openTerminal('${s.session}')">
      ${favStar(s.session)}
      <div class="session-info">
        <div class="session-name">${s.session}</div>
        <div class="session-preview">${escapeHtml(s.preview)}</div>
      </div>
      <span class="session-badge ${s.state}">${s.state}</span>
    </div>
  `).join('') || '<div class="no-data">No active Claude Code sessions</div>';
}

function updateNotifications(sessions) {
  const waiting = sessions.filter(s => s.state === 'waiting');
  const badge = document.getElementById('notif-badge');
  waitingSessions = waiting;
  if (waiting.length > 0) {
    badge.textContent = waiting.length + ' session' + (waiting.length > 1 ? 's' : '') + ' waiting';
    badge.classList.add('visible');
    if (!liveSession && 'Notification' in window && Notification.permission === 'granted') {
      new Notification('Clanker: Input needed', {
        body: waiting.map(s => s.session).join(', '), tag: 'clanker-input', renotify: true,
      });
    }
  } else { badge.classList.remove('visible'); }
}

function openWaitingSession() {
  if (waitingSessions.length > 0) openTerminal(waitingSessions[0].session);
}

let liveTerm = null, liveFit = null, liveResizeHandler = null;
let liveMode = null;   // 'pty' (xterm, desktop) | 'view' (capture, wraps, mobile)
let livePre = null;    // the <pre> element in view mode
let liveName = null;   // current session name (kept for reconnect)
let reconnectTimer = null, reconnectDelay = 600;

function setTermState(s) { const el = document.getElementById('terminal-state'); if (el) el.textContent = s; }

// Mobile browsers suspend a backgrounded tab and drop its WebSocket; without
// this the terminal looked dead until you exited and re-opened it. Reconnect
// whenever the socket closes while the terminal is still open, and immediately
// when the tab returns to the foreground.
function liveSocketDead() { return !liveWs || liveWs.readyState > 1; }  // 2=CLOSING 3=CLOSED
function reconnectLive() {
  if (!liveMode || !liveName) return;               // terminal was closed
  setTermState('reconnecting');
  // reader rides the view socket; only pty reconnects the PTY bridge
  (liveMode === 'pty' ? connectPty : connectView)(liveName);
}
function scheduleReconnect() {
  if (!liveMode || !liveName || reconnectTimer) return;
  if (document.hidden) return;                       // reconnect on return instead
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (liveMode && liveName && liveSocketDead()) reconnectLive();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 5000);
}
function liveSendResize() {
  if (liveTerm && liveWs && liveWs.readyState === WebSocket.OPEN)
    liveWs.send(JSON.stringify({ type: 'resize', cols: liveTerm.cols, rows: liveTerm.rows }));
}

// Raw key→byte-sequence map for PTY mode (built via fromCharCode so no
// backslash-escapes live in the Python-embedded JS string).
const PTYSEQ = {
  'Enter':  String.fromCharCode(13),
  'Escape': String.fromCharCode(27),
  'Tab':    String.fromCharCode(9),
  'BSpace': String.fromCharCode(127),
  'Up':     String.fromCharCode(27) + '[A',
  'Down':   String.fromCharCode(27) + '[B',
  'Left':   String.fromCharCode(27) + '[D',
  'Right':  String.fromCharCode(27) + '[C',
  'PPage':  String.fromCharCode(27) + '[5~',
  'NPage':  String.fromCharCode(27) + '[6~',
  'C-c':    String.fromCharCode(3),
  'C-d':    String.fromCharCode(4),
};

function isMobileView() { return window.matchMedia('(max-width: 768px)').matches; }

// Mobile renderer preference: 'reader' (transcript as real HTML — the default;
// terminal frames can't reflow phone-width) or 'view' (capture-pane frames).
let mobileMode = localStorage.getItem('clanker_mobile_mode') || 'reader';
function toggleMobileMode() {
  mobileMode = mobileMode === 'reader' ? 'view' : 'reader';
  localStorage.setItem('clanker_mobile_mode', mobileMode);
  if (navigator.vibrate) navigator.vibrate(15);
  if (liveName) { const n = liveName; closeTerminal(); openTerminal(n); }
  updateModeBtn();
}
function updateModeBtn() {
  const b = document.getElementById('terminal-mode-btn');
  if (!b) return;
  b.style.display = isMobileView() ? '' : 'none';
  b.textContent = mobileMode === 'reader' ? '📖' : '🖥';
  b.title = mobileMode === 'reader'
    ? 'Reader mode (tap for raw terminal)' : 'Terminal view (tap for reader)';
}

function openTerminal(name) {
  liveSession = name; liveName = name;
  reconnectDelay = 600;
  document.getElementById('terminal-title').textContent = name;
  const overlay = document.getElementById('terminal-overlay');
  overlay.classList.add('open');
  updateFab();
  document.getElementById('terminal-body').innerHTML = '';
  applyTerminalViewport();
  // Mobile: reader (default) or capture-view; both are non-attaching and size-
  // neutral. Desktop: xterm.js over the PTY bridge (true interactivity).
  if (isMobileView()) {
    if (mobileMode === 'reader' && window.buildReaderTerminal) {
      liveMode = 'reader';
      window.buildReaderTerminal(name, connectView);
    } else {
      buildViewTerminal(name);
    }
  } else {
    buildPtyTerminal(name);
  }
  updateModeBtn();
  applyComposeMode();
}

// A8: xterm.js (~300KB core) + its 3 addons load lazily on the first desktop
// terminal open — the vendor <script> tags were removed from _live_features_html,
// and the mobile reader/view paths never call this, so a phone that only reads
// transcripts never fetches xterm at all. The promise is cached (loaded once) and
// resolves when window.Terminal exists.
let _xtermPromise = null;
function ensureXterm() {
  if (window.Terminal && window.FitAddon && window.WebglAddon && window.WebLinksAddon)
    return Promise.resolve();
  if (_xtermPromise) return _xtermPromise;
  const load = (src) => new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.async = false;   // keep execution order (addons need window.Terminal defined)
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('failed to load ' + src));
    document.head.appendChild(s);
  });
  const p = load('/vendor/xterm.min.js')
    .then(() => load('/vendor/addon-fit.min.js'))
    .then(() => load('/vendor/addon-web-links.min.js'))
    .then(() => load('/vendor/addon-webgl.min.js'))
    .then(() => { if (!window.Terminal) throw new Error('xterm failed to initialize'); });
  p.catch(() => { if (_xtermPromise === p) _xtermPromise = null; });   // allow a retry after a failed load
  _xtermPromise = p;
  return p;
}

// ── Desktop: xterm.js over the PTY bridge ──
function buildPtyTerminal(name) {
  liveMode = 'pty';
  const container = document.getElementById('terminal-body');
  container.innerHTML = '<div class="term-loading">loading terminal…</div>';
  ensureXterm().then(() => {
    // The overlay may have been closed (or switched sessions) while xterm loaded;
    // liveTerm guards against a rapid close+reopen attaching two build callbacks.
    if (liveMode !== 'pty' || liveName !== name || liveTerm) return;
    container.innerHTML = '';
    const term = new Terminal({
      theme: XTERM_THEME, fontFamily: 'JetBrains Mono, ui-monospace, monospace',
      fontSize: 13, cursorBlink: false, scrollback: 5000, allowProposedApi: true,
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    try { term.loadAddon(new WebLinksAddon.WebLinksAddon()); } catch (e) {}
    term.open(container);
    // WebGL renderer: big scroll/render win, esp. on mobile GPUs. Loaded AFTER
    // open() (needs the element); any failure (no WebGL2, context loss) falls
    // back to the default DOM renderer silently.
    try {
      const gl = new WebglAddon.WebglAddon();
      gl.onContextLoss(() => { try { gl.dispose(); } catch (e) {} });
      term.loadAddon(gl);
    } catch (e) {}
    try { fit.fit(); } catch (e) {}
    liveTerm = term; liveFit = fit;
    // term/onData persist across reconnects; route input through the live socket.
    term.onData(d => { if (liveWs && liveWs.readyState === WebSocket.OPEN) liveWs.send(JSON.stringify({ type: 'input', data: d })); });
    term.onResize(() => liveSendResize());
    liveResizeHandler = () => { try { fit.fit(); liveSendResize(); } catch (e) {} };
    window.addEventListener('resize', liveResizeHandler);
    connectPty(name);
    setTimeout(() => { try { fit.fit(); liveSendResize(); } catch (e) {} }, 60);
  }).catch(() => {
    if (liveMode === 'pty' && liveName === name)
      container.innerHTML = '<div class="term-loading">terminal failed to load — reopen to retry</div>';
  });
}

// App-level keepalive: cloudflared/mobile proxies drop WS connections idle
// ~100s; a periodic no-op frame keeps the path warm so the terminal doesn't
// silently die between glances. Server handlers ignore unknown types.
function armKeepalive(ws) {
  const iv = setInterval(() => {
    if (document.hidden) return;   // A7: no keepalive ping while backgrounded (battery); resumes on return
    if (ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'ping' })); } catch (e) {}
    } else clearInterval(iv);
  }, 25000);
  ws.addEventListener('close', () => clearInterval(iv));
}

function connectPty(name) {
  if (liveWs) { try { liveWs.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/terminal/${name}`);
  ws.binaryType = 'arraybuffer';
  liveWs = ws;
  armKeepalive(ws);
  ws.onopen = () => {
    reconnectDelay = 600; setTermState('connected');
    try { liveFit.fit(); } catch (e) {}
    liveSendResize();
    if (liveTerm) liveTerm.focus();
  };
  ws.onmessage = (e) => { if (liveTerm) liveTerm.write(typeof e.data === 'string' ? e.data : new Uint8Array(e.data)); };
  ws.onclose = () => { setTermState('disconnected'); scheduleReconnect(); };
  ws.onerror = () => { setTermState('error'); };
}

// ── Mobile: capture-pane view. Streams `capture-pane -e -p` (read-only) and
// renders ANSI per-line so prose wraps but decoration rules clip (adaptive wrap).
// Typing goes through a hidden textarea (tap the terminal) — no reply bar.
// SCROLLBACK: Claude Code (and other TUIs) run on the terminal's ALTERNATE screen,
// which has NO tmux scrollback — the transcript lives inside the app. So to scroll
// back we send the session its own PageUp/PagePown (Claude scrolls its transcript)
// and the next capture shows the earlier content. Works via the ⇞/⇟ keys, the
// mouse wheel, and a touch drag. ──
let viewPending = null, viewRaf = null, viewCapture = null, viewLastFrame = null;
let jumpLiveBtn = null, viewScrolledUp = false, viewIsTui = false;
let viewAppScrolled = false;   // the APP's transcript was paged up (vs native pan only)
let viewStickBottom = true;    // follow the live tail through size changes (keyboard)
let viewResizeObs = null;
let viewLastCH = 0;            // clientHeight at the last scroll event (resize detector)

function _viewRenderNow() {
  viewRaf = null;
  if (viewPending == null || !livePre) return;
  viewLastFrame = viewPending;   // kept so display toggles can re-render instantly
  // Stick-to-bottom: a new frame keeps the view anchored to the live tail, but
  // only if the user was already there — someone inspecting the top of the frame
  // (e.g. a fresh session's banner) must not be yanked back down every capture.
  const atBottom = livePre.scrollHeight - livePre.scrollTop - livePre.clientHeight < 24;
  livePre.innerHTML = renderAnsiToLines(viewPending);
  if (atBottom) livePre.scrollTop = livePre.scrollHeight;
  viewPending = null;
}
function _viewSchedule() {
  // Paint the latest frame on the next display refresh (rAF) — aligned to the
  // screen so motion is smooth, coalesced (only the newest frame is drawn), and
  // paused entirely while the tab is hidden (battery; repaints on return).
  if (document.hidden || viewRaf) return;
  viewRaf = requestAnimationFrame(_viewRenderNow);
}

function _setScrolled(v) {
  viewScrolledUp = v;
  if (jumpLiveBtn) jumpLiveBtn.classList.toggle('show', v);
}
// ⇞/⇟ buttons: pan the local frame first; once it's at its edge, page the app's
// own transcript (PageUp/PageDown). A fresh session with no transcript to page is
// still fully viewable via the local pan.
function viewScroll(dir) {
  if (livePre) {
    const room = dir === 'up'
      ? livePre.scrollTop > 4
      : livePre.scrollHeight - livePre.scrollTop - livePre.clientHeight > 4;
    if (room) {
      livePre.scrollTop += (dir === 'up' ? -0.9 : 0.9) * livePre.clientHeight;
      return;
    }
  }
  wsKey(dir === 'up' ? 'PPage' : 'NPage');
  if (dir === 'up') { viewAppScrolled = true; _setScrolled(true); }
}
// Return the VIEW to the live tail: anchor the local frame and re-arm
// stick-to-bottom so size changes (the soft keyboard opening/closing) keep the
// input line in sight. Used whenever the user acts on the session — typing is
// an implicit "show me where I'm typing".
function _viewSnapLive() {
  viewAppScrolled = false;
  viewStickBottom = true;
  if (livePre) livePre.scrollTop = livePre.scrollHeight;
  _setScrolled(false);
}
// Snap back to the live tail: page the app back down (only if its transcript was
// actually scrolled) and re-anchor the local frame.
function jumpToLive() {
  if (viewAppScrolled) for (let i = 0; i < 14; i++) wsKey('NPage');
  _viewSnapLive();
}

// ── line-smooth scroll: a wheel/drag sends the TUI mouse-wheel events (it scrolls
// its transcript line-by-line). `n` = notches, batched into one message. Gated to
// TUIs so a plain shell never receives stray mouse bytes. ──
function wsWheel(dir, n) {
  if (!viewIsTui || !liveWs || liveWs.readyState !== WebSocket.OPEN) return;
  const ESC = String.fromCharCode(27);
  const one = ESC + (dir === 'up' ? '[<64;1;1M' : '[<65;1;1M');
  liveWs.send(JSON.stringify({ type: 'keys', data: one.repeat(Math.max(1, Math.min(16, n))) }));
  if (dir === 'up') { viewAppScrolled = true; _setScrolled(true); }
}

// Coalesce wheel/drag input to ONE batched send per animation frame, so a fast
// flick can't flood the session with dozens of send-keys (which itself lags).
let wheelAccum = 0, wheelRaf = null;
function _flushWheel() {
  wheelRaf = null;
  if (!wheelAccum) return;
  const n = wheelAccum; wheelAccum = 0;
  wsWheel(n > 0 ? 'up' : 'down', Math.abs(n));   // +lines = scroll up (older)
}
function queueWheel(lines) {
  if (!viewIsTui || !lines) return;
  wheelAccum += lines;
  if (!wheelRaf) wheelRaf = requestAnimationFrame(_flushWheel);
}

function buildViewTerminal(name) {
  liveMode = 'view';
  viewPending = null; viewScrolledUp = false; viewAppScrolled = false; viewStickBottom = true;
  viewLastCH = 0;
  const container = document.getElementById('terminal-body');
  const pre = document.createElement('pre');
  pre.className = 'live-terminal-pre';
  pre.id = 'live-pre';
  container.appendChild(pre);
  livePre = pre;

  // "↓ live" pill — shown when scrolled up; tap to return to the live tail.
  const jl = document.createElement('button');
  jl.className = 'jump-live'; jl.textContent = '↓ live';
  jl.addEventListener('click', jumpToLive);
  container.appendChild(jl);
  jumpLiveBtn = jl;

  // Hidden textarea captures the soft keyboard; tapping the terminal focuses it.
  const ta = document.createElement('textarea');
  ta.className = 'terminal-input-capture';
  ta.setAttribute('autocomplete', 'off'); ta.setAttribute('autocapitalize', 'off');
  ta.setAttribute('autocorrect', 'off'); ta.setAttribute('spellcheck', 'false');
  container.appendChild(ta);
  viewCapture = ta;
  ta.addEventListener('input', () => { if (ta.value) { sendText(ta.value); ta.value = ''; } });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendNamed('Enter'); ta.value = ''; }
    else if (e.key === 'Backspace' && !ta.value) { e.preventDefault(); sendNamed('BSpace'); }
  });

  // A tap (not a drag) focuses the input. `click` covers mouse/desktop; the
  // explicit tap detector in touchend covers mobile, where browsers' tap-to-click
  // heuristics get strict over a pan-y scroller (a few px of finger noise
  // suppresses the click). A tap = barely moved, quick, and didn't scroll.
  pre.addEventListener('click', () => {
    const inp = composeMode ? composeInput() : viewCapture;
    if (inp) inp.focus({ preventScroll: true });
  });
  let tapY = null, tapT = 0, tapST = 0;

  // The pill tracks the LOCAL scroll position too: visible whenever the view is
  // away from the live tail (natively panned up, or app transcript paged up).
  pre.addEventListener('scroll', () => {
    // A box resize (soft keyboard opening/closing) fires a scroll event with the
    // OLD scrollTop against the NEW geometry — that's the box moving under the
    // user, not the user scrolling. Detect it by the height change and don't let
    // it overwrite their intent; the ResizeObserver below re-anchors.
    if (pre.clientHeight !== viewLastCH) { viewLastCH = pre.clientHeight; return; }
    const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    viewStickBottom = nearBottom;
    if (!nearBottom) _setScrolled(true);
    else if (!viewAppScrolled) _setScrolled(false);
  }, { passive: true });

  // Soft keyboard (or rotation) resizes the terminal: if the user was following
  // the live tail, keep them there — the input box rides up above the keyboard
  // and drops back down when it dismisses. A deliberately scrolled-up reader is
  // left where they are.
  viewResizeObs = new ResizeObserver(() => {
    viewLastCH = pre.clientHeight;
    if (viewStickBottom) pre.scrollTop = pre.scrollHeight;
  });
  viewResizeObs.observe(pre);

  // Scrolling a TUI is two-layered: within the frame the browser pans natively
  // (the frame is often taller than a phone viewport — wrapped lines, 50-row
  // panes, a fresh session's banner). Only a pull BEYOND the frame's edge pages
  // the app's own transcript via line-granular mouse-wheel SGR. For a plain
  // shell (viewIsTui false) everything stays native.
  const atEdge = (up) => up
    ? pre.scrollTop <= 2
    : pre.scrollHeight - pre.scrollTop - pre.clientHeight <= 2;
  const frameScrolls = () => pre.scrollHeight - pre.clientHeight > 4;
  pre.addEventListener('wheel', (e) => {
    if (!viewIsTui) return;
    const up = e.deltaY < 0;
    if (frameScrolls() && !atEdge(up)) return;   // native wheel pans the frame
    e.preventDefault();
    const notches = Math.max(1, Math.min(8, Math.round(Math.abs(e.deltaY) / 32)));
    queueWheel(up ? notches : -notches);   // +up = older
  }, { passive: false });
  let tY = null, tAcc = 0;
  const WHEEL_PX = 14;   // px of drag per line — fine-grained, smooth
  pre.addEventListener('touchstart', (e) => {
    tY = e.touches[0].clientY; tAcc = 0;
    tapY = tY; tapT = Date.now(); tapST = pre.scrollTop;
  }, { passive: true });
  pre.addEventListener('touchmove', (e) => {
    if (!viewIsTui || tY == null) return;
    const y = e.touches[0].clientY;
    const dy = y - tY;
    // Within the frame the browser pans natively (touch-action: pan-y); only a
    // pull at the edge pages the app. (Mid-gesture the browser may ignore a late
    // preventDefault once a native pan owns the gesture — lifting the finger and
    // dragging again at the edge always engages the app paging.)
    if (frameScrolls() && !atEdge(dy > 0)) { tY = y; tAcc = 0; return; }
    tAcc += dy; tY = y;
    const lines = Math.trunc(tAcc / WHEEL_PX);
    if (lines !== 0) {
      queueWheel(lines);   // drag DOWN (positive) reveals EARLIER content
      tAcc -= lines * WHEEL_PX;
      e.preventDefault();
    }
  }, { passive: false });
  pre.addEventListener('touchend', (e) => {
    const t = e.changedTouches && e.changedTouches[0];
    if (t && tapY != null && Math.abs(t.clientY - tapY) < 8 &&
        Date.now() - tapT < 350 && Math.abs(pre.scrollTop - tapST) < 4) {
      const inp = composeMode ? composeInput() : viewCapture;
      if (inp) inp.focus({ preventScroll: true });
    }
    tY = null; tapY = null;
  });

  connectView(name);
  // No auto-focus: opening a session must NOT raise the soft keyboard. The
  // keyboard appears only when the user taps the terminal (the `pre` click
  // handler above focuses the capture textarea on demand).
}

function connectView(name) {
  if (liveWs) { try { liveWs.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/view/${name}`);
  liveWs = ws;
  armKeepalive(ws);
  ws.onopen = () => { reconnectDelay = 600; setTermState('connected'); };
  ws.onmessage = (e) => {
    let msg; try { msg = JSON.parse(e.data); } catch (err) { return; }
    if (msg.type === 'content') {
      viewPending = msg.data || ''; _viewSchedule();
      // Reader rides this socket for input; the frames also feed its pinned
      // live status line (the pane's real one, ANSI intact).
      if (liveMode === 'reader' && window.readerFrame) window.readerFrame(msg.data || '');
    }
    else if (msg.type === 'meta') {
      viewIsTui = !!msg.tui;
      // TUIs scroll via wheel events → disable native scroll (no bounce). A shell
      // keeps native scroll. Toggled here because meta arrives after the pre exists.
      if (livePre) livePre.classList.toggle('tui-view', viewIsTui);
    }
  };
  ws.onclose = () => { setTermState('disconnected'); scheduleReconnect(); };
  ws.onerror = () => { setTermState('error'); };
}

function closeTerminal() {
  // If we scrolled the shared session's transcript up, return it to the live tail
  // BEFORE disconnecting — otherwise the desktop Claude session (same tmux pane)
  // is left scrolled up. A native-only pan never touched the pane, so it needs no
  // reset. Send the page-downs while the socket is still open.
  if (viewAppScrolled && liveWs && liveWs.readyState === WebSocket.OPEN) {
    for (let i = 0; i < 16; i++) wsKey('NPage');
  }
  document.getElementById('terminal-overlay').classList.remove('open');
  updateFab();
  if (liveResizeHandler) { window.removeEventListener('resize', liveResizeHandler); liveResizeHandler = null; }
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  // Null these BEFORE closing the socket so onclose's scheduleReconnect bails.
  liveMode = null; liveName = null;
  if (liveWs) { try { liveWs.close(); } catch (e) {} liveWs = null; }
  if (liveTerm) { try { liveTerm.dispose(); } catch (e) {} liveTerm = null; }
  if (viewRaf) { cancelAnimationFrame(viewRaf); viewRaf = null; }
  if (wheelRaf) { cancelAnimationFrame(wheelRaf); wheelRaf = null; }
  wheelAccum = 0;
  viewPending = null; viewCapture = null; jumpLiveBtn = null; viewScrolledUp = false; viewIsTui = false;
  viewAppScrolled = false; viewStickBottom = true;
  if (viewResizeObs) { viewResizeObs.disconnect(); viewResizeObs = null; }
  liveFit = null; livePre = null;
  liveSession = null;
  if (window.teardownReader) window.teardownReader();
  applyComposeMode();   // liveMode is null -> bar hides
  resetTerminalViewport();
}

// ── Unified input: routes to the PTY byte-stream or the capture-view send-keys
// protocol depending on which renderer is active. ──
function sendText(text) {
  if (!text || !liveWs || liveWs.readyState !== WebSocket.OPEN) return;
  // reader mode rides the /ws/view socket — same send-keys protocol as view
  if (liveMode === 'view' || liveMode === 'reader') liveWs.send(JSON.stringify({ type: 'keys', data: text }));
  else { liveWs.send(JSON.stringify({ type: 'input', data: text })); if (liveTerm) liveTerm.focus(); }
  _viewSnapLive();   // typing snaps the view back to the live tail (input line)
}

// Raw named-key send (no scroll-state side effect) — used by viewScroll.
function wsKey(key) {
  if (!liveWs || liveWs.readyState !== WebSocket.OPEN) return;
  if (liveMode === 'view' || liveMode === 'reader') {
    liveWs.send(JSON.stringify({ type: 'key', data: key }));
  } else {
    const seq = PTYSEQ[key];
    if (seq != null) { liveWs.send(JSON.stringify({ type: 'input', data: seq })); if (liveTerm) liveTerm.focus(); }
  }
}

function sendNamed(key) {   // key: Enter|Escape|Tab|Up|Down|Left|Right|C-c|...
  wsKey(key);
  // Any non-scroll key returns the TUI to the live tail (the input sits there).
  if (key !== 'PPage' && key !== 'NPage') _viewSnapLive();
}

// ── Compose bar: native-typing input (2026-07-19). The field is a plain
// textarea we never intercept per-keystroke, so glide typing / autocorrect /
// dictation behave exactly like a messaging app; Enter sends the whole line
// atomically (send-keys -l + Enter) — which also survives flaky tunnels better
// than streamed keystrokes. Default ON for touch devices, OFF for fine
// pointers; ⌨ in the header (or the saved preference) toggles. Raw keystroke
// mode (the hidden capture textarea / xterm passthrough) remains for TUIs. ──
let composeMode = localStorage.getItem('clanker_compose') === null
  ? window.matchMedia('(pointer: coarse)').matches
  : localStorage.getItem('clanker_compose') === '1';
let composeHist = [];
try { composeHist = JSON.parse(localStorage.getItem('clanker_compose_hist') || '[]'); } catch (e) {}
let composeHistIdx = null, composeDraft = '';

function composeInput() { return document.getElementById('compose-input'); }
function applyComposeMode() {
  const bar = document.getElementById('compose-bar');
  if (!bar) return;
  bar.classList.toggle('show', composeMode && !!liveMode);
}
function toggleComposeMode() {
  composeMode = !composeMode;
  localStorage.setItem('clanker_compose', composeMode ? '1' : '0');
  if (navigator.vibrate) navigator.vibrate(15);
  applyComposeMode();
  const inp = composeInput();
  if (composeMode && liveMode && inp) inp.focus();
  else if (liveMode === 'view' && viewCapture) viewCapture.focus({ preventScroll: true });
  else if (liveMode === 'pty' && liveTerm) liveTerm.focus();
}
function composeGrow() {
  const inp = composeInput(); if (!inp) return;
  inp.style.height = 'auto';
  inp.style.height = Math.min(inp.scrollHeight, 96) + 'px';
}
function composeSend() {
  const inp = composeInput(); if (!inp) return;
  // No backslash escapes in this Python-embedded JS (see PTYSEQ note) —
  // trim trailing newlines via charCode instead of a regex literal.
  const NL = String.fromCharCode(10);
  let text = inp.value;
  while (text.endsWith(NL)) text = text.slice(0, -1);
  if (!text) { sendNamed('Enter'); return; }   // empty send = bare Enter
  sendText(text);
  sendNamed('Enter');
  if (composeHist[composeHist.length - 1] !== text) {
    composeHist.push(text);
    if (composeHist.length > 50) composeHist = composeHist.slice(-50);
    try { localStorage.setItem('clanker_compose_hist', JSON.stringify(composeHist)); } catch (e) {}
  }
  composeHistIdx = null;
  inp.value = ''; composeGrow();
  inp.focus();
}
(function initCompose() {
  const inp = composeInput(); if (!inp) return;
  inp.addEventListener('input', composeGrow);
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); composeSend(); return; }
    // History browse only when the caret can't move further in that direction.
    if (e.key === 'ArrowUp' && inp.selectionStart === 0 && composeHist.length) {
      e.preventDefault();
      if (composeHistIdx === null) { composeDraft = inp.value; composeHistIdx = composeHist.length; }
      if (composeHistIdx > 0) { composeHistIdx--; inp.value = composeHist[composeHistIdx]; composeGrow(); }
    } else if (e.key === 'ArrowDown' && composeHistIdx !== null && inp.selectionEnd === inp.value.length) {
      e.preventDefault();
      composeHistIdx++;
      if (composeHistIdx >= composeHist.length) { composeHistIdx = null; inp.value = composeDraft; }
      else inp.value = composeHist[composeHistIdx];
      composeGrow();
    }
  });
  const send = document.getElementById('compose-send');
  if (send) send.addEventListener('click', composeSend);
})();

// ── Todo-list visibility (display-only). Hidden by default on mobile, where the
// checklist eats most of the screen; the pane itself is never sent any keys. ──
let todosHidden = localStorage.getItem('clanker_todos_hidden') === null
  ? isMobileView() : localStorage.getItem('clanker_todos_hidden') === '1';
function toggleTodos() {
  todosHidden = !todosHidden;
  localStorage.setItem('clanker_todos_hidden', todosHidden ? '1' : '0');
  if (navigator.vibrate) navigator.vibrate(15);
  if (viewLastFrame != null) { viewPending = viewLastFrame; _viewSchedule(); }
}
// esc: tap = send Escape; hold 550ms = hide/show the todo list.
(() => {
  const b = document.getElementById('key-esc');
  if (!b) return;
  let t = null, held = false;
  b.addEventListener('pointerdown', () => {
    held = false; clearTimeout(t);
    t = setTimeout(() => { held = true; toggleTodos(); }, 550);
  });
  b.addEventListener('pointerup', () => { clearTimeout(t); if (!held) sendNamed('Escape'); held = false; });
  b.addEventListener('pointerleave', () => clearTimeout(t));
  b.addEventListener('pointercancel', () => clearTimeout(t));
  b.addEventListener('contextmenu', e => e.preventDefault());
})();

// When the tab returns to the foreground: if the socket died while we were
// backgrounded (mobile suspends the tab + drops the WS), reconnect right away so
// the terminal is live again WITHOUT the operator having to exit and re-open it.
// Otherwise just paint the latest buffered frame (rendering is skipped while hidden).
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  if (liveMode && liveName && liveSocketDead()) {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    reconnectDelay = 600;
    reconnectLive();
  } else if (liveMode === 'view' && viewPending != null) {
    _viewSchedule();
  }
  // A2: repaint the system monitor from the snapshot we withheld while hidden.
  if (_sysmonLast && document.getElementById('sysmon-overlay').classList.contains('open')) {
    renderSysmon(_sysmonLast);
  }
});
// Some mobile browsers fire pageshow (bfcache restore) but not visibilitychange.
window.addEventListener('pageshow', () => {
  if (liveMode && liveName && liveSocketDead()) reconnectLive();
});

// ── Keep the terminal (and its input bar) above the on-screen keyboard.
// The soft keyboard shrinks the VISUAL viewport but not the layout viewport, so
// a position:fixed overlay would otherwise sit behind the keyboard. Pin the
// overlay box to the visual viewport. ──
function applyTerminalViewport() {
  const vv = window.visualViewport;
  const ov = document.getElementById('terminal-overlay');
  if (!vv || !ov.classList.contains('open')) return;
  ov.style.top = vv.offsetTop + 'px';
  ov.style.left = vv.offsetLeft + 'px';
  ov.style.height = vv.height + 'px';
  ov.style.width = vv.width + 'px';
}
function resetTerminalViewport() {
  const ov = document.getElementById('terminal-overlay');
  ov.style.top = ov.style.left = ov.style.height = ov.style.width = '';
}
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', applyTerminalViewport);
  window.visualViewport.addEventListener('scroll', applyTerminalViewport);
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (document.getElementById('newsess-overlay').classList.contains('open')) { closeNewSession(); return; }
  if (document.getElementById('sysmon-overlay').classList.contains('open')) { closeSysmon(); return; }
  if (document.getElementById('terminal-overlay').classList.contains('open')) closeTerminal();
});
// ─── New Session (launch a fresh tmux instance from the WebUI) ───
function openNewSession() {
  document.getElementById('newsess-name').value = '';
  document.getElementById('newsess-shell').checked = false;
  document.getElementById('newsess-overlay').classList.add('open');
  updateFab();
  setTimeout(() => document.getElementById('newsess-name').focus(), 60);
}
function closeNewSession() { document.getElementById('newsess-overlay').classList.remove('open'); updateFab(); }

// ─── Native System Monitor (floating button → overlay, real-time WS stream) ───
let sysmonWs = null, sysmonSort = 'cpu', sysmonFilter = '', sysmonReconnect = null;

// Floating sys FAB hides whenever an overlay is open (the terminal header carries
// its own sys button, so the monitor stays reachable while viewing a terminal).
function updateFab() {
  const any = ['terminal-overlay', 'sysmon-overlay', 'newsess-overlay']
    .some(id => document.getElementById(id).classList.contains('open'));
  document.body.classList.toggle('overlay-open', any);
}
function openSysmon() {
  document.getElementById('sysmon-overlay').classList.add('open');
  updateFab();
  connectSysmon();
}
function closeSysmon() {
  document.getElementById('sysmon-overlay').classList.remove('open');
  updateFab();
  if (sysmonReconnect) { clearTimeout(sysmonReconnect); sysmonReconnect = null; }
  if (sysmonWs) { try { sysmonWs.close(); } catch (e) {} sysmonWs = null; }
}
// Real-time: one WebSocket streaming a JSON snapshot ~every second (one persistent
// SSH loop on the host), instead of polling. Reconnects if the socket drops.
function connectSysmon() {
  if (sysmonWs) { try { sysmonWs.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/sysmon`);
  sysmonWs = ws;
  ws.onmessage = (e) => {
    let d; try { d = JSON.parse(e.data); } catch (err) { return; }
    if (d.error) { renderSysmonError(d.error); return; }
    renderSysmon(d);
  };
  ws.onclose = () => {
    if (!document.getElementById('sysmon-overlay').classList.contains('open')) return;
    if (sysmonReconnect) clearTimeout(sysmonReconnect);
    sysmonReconnect = setTimeout(() => { if (!document.hidden) connectSysmon(); }, 1500);
  };
  ws.onerror = () => {};
}

// ── formatters ──
function smBytes(n) {
  if (n == null) return '—';
  const u = ['B','K','M','G','T','P']; let i = 0; n = Math.abs(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + u[i];
}
function smRate(n) { return smBytes(n) + '/s'; }
function smCls(pct) { return pct >= 85 ? 'crit' : pct >= 60 ? 'warn' : ''; }
function smDur(s) {
  s = Math.floor(s || 0); const d = Math.floor(s/86400), h = Math.floor(s%86400/3600), m = Math.floor(s%3600/60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

function bar(label, pct, valText, extraCls) {
  const c = smCls(pct);
  return `<div class="sm-bar-row"><span class="sm-lbl">${esc(label)}</span>`
    + `<span class="sm-bar ${c} ${extraCls||''}"><span style="width:${Math.max(0,Math.min(100,pct)).toFixed(1)}%"></span></span>`
    + `<span class="sm-val">${valText}</span></div>`;
}

let _sysmonLast = null;
function renderSysmon(d) {
  _sysmonLast = d;
  // A2: while the tab is backgrounded, keep only the latest snapshot and skip the
  // ~6-card innerHTML rebuild (the 1s WS stream keeps flowing); the visibilitychange
  // handler repaints from _sysmonLast on return.
  if (document.hidden) return;
  document.getElementById('sm-host').textContent = (d.host || '') + (d.model ? ' · ' + d.model : '');
  document.getElementById('sm-age').textContent = '● live';
  // Don't rebuild the body while the user is typing in the process filter — a full
  // innerHTML swap would steal focus. The snapshot is stored; the next poll after
  // they blur repaints. (The header age above still updates.)
  const af = document.activeElement;
  if (af && af.id === 'sm-filter') return;

  const memPct = d.mem.total ? 100 * d.mem.used / d.mem.total : 0;
  const swapPct = d.swap.total ? 100 * d.swap.used / d.swap.total : 0;
  const loadPctOfCores = d.ncpu ? 100 * d.load[0] / d.ncpu : 0;

  // ── CPU card ──
  let cpu = `<div class="sm-card"><h4>CPU <span class="sm-sub">${d.ncpu} threads</span></h4>`;
  cpu += `<div class="sm-chips">`
    + `<span class="sm-chip"><b>${d.cpu}%</b> total</span>`
    + (d.temp != null ? `<span class="sm-chip" style="${d.temp>=85?'border-color:var(--accent-red)':''}"><b>${d.temp}°C</b></span>` : '')
    + (d.freq != null ? `<span class="sm-chip"><b>${(d.freq/1000).toFixed(2)}</b>GHz</span>` : '')
    + `<span class="sm-chip">load <b>${d.load[0].toFixed(2)}</b> ${d.load[1].toFixed(2)} ${d.load[2].toFixed(2)}</span>`
    + `<span class="sm-chip">tasks <b>${esc(d.tasks)}</b></span>`
    + `<span class="sm-chip">up <b>${smDur(d.uptime)}</b></span>`
    + `</div>`;
  cpu += bar('all', d.cpu, d.cpu + '%');
  cpu += `<div class="sm-cores">` + d.cores.map((c, i) =>
    `<div class="sm-core">${i}<div class="sm-cbar ${smCls(c)}"><span style="width:${c}%"></span></div></div>`).join('') + `</div>`;
  cpu += `</div>`;

  // ── GPU card (from the GPU host's nvidia-smi via the sysmon warmer) ──
  let gpu = '';
  if (d.gpus && d.gpus.length) {
    gpu = `<div class="sm-card"><h4>GPU${d.gpus.length > 1 ? 's' : ''} <span class="sm-sub">${esc(d.gpus[0].name)}</span></h4>`;
    d.gpus.forEach((g, i) => {
      const vramPct = g.mem_total ? 100 * g.mem_used / g.mem_total : 0;
      gpu += bar(d.gpus.length > 1 ? `${i}·util` : 'util', g.util, `${Math.round(g.util)}%`);
      gpu += bar('vram', vramPct, `${(g.mem_used / 1024).toFixed(1)} / ${(g.mem_total / 1024).toFixed(1)} GiB`);
      gpu += `<div class="sm-chips" style="margin-top:4px">`
        + (g.temp != null ? `<span class="sm-chip" style="${g.temp >= 85 ? 'border-color:var(--accent-red)' : ''}"><b>${Math.round(g.temp)}°C</b></span>` : '')
        + (g.power != null ? `<span class="sm-chip"><b>${Math.round(g.power)}W</b>${g.plimit ? ' / ' + Math.round(g.plimit) + 'W' : ''}</span>` : '')
        + `</div>`;
    });
    gpu += `</div>`;
  }

  // ── Memory card ──
  let mem = `<div class="sm-card"><h4>Memory</h4>`;
  mem += bar('RAM', memPct, `${smBytes(d.mem.used)} / ${smBytes(d.mem.total)}`);
  mem += `<div class="sm-chips" style="margin-top:6px">`
    + `<span class="sm-chip">avail <b>${smBytes(d.mem.avail)}</b></span>`
    + `<span class="sm-chip">cached <b>${smBytes(d.mem.cached)}</b></span>`
    + `<span class="sm-chip">buffers <b>${smBytes(d.mem.buffers)}</b></span></div>`;
  mem += d.swap.total ? bar('swap', swapPct, `${smBytes(d.swap.used)} / ${smBytes(d.swap.total)}`)
                      : `<div class="sm-bar-row"><span class="sm-lbl">swap</span><span class="sm-val">none</span></div>`;
  mem += `</div>`;

  // ── Network card ──
  let net = `<div class="sm-card"><h4>Network</h4>`;
  net += (d.net.length ? d.net.map(n =>
    `<div class="sm-bar-row"><span class="sm-lbl" style="min-width:74px">${esc(n.iface)}</span>`
    + `<span class="sm-val" style="margin-left:0">↓ ${smRate(n.rx)}</span>`
    + `<span class="sm-val">↑ ${smRate(n.tx)}</span></div>`).join('')
    : `<div class="no-data">no active interfaces</div>`);
  net += `</div>`;

  // ── Disk card (I/O + filesystems) ──
  let disk = `<div class="sm-card"><h4>Disk</h4>`;
  if (d.disk_io.length) {
    disk += `<div style="font-size:10px;color:var(--text-muted);font-family:var(--font-mono);margin-bottom:4px">I/O</div>`;
    disk += d.disk_io.map(x =>
      `<div class="sm-bar-row"><span class="sm-lbl" style="min-width:64px">${esc(x.dev)}</span>`
      + `<span class="sm-val" style="margin-left:0">r ${smRate(x.read)}</span>`
      + `<span class="sm-val">w ${smRate(x.write)}</span></div>`).join('');
  }
  if (d.fs.length) {
    disk += `<div style="font-size:10px;color:var(--text-muted);font-family:var(--font-mono);margin:8px 0 4px">Filesystems</div>`;
    disk += d.fs.map(f => bar(f.mount.length > 18 ? '…' + f.mount.slice(-17) : f.mount, f.pct,
      `${smBytes(f.used)}/${smBytes(f.total)} (${f.pct}%)`)).join('');
  }
  disk += `</div>`;

  // ── Processes card ──
  let proc = `<div class="sm-card wide"><h4>Processes <span class="sm-sub">${d.top.length} shown</span></h4>`;
  proc += `<div class="sm-proc-ctl">`
    + `<input id="sm-filter" placeholder="filter…" value="${esc(sysmonFilter)}" oninput="sysmonOnFilter(this.value)" autocomplete="off" autocapitalize="off" spellcheck="false">`
    + `<button class="sm-sort-btn ${sysmonSort==='cpu'?'active':''}" onclick="sysmonSetSort('cpu')">CPU</button>`
    + `<button class="sm-sort-btn ${sysmonSort==='mem'?'active':''}" onclick="sysmonSetSort('mem')">MEM</button></div>`;
  proc += renderProcTable(d.top);
  proc += `</div>`;

  document.getElementById('sysmon-body').innerHTML = cpu + gpu + mem + net + disk + proc;
}

function renderProcTable(rows) {
  let list = rows.slice();
  const f = sysmonFilter.trim().toLowerCase();
  if (f) list = list.filter(p => (p.cmd || '').toLowerCase().includes(f) || String(p.pid).includes(f) || (p.user||'').toLowerCase().includes(f));
  list.sort((a, b) => (sysmonSort === 'mem' ? b.mem - a.mem : b.cpu - a.cpu));
  let t = `<table class="sm-proc"><thead><tr><th>PID</th><th>USER</th><th class="num">CPU%</th><th class="num">MEM%</th><th class="num">RSS</th><th>COMMAND</th></tr></thead><tbody>`;
  t += list.map(p =>
    `<tr><td>${p.pid}</td><td>${esc(p.user)}</td>`
    + `<td class="num ${p.cpu>=50?'hot':''}">${p.cpu.toFixed(1)}</td>`
    + `<td class="num ${p.mem>=20?'hot':''}">${p.mem.toFixed(1)}</td>`
    + `<td class="num">${smBytes(p.rss)}</td><td class="cmd">${esc(p.cmd)}</td></tr>`).join('');
  t += `</tbody></table>`;
  return t;
}

// Sort/filter re-render the process table from the last snapshot (no refetch).
function sysmonSetSort(s) {
  sysmonSort = s;
  document.querySelectorAll('.sm-sort-btn').forEach(b => b.classList.toggle('active', b.textContent.toLowerCase() === s));
  if (_sysmonLast) document.querySelector('.sm-proc').outerHTML = renderProcTable(_sysmonLast.top);
}
function sysmonOnFilter(v) {
  sysmonFilter = v;
  if (_sysmonLast) document.querySelector('.sm-proc').outerHTML = renderProcTable(_sysmonLast.top);
}
function renderSysmonError(msg) {
  document.getElementById('sysmon-body').innerHTML = `<div class="sm-card wide"><div class="sm-err">monitor unavailable: ${esc(msg)}</div></div>`;
}

async function createNewSession() {
  const go = document.getElementById('newsess-go');
  const name = document.getElementById('newsess-name').value.trim();
  const shell = document.getElementById('newsess-shell').checked;
  go.disabled = true; go.textContent = 'Creating…';
  try {
    const r = await fetch('/api/session/new', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, shell }),
    });
    const d = await r.json();
    if (!r.ok || d.error) { alert('Could not create session: ' + (d.error || r.status)); return; }
    closeNewSession();
    fetchStatus();
    // A bare shell isn't in the claude-filtered list; open it directly either way.
    openTerminal(d.session);
  } catch (e) { alert('create error'); }
  finally { go.disabled = false; go.textContent = 'Create & Open'; }
}

function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ─── Tiled View ───
let tiledActive = false;
let tiledTerminals = {};
let tileXtermPending = new Set();   // A8: tiles whose xterm bundle is still loading (double-connect guard)
let tiledInterval = null;
let prevTiledStates = {};
let tiledSelectedSessions = new Set();
let tiledLayout = 'grid';
let allSessions = []; // cached for session switcher

const XTERM_THEME = {
  background: '#0C0A09', foreground: '#FAFAF9', cursor: '#C2410C',
  black: '#1C1917', red: '#DC2626', green: '#65A30D', yellow: '#D97706',
  blue: '#2563EB', magenta: '#9333EA', cyan: '#0891B2', white: '#A8A29E',
  brightBlack: '#57534E', brightRed: '#EF4444', brightGreen: '#84CC16',
  brightYellow: '#F59E0B', brightBlue: '#3B82F6', brightMagenta: '#A855F7',
  brightCyan: '#06B6D4', brightWhite: '#FAFAF9',
};

// ─── ANSI Color Map ───
const ANSI_COLORS = {
  '30': '#1C1917', '31': '#DC2626', '32': '#65A30D', '33': '#D97706',
  '34': '#2563EB', '35': '#9333EA', '36': '#0891B2', '37': '#A8A29E',
  '90': '#57534E', '91': '#EF4444', '92': '#84CC16', '93': '#F59E0B',
  '94': '#3B82F6', '95': '#A855F7', '96': '#06B6D4', '97': '#FAFAF9',
};
const ANSI_BG_COLORS = {
  '40': '#1C1917', '41': '#DC2626', '42': '#65A30D', '43': '#D97706',
  '44': '#2563EB', '45': '#9333EA', '46': '#0891B2', '47': '#A8A29E',
  '100': '#57534E', '101': '#EF4444', '102': '#84CC16', '103': '#F59E0B',
  '104': '#3B82F6', '105': '#A855F7', '106': '#06B6D4', '107': '#FAFAF9',
};

// 256-color lookup (16-231: 6x6x6 cube, 232-255: grayscale)
function color256(n) {
  if (n < 16) {
    const basic = ['#1C1917','#DC2626','#65A30D','#D97706','#2563EB','#9333EA','#0891B2','#A8A29E',
                   '#57534E','#EF4444','#84CC16','#F59E0B','#3B82F6','#A855F7','#06B6D4','#FAFAF9'];
    return basic[n];
  }
  if (n < 232) {
    const i = n - 16;
    const r = Math.floor(i / 36) * 51;
    const g = Math.floor((i % 36) / 6) * 51;
    const b = (i % 6) * 51;
    return `rgb(${r},${g},${b})`;
  }
  const v = (n - 232) * 10 + 8;
  return `rgb(${v},${v},${v})`;
}

function parseAnsiToSegments(text) {
  const segments = [];
  let fg = null, bg = null, bold = false, italic = false, underline = false, dim = false;
  // Split on ESC[ (real escape char is \x1b = \u001b)
  const parts = text.split('\u001b[');

  // First part has no escape prefix
  if (parts[0]) segments.push({ text: parts[0], fg: null, bg: null, bold: false, italic: false, underline: false, dim: false });

  for (let i = 1; i < parts.length; i++) {
    // Find the CSI terminator (any letter). Only process SGR codes (ending in 'm').
    const termMatch = parts[i].match(/^([0-9;]*)([A-Za-z])/);
    if (!termMatch) { segments.push({ text: parts[i], fg, bg, bold, italic, underline, dim }); continue; }
    if (termMatch[2] !== 'm') {
      // Non-SGR sequence (cursor move, erase, etc.) — skip the code, keep any trailing text
      const rest = parts[i].substring(termMatch[0].length);
      if (rest) segments.push({ text: rest, fg, bg, bold, italic, underline, dim });
      continue;
    }
    const mIdx = termMatch[0].length - 1;
    // mIdx now points to 'm'

    const codes = parts[i].substring(0, mIdx).split(';');
    const rest = parts[i].substring(mIdx + 1);

    for (let j = 0; j < codes.length; j++) {
      const c = codes[j];
      if (c === '0' || c === '') { fg = null; bg = null; bold = false; italic = false; underline = false; dim = false; }
      else if (c === '1') bold = true;
      else if (c === '2') dim = true;
      else if (c === '3') italic = true;
      else if (c === '4') underline = true;
      else if (c === '22') { bold = false; dim = false; }
      else if (c === '23') italic = false;
      else if (c === '24') underline = false;
      else if (c === '39') fg = null;
      else if (c === '49') bg = null;
      else if (ANSI_COLORS[c]) fg = ANSI_COLORS[c];
      else if (ANSI_BG_COLORS[c]) bg = ANSI_BG_COLORS[c];
      else if (c === '38' && codes[j+1] === '5') { fg = color256(parseInt(codes[j+2])); j += 2; }
      else if (c === '48' && codes[j+1] === '5') { bg = color256(parseInt(codes[j+2])); j += 2; }
      else if (c === '38' && codes[j+1] === '2') { fg = `rgb(${codes[j+2]},${codes[j+3]},${codes[j+4]})`; j += 4; }
      else if (c === '48' && codes[j+1] === '2') { bg = `rgb(${codes[j+2]},${codes[j+3]},${codes[j+4]})`; j += 4; }
    }

    if (rest) segments.push({ text: rest, fg, bg, bold, italic, underline, dim });
  }
  return segments;
}

function renderAnsiToHTML(ansiText) {
  const segments = parseAnsiToSegments(ansiText);
  let html = '';
  for (const seg of segments) {
    if (!seg.text) continue;
    let style = '';
    if (seg.fg) style += `color:${seg.fg};`;
    if (seg.bg) style += `background:${seg.bg};`;
    if (seg.bold) style += 'font-weight:700;';
    if (seg.dim) style += 'opacity:0.6;';
    if (seg.italic) style += 'font-style:italic;';
    if (seg.underline) style += 'text-decoration:underline;';
    const escaped = seg.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    html += style ? `<span style="${style}">${escaped}</span>` : escaped;
  }
  return html;
}

// Characters that make up horizontal rules / box borders. A line that is almost
// entirely these is decoration — clip it to one row rather than wrap it into
// several identical-looking rows (the mobile clutter the operator flagged).
const RULE_CHARS = new Set([
  '─','━','│','┃','┄','┅','┆','┇',
  '┈','┉','┊','┋','═','║','╌','╍',
  '╭','╮','╯','╰','╱','╴','╶','╸','╺',
  '├','┤','┬','┴','┼','▀','▁','▔','▔',
  '-','—','–','_','=','·','⎯','•',
]);
function isRuleLine(visible) {
  const t = (visible || '').trim();
  if (t.length < 10) return false;
  let rule = 0, other = 0;
  for (const ch of t) {
    if (ch === ' ') continue;
    if (RULE_CHARS.has(ch)) rule++; else other++;
  }
  return rule >= 8 && other <= Math.max(2, rule * 0.12);
}

// Per-line renderer: splits the styled segments into lines, decides wrap vs
// clip per line, and emits one <div> each. Reuses parseAnsiToSegments so the
// (correct) ANSI decoding isn't duplicated.
// A TUI todo-checklist row (the task list Claude renders above its input box).
const TODO_ROW_RE = /^\s*(?:⎿\s*)?[☐☒☑◻◼✓✔]/;
// The panel's trailer row, e.g. "… +6 completed".
const TODO_TRAILER_RE = /^\s*(?:…|\.{3})?\s*\+\d+\s+completed\b/;
function renderAnsiToLines(ansiText) {
  const segments = parseAnsiToSegments(ansiText);
  const lines = [[]];
  for (const seg of segments) {
    const parts = (seg.text || '').split('\n');
    for (let k = 0; k < parts.length; k++) {
      if (k > 0) lines.push([]);
      if (parts[k]) lines[lines.length - 1].push({ ...seg, text: parts[k] });
    }
  }
  // Materialize rows first so the TUI's bottom chrome (input-box rules, status
  // zone, todo list) can be located before any HTML is emitted.
  const rows = lines.map(line => {
    let visible = '', inner = '';
    for (const seg of line) {
      visible += seg.text;
      let style = '';
      if (seg.fg) style += `color:${seg.fg};`;
      if (seg.bg) style += `background:${seg.bg};`;
      if (seg.bold) style += 'font-weight:700;';
      if (seg.dim) style += 'opacity:0.6;';
      if (seg.italic) style += 'font-style:italic;';
      if (seg.underline) style += 'text-decoration:underline;';
      const esc = seg.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      inner += style ? `<span style="${style}">${esc}</span>` : esc;
    }
    return { visible, inner, cls: isRuleLine(visible) ? 'tline rule' : 'tline' };
  });

  // Locate the input box: the last two rule lines near the bottom of the frame.
  let ruleBot = -1, ruleTop = -1;
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].cls.indexOf('rule') < 0) continue;
    if (ruleBot < 0) { ruleBot = i; } else { ruleTop = i; break; }
  }
  const tuiBottom = ruleBot >= 0 && ruleTop >= 0 && (ruleBot - ruleTop) <= 8 &&
                    (rows.length - 1 - ruleBot) <= 16;

  const skip = new Set();
  let todoSummaryAt = -1, todoCount = 0;
  if (tuiBottom) {
    // 1. STATUS ZONE below the input box (statusline + mode hints): on a phone
    //    these wrap to 5-6 lines. Render the first 3 as single ellipsis-clamped
    //    lines and drop the rest. >5 non-blank rows means a menu/dialog is open
    //    down there — leave that alone.
    const foot = [];
    for (let i = ruleBot + 1; i < rows.length; i++) if (rows[i].visible.trim()) foot.push(i);
    if (foot.length > 0 && foot.length <= 5) {
      for (let i = ruleBot + 1; i < rows.length; i++) {
        if (!rows[i].visible.trim()) { skip.add(i); continue; }
        if (foot.indexOf(i) < 3) rows[i].cls += ' tfoot'; else skip.add(i);
      }
    }
    // 2. TODO LIST: consecutive checkbox rows sitting right above the input box.
    //    Display-side collapse only — the session itself is never touched.
    if (todosHidden) {
      let i = ruleTop - 1;
      while (i >= 0 && !rows[i].visible.trim()) i--;
      const end = i;
      // Optional trailer ("… +6 completed"), then the checkbox rows above it.
      let t = 0;
      while (i >= 0 && t < 2 && TODO_TRAILER_RE.test(rows[i].visible)) { i--; t++; }
      while (i >= 0 && TODO_ROW_RE.test(rows[i].visible)) { todoCount++; i--; }
      if (todoCount > 0) {
        for (let k = i + 1; k <= end; k++) skip.add(k);
        todoSummaryAt = end;
      }
    }
  }

  let html = '';
  for (let i = 0; i < rows.length; i++) {
    if (skip.has(i)) {
      if (i === todoSummaryAt)
        html += `<div class="tline todo-sum">☐ ${todoCount} todo${todoCount > 1 ? 's' : ''} hidden · hold esc</div>`;
      continue;
    }
    html += `<div class="${rows[i].cls}">${rows[i].inner || '&nbsp;'}</div>`;
  }
  return html;
}

function openTiledView() {
  document.getElementById('tile-picker').classList.add('open');
  fetch('/api/status').then(r => r.json()).then(data => {
    allSessions = data.sessions.filter(s => s.command === 'claude').sort(favStateSort);
    const saved = JSON.parse(localStorage.getItem('clanker_tiled_sessions') || '[]');
    const list = document.getElementById('picker-list');
    list.innerHTML = allSessions.map(s => {
      const checked = saved.length === 0 || saved.includes(s.session) ? 'checked' : '';
      return `<label class="picker-item"><input type="checkbox" value="${s.session}" ${checked}>
        ${favStar(s.session)}
        <span class="picker-name">${s.session}</span>
        <span class="picker-badge ${s.state}">${s.state}</span></label>`;
    }).join('');
  });
}

function launchTiledView() {
  const selected = Array.from(document.querySelectorAll('#picker-list input:checked')).map(c => c.value);
  localStorage.setItem('clanker_tiled_sessions', JSON.stringify(selected));
  document.getElementById('tile-picker').classList.remove('open');
  tiledActive = true;
  tiledSelectedSessions = new Set(selected);
  document.getElementById('tiled-overlay').classList.add('open');
  document.getElementById('tiled-grid').innerHTML = '';
  tiledLayout = localStorage.getItem('clanker_tile_layout_mode') || 'grid';
  updateLayoutButtons();
  fetchTiledPanes();
  tiledInterval = setInterval(() => { if (!document.hidden) fetchTiledPanes(); }, 2000);
}

function closeTiledView() {
  tiledActive = false;
  document.getElementById('tiled-overlay').classList.remove('open');
  if (tiledInterval) { clearInterval(tiledInterval); tiledInterval = null; }
  Object.keys(tiledTerminals).forEach(s => disconnectTile(s));
  tiledTerminals = {};
  tileXtermPending.clear();
  prevTiledStates = {};
}

async function fetchTiledPanes() {
  try {
    // A3: /api/panes items already carry {session,target,state,content,width,height},
    // so the extra /api/status round-trip is gone. Pass the visible tiles so the
    // server only runs the expensive capture-pane on those panes; every claude pane
    // is still returned (with its cheap state) so the tile switcher list stays whole.
    const sel = [...tiledSelectedSessions];
    const q = sel.length ? ('?sessions=' + sel.map(encodeURIComponent).join(',')) : '';
    const r = await fetch('/api/panes' + q);
    if (!r.ok) return;
    const panes = await r.json();
    allSessions = panes;   // {session,state,…} — shape-compatible with the picker/switcher
    renderTiles(panes);
  } catch(e) {}
}

function renderTiles(panes) {
  const grid = document.getElementById('tiled-grid');
  const filtered = panes.filter(p => tiledSelectedSessions.has(p.session));

  filtered.forEach(p => {
    let tile = document.getElementById('tile-' + p.session);
    if (!tile) {
      tile = document.createElement('div');
      tile.id = 'tile-' + p.session;
      tile.className = 'tile';
      tile.dataset.session = p.session;
      tile.innerHTML = buildTileHTML(p);
      grid.appendChild(tile);
    }
    // Update badge
    const badge = tile.querySelector('.tile-badge');
    if (badge) { badge.className = 'tile-badge ' + p.state; badge.textContent = p.state; }
    // Flash on waiting transition
    const prev = prevTiledStates[p.session];
    if (p.state === 'waiting' && prev === 'working') {
      tile.classList.add('flash');
      playChime();
      if ('Notification' in window && Notification.permission === 'granted')
        new Notification(p.session + ' needs input', { tag: 'tile-' + p.session, renotify: true });
    } else if (p.state !== 'waiting') { tile.classList.remove('flash'); }
    prevTiledStates[p.session] = p.state;
    // Update monitor
    if (!tiledTerminals[p.session]) {
      const pre = tile.querySelector('.tile-monitor');
      if (pre) pre.textContent = p.content;
    }
    const btn = tile.querySelector('.tile-connect');
    if (btn) btn.textContent = tiledTerminals[p.session] ? 'disconnect' : 'connect';
    // Update select value
    const sel = tile.querySelector('.tile-select');
    if (sel && sel.value !== p.session) sel.value = p.session;
  });

  // Apply layout after first render
  if (grid.children.length > 0) applyLayout(tiledLayout);
  // Keep connected terminals fitted as the grid updates (fit() is a no-op if unchanged).
  Object.values(tiledTerminals).forEach(t => { try { if (t.fitAddon) t.fitAddon.fit(); } catch (e) {} });
}

function buildTileHTML(p) {
  const opts = allSessions.map(s =>
    `<option value="${s.session}" ${s.session === p.session ? 'selected' : ''}>${s.session} [${s.state}]</option>`
  ).join('');
  return `<div class="tile-header">
      <select class="tile-select" onchange="switchTileSession('${p.session}', this.value, this)" onmousedown="event.stopPropagation()">${opts}</select>
      <span class="tile-badge ${p.state}">${p.state}</span>
      <button class="tile-connect" onclick="toggleTileTerminal('${p.session}')">connect</button>
    </div>
    <div class="tile-body" id="tile-body-${p.session}">
      <pre class="tile-monitor">${escapeHtml(p.content)}</pre>
    </div>`;
}

// ─── Layouts (tiling WM style) ───
function setLayout(mode) {
  tiledLayout = mode;
  localStorage.setItem('clanker_tile_layout_mode', mode);
  updateLayoutButtons();
  applyLayout(mode);
  // Refit terminals
  setTimeout(() => Object.values(tiledTerminals).forEach(t => { if (t.fitAddon) t.fitAddon.fit(); }), 100);
}

function updateLayoutButtons() {
  document.querySelectorAll('.layout-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.layout === tiledLayout);
  });
}

function applyLayout(mode) {
  const grid = document.getElementById('tiled-grid');
  const tiles = Array.from(grid.querySelectorAll('.tile'));
  if (!tiles.length) return;
  const n = tiles.length;
  const cw = grid.clientWidth;
  const ch = grid.clientHeight;
  const gap = 2;

  // Reset all tile styles
  tiles.forEach(t => { t.style.cssText = ''; });

  switch(mode) {
    case 'grid': {
      const cols = Math.ceil(Math.sqrt(n * (cw / ch)));
      const rows = Math.ceil(n / cols);
      grid.style.display = 'grid';
      grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
      grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;
      grid.style.gap = gap + 'px';
      tiles.forEach(t => { t.style.minHeight = '0'; t.style.minWidth = '0'; });
      break;
    }
    case 'master': {
      grid.style.display = 'grid';
      grid.style.gap = gap + 'px';
      if (n === 1) {
        grid.style.gridTemplateColumns = '1fr';
        grid.style.gridTemplateRows = '1fr';
      } else {
        grid.style.gridTemplateColumns = '3fr 2fr';
        grid.style.gridTemplateRows = `repeat(${n - 1}, 1fr)`;
        tiles[0].style.gridRow = `1 / ${n}`;
      }
      tiles.forEach(t => { t.style.minHeight = '0'; t.style.minWidth = '0'; });
      break;
    }
    case 'columns': {
      grid.style.display = 'grid';
      grid.style.gridTemplateColumns = `repeat(${n}, 1fr)`;
      grid.style.gridTemplateRows = '1fr';
      grid.style.gap = gap + 'px';
      tiles.forEach(t => { t.style.minHeight = '0'; t.style.minWidth = '0'; });
      break;
    }
    case 'rows': {
      grid.style.display = 'grid';
      grid.style.gridTemplateColumns = '1fr';
      grid.style.gridTemplateRows = `repeat(${n}, 1fr)`;
      grid.style.gap = gap + 'px';
      tiles.forEach(t => { t.style.minHeight = '0'; t.style.minWidth = '0'; });
      break;
    }
    case 'focus': {
      grid.style.display = 'grid';
      grid.style.gap = gap + 'px';
      if (n === 1) {
        grid.style.gridTemplateColumns = '1fr';
        grid.style.gridTemplateRows = '1fr';
      } else {
        grid.style.gridTemplateColumns = '1fr';
        grid.style.gridTemplateRows = `1fr 120px`;
        tiles[0].style.gridColumn = '1';
        tiles[0].style.gridRow = '1';
        // Remaining tiles in a horizontal strip at the bottom
        const sub = document.createElement('div');
        sub.className = 'focus-strip';
        sub.style.cssText = `display:flex;gap:${gap}px;grid-column:1;grid-row:2;overflow-x:auto;`;
        tiles.slice(1).forEach(t => {
          t.style.minWidth = '250px';
          t.style.minHeight = '0';
          t.style.flex = '1';
          sub.appendChild(t);
        });
        grid.appendChild(sub);
      }
      break;
    }
  }
}

function resetTileLayout() {
  localStorage.removeItem('clanker_tile_layout_mode');
  tiledLayout = 'grid';
  updateLayoutButtons();
  applyLayout('grid');
  setTimeout(() => Object.values(tiledTerminals).forEach(t => { if (t.fitAddon) t.fitAddon.fit(); }), 100);
}

// ─── Session Switcher (select dropdown) ───
function switchTileSession(oldSession, newSession, selectEl) {
  if (oldSession === newSession) return;
  disconnectTile(oldSession);
  const tile = document.getElementById('tile-' + oldSession);
  if (!tile) return;

  tile.id = 'tile-' + newSession;
  tile.dataset.session = newSession;
  const body = tile.querySelector('.tile-body');
  if (body) { body.id = 'tile-body-' + newSession; body.innerHTML = '<pre class="tile-monitor">Loading...</pre>'; }
  // Update connect button onclick
  const btn = tile.querySelector('.tile-connect');
  if (btn) btn.setAttribute('onclick', "toggleTileTerminal('" + newSession + "')");
  // Update select onchange
  if (selectEl) selectEl.setAttribute('onchange', "switchTileSession('" + newSession + "', this.value, this)");
  tiledSelectedSessions.delete(oldSession);
  tiledSelectedSessions.add(newSession);
}

// ─── Terminal Connect/Disconnect ───
function toggleTileTerminal(session) {
  tiledTerminals[session] ? disconnectTile(session) : connectTile(session);
}

function sendKey(ws, key) { ws.send(JSON.stringify({ type: 'key', data: key })); }
function sendKeys(ws, text) { ws.send(JSON.stringify({ type: 'keys', data: text })); }

function setupKeyboardInput(el, ws, session) {
  // Hidden textarea for mobile keyboard capture
  const ta = document.createElement('textarea');
  ta.className = 'terminal-input-capture';
  ta.setAttribute('autocomplete', 'off');
  ta.setAttribute('autocapitalize', 'off');
  ta.setAttribute('autocorrect', 'off');
  ta.setAttribute('spellcheck', 'false');
  el.parentNode.appendChild(ta);

  // Desktop: keydown on the pre element
  const keyHandler = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    e.preventDefault();
    if (e.key === 'Enter') sendKey(ws, 'Enter');
    else if (e.key === 'Backspace') sendKey(ws, 'BSpace');
    else if (e.key === 'Tab') sendKey(ws, 'Tab');
    else if (e.key === 'Escape') {
      if (e.shiftKey && session) { closeTiledView(); return; }
      sendKey(ws, 'Escape');
    }
    else if (e.key === 'ArrowUp') sendKey(ws, 'Up');
    else if (e.key === 'ArrowDown') sendKey(ws, 'Down');
    else if (e.key === 'ArrowLeft') sendKey(ws, 'Left');
    else if (e.key === 'ArrowRight') sendKey(ws, 'Right');
    else if (e.key === 'Home') sendKey(ws, 'Home');
    else if (e.key === 'End') sendKey(ws, 'End');
    else if (e.key === 'PageUp') sendKey(ws, 'PPage');
    else if (e.key === 'PageDown') sendKey(ws, 'NPage');
    else if (e.ctrlKey && e.key === 'c') sendKey(ws, 'C-c');
    else if (e.ctrlKey && e.key === 'd') sendKey(ws, 'C-d');
    else if (e.ctrlKey && e.key === 'z') sendKey(ws, 'C-z');
    else if (e.ctrlKey && e.key === 'l') sendKey(ws, 'C-l');
    else if (e.ctrlKey && e.key === 'a') sendKey(ws, 'C-a');
    else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
      sendKeys(ws, e.key);
    }
  };
  el.addEventListener('keydown', keyHandler);

  // Mobile: textarea input event (captures on-screen keyboard)
  ta.addEventListener('input', () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const text = ta.value;
    if (text) { sendKeys(ws, text); ta.value = ''; }
  });
  ta.addEventListener('keydown', (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (e.key === 'Enter') { e.preventDefault(); sendKey(ws, 'Enter'); ta.value = ''; }
    else if (e.key === 'Backspace' && !ta.value) { e.preventDefault(); sendKey(ws, 'BSpace'); }
  });

  // Tap on terminal → focus hidden textarea (shows mobile keyboard)
  el.addEventListener('click', () => {
    if ('ontouchstart' in window) ta.focus();
    else el.focus();
  });

  return { keyHandler, textarea: ta };
}

function connectTile(session) {
  const body = document.getElementById('tile-body-' + session);
  if (!body || tiledTerminals[session] || tileXtermPending.has(session)) return;
  // A8: lazy-load xterm on the first tile connect (same vendor bundle as the
  // full-screen terminal). Placeholder while it loads; the pending set prevents a
  // double-connect racing the async load.
  tileXtermPending.add(session);
  body.innerHTML = '<pre class="tile-monitor">loading terminal…</pre>';
  ensureXterm().then(() => {
    tileXtermPending.delete(session);
    const body2 = document.getElementById('tile-body-' + session);
    if (!body2 || tiledTerminals[session]) return;   // tile removed/switched, or already connected
    body2.innerHTML = '';

    // Real xterm.js terminal per tile, over the PTY bridge (same as the full-screen one).
    const term = new Terminal({
      theme: XTERM_THEME, fontFamily: 'JetBrains Mono, ui-monospace, monospace',
      fontSize: 11, cursorBlink: false, scrollback: 2000, allowProposedApi: true,
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(body2);
    try {
      const gl = new WebglAddon.WebglAddon();
      gl.onContextLoss(() => { try { gl.dispose(); } catch (e) {} });
      term.loadAddon(gl);
    } catch (e) {}
    try { fitAddon.fit(); } catch (e) {}

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/terminal/${session}`);
    ws.binaryType = 'arraybuffer';

    function sendResize() {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
    }

    ws.onopen = () => {
      const btn = document.querySelector('#tile-' + session + ' .tile-connect');
      if (btn) btn.textContent = 'disconnect';
      try { fitAddon.fit(); } catch (e) {}
      sendResize();
    };
    ws.onmessage = (e) => { term.write(typeof e.data === 'string' ? e.data : new Uint8Array(e.data)); };
    ws.onclose = () => {
      const btn = document.querySelector('#tile-' + session + ' .tile-connect');
      if (btn) btn.textContent = 'connect';
    };
    term.onData(d => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'input', data: d })); });
    term.onResize(() => sendResize());

    tiledTerminals[session] = { ws, term, fitAddon };
  }).catch(() => {
    tileXtermPending.delete(session);
    const body2 = document.getElementById('tile-body-' + session);
    if (body2) body2.innerHTML = '<pre class="tile-monitor">terminal failed to load</pre>';
  });
}

function disconnectTile(session) {
  const t = tiledTerminals[session];
  if (!t) return;
  if (t.ws && t.ws.readyState <= 1) { try { t.ws.close(); } catch (e) {} }
  if (t.term) { try { t.term.dispose(); } catch (e) {} }
  delete tiledTerminals[session];
  const body = document.getElementById('tile-body-' + session);
  if (body) body.innerHTML = '<pre class="tile-monitor">Disconnected</pre>';
  const btn = document.querySelector('#tile-' + session + ' .tile-connect');
  if (btn) btn.textContent = 'connect';
}

async function connectAllTiles() {
  for (const tile of document.querySelectorAll('.tile')) {
    const session = tile.dataset.session;
    if (session && !tiledTerminals[session]) {
      connectTile(session);
      await new Promise(r => setTimeout(r, 600));
    }
  }
}

function disconnectAllTiles() {
  Object.keys(tiledTerminals).forEach(s => disconnectTile(s));
}

function playChime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.setValueAtTime(1100, ctx.currentTime + 0.1);
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 0.3);
  } catch(e) {}
}

// Inject button
(function() {
  const h2 = document.querySelector('[data-label="LIVE"] h2');
  const btn = 'float:right;font-family:var(--font-mono);font-size:10px;padding:4px 12px;background:var(--bg-surface);color:var(--text-secondary);border:1px solid var(--border);cursor:pointer;text-transform:uppercase;letter-spacing:0.1em';
  const btnNew = btn + ';background:var(--accent-terracotta);color:var(--accent-cream);border-color:var(--accent-terracotta)';
  if (h2) h2.innerHTML += ` <button onclick="openNewSession()" style="${btnNew}">+ New Session</button> <button onclick="openTiledView()" style="${btn}">Tiled View</button> <button onclick="openOrch()" style="${btn}">Orchestration</button>`;
})();
let orchInterval = null;
const ORCH_TOGGLES = [['enabled','Orchestration',true],['auto_nudge','Auto-nudge',false],['auto_spawn','Auto-spawn',false],['auto_merge','Auto-merge',false]];
function openOrch() { document.getElementById('orch-overlay').classList.add('open'); orchRefresh(); orchInterval = setInterval(() => { if (!document.hidden) orchRefresh(); }, 4000); }
function closeOrch() { document.getElementById('orch-overlay').classList.remove('open'); if (orchInterval) { clearInterval(orchInterval); orchInterval = null; } }
async function orchRefresh() {
  try { const r = await fetch('/api/orch'); if (!r.ok) return; const d = await r.json();
    if (!d.available) { document.getElementById('orch-toggles').innerHTML = '<span class="orch-hint">orchestration package unavailable</span>'; return; }
    renderOrch(d); } catch(e) {}
}
function renderOrch(d) {
  const cfg = d.config || {};
  document.getElementById('orch-toggles').innerHTML = ORCH_TOGGLES.map(([k,label,master]) => {
    const on = !!cfg[k];
    return `<span class="orch-toggle ${master?'master':''} ${on?'on':''}" onclick="orchSet('${k}', ${!on})">${label}: ${on?'ON':'off'}</span>`;
  }).join('')
    + ` <span class="orch-toggle">max <input class="orch-num" type="number" min="1" max="32" value="${cfg.max_parallel||4}" onchange="orchSet('max_parallel', parseInt(this.value)||4)"></span>`
    + ` <span class="orch-toggle">nudge risk≤ <select class="orch-num" style="width:auto" onchange="orchSet('nudge_risk_max', this.value)">${['allow','review','confirm','block'].map(o=>`<option ${cfg.nudge_risk_max===o?'selected':''}>${o}</option>`).join('')}</select></span>`;
  document.getElementById('orch-hint').textContent = cfg.enabled
    ? (cfg.auto_nudge ? 'Auto-nudge ON: routine waiting sessions are auto-continued (risk-gated; never on confirm/block actions).' : 'Read-only supervision. Turn on auto-nudge / auto-spawn / auto-merge to let it act.')
    : 'Orchestration is OFF. Turn it on for read-only supervision; the act-on-sessions toggles stay off until you enable them.';
  const sessions = (d.sessions||[]).filter(s => ['pending','running','waiting','idle','stale'].includes(s.state));
  document.getElementById('orch-fleet').innerHTML = sessions.length ? sessions.map(s => `
    <div class="orch-row"><span class="session-badge ${s.state}" style="font-size:9px;padding:2px 7px">${s.state}</span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.id.slice(0,8)} · ${s.project||'-'} · ${(s.task||'').slice(0,60)}</span>
      <button class="orch-toggle" onclick="orchStop('${s.id}')">stop</button></div>`).join('') : '<div class="orch-hint">no active sessions</div>';
  const bl = d.backlog||[];
  document.getElementById('orch-backlog').innerHTML = bl.length ? bl.map(b => `<div class="orch-row"><span style="flex:1">${(b.task||'').slice(0,70)}</span><span class="orch-hint">${b.project||''}</span></div>`).join('') : '<div class="orch-hint">empty</div>';
}
async function orchSet(key, val) { try { const r = await fetch('/api/orch/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({[key]: val})}); if (r.ok) orchRefresh(); } catch(e) {} }
async function orchSpawn() {
  const task = document.getElementById('orch-task').value.trim(); if (!task) return;
  const project = document.getElementById('orch-project').value.trim();
  const headless = document.getElementById('orch-headless').checked;
  try { const r = await fetch('/api/orch/spawn', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({task, project, headless})});
    const d = await r.json(); if (d.error) alert('Spawn failed: ' + d.error); else { document.getElementById('orch-task').value=''; orchRefresh(); }
  } catch(e) { alert('spawn error'); }
}
async function orchStop(id) { try { await fetch('/api/orch/session/'+id+'/stop', {method:'POST'}); orchRefresh(); } catch(e) {} }
