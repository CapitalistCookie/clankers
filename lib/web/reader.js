// reader.js — Reader mode: the mobile flagship (2026-07-19).
// Renders the session TRANSCRIPT as real HTML (markdown via marked +
// DOMPurify, both vendored) instead of re-rendering terminal frames — a
// 107-col TUI table cannot reflow on a 45-col phone, but its markdown source
// can. TERMINAL-FAITHFUL styling (operator, 2026-07-19): same palette/mono
// font as the xterm theme, Claude Code's own vocabulary ('>' prompts, '⏺'
// tool lines, '⎿' results) — it should read like your tmux pane, just
// reflowed. The LIVE STATUS LINE is extracted from the /ws/view frames the
// input socket already streams and pinned above the compose bar with its
// real ANSI colors (rendered via live.js's renderAnsiToLines).
//
// Data: GET /api/reader/<session>?offset=N -> {units, offset, reset} —
// incremental byte-offset polling (3s), server bounds the tail window.

(function () {
  'use strict';

  // Palette mirrors XTERM_THEME in live.js — the pane and the reader must
  // not look like two different apps.
  const CSS = `
  .reader-wrap { height: 100%; display: flex; flex-direction: column;
    background: #0C0A09; }
  .reader-feed { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 8px 12px; font-family: var(--font-mono); font-size: 12px;
    line-height: 1.35; color: #FAFAF9; }
  @media (min-width: 769px) { .reader-feed { font-size: 14px; } }
  .reader-feed .r-user { margin: 14px 0 6px; white-space: pre-wrap;
    word-break: break-word; color: #FAFAF9; }
  .reader-feed .r-user::before { content: "> "; color: #C2410C; font-weight: 700; }
  .reader-feed .r-asst { word-break: break-word; }
  .reader-feed .r-asst > *:first-child { margin-top: 4px; }
  .reader-feed .r-asst p { margin: 5px 0; }
  .reader-feed .r-asst p::before { content: "⏺ "; color: #57534E; }
  .reader-feed .r-asst li::marker { color: #57534E; }
  .reader-feed .r-asst pre { background: #1C1917; border: 1px solid #292524;
    padding: 7px 9px; margin: 6px 0; overflow-x: auto; font-size: 12px;
    font-family: inherit; }
  .reader-feed .r-asst pre p::before, .reader-feed .r-asst li p::before,
  .reader-feed .r-asst th p::before, .reader-feed .r-asst td p::before,
  .reader-feed .r-asst blockquote p::before { content: none; }
  .reader-feed .r-asst code { background: #1C1917; padding: 0 3px;
    font-family: inherit; font-size: inherit; color: #F59E0B; }
  .reader-feed .r-asst pre code { background: none; padding: 0; color: #FAFAF9; }
  .reader-feed .r-asst table { border-collapse: collapse; display: block;
    overflow-x: auto; max-width: 100%; margin: 6px 0; font-size: 12px; }
  .reader-feed .r-asst th, .reader-feed .r-asst td { border: 1px solid #292524;
    padding: 3px 7px; white-space: normal; word-break: break-word;
    overflow-wrap: anywhere; }
  .reader-feed .r-asst th { color: #A8A29E; }
  .reader-feed .r-asst h1, .reader-feed .r-asst h2, .reader-feed .r-asst h3,
  .reader-feed .r-asst h4 { font-size: 12.5px; font-weight: 700; color: #F59E0B;
    margin: 10px 0 3px; }
  .reader-feed .r-asst ul, .reader-feed .r-asst ol { padding-left: 20px; margin: 4px 0; }
  .reader-feed .r-asst a { color: #0891B2; }
  .reader-feed .r-asst blockquote { border-left: 2px solid #57534E; margin: 5px 0;
    padding-left: 9px; color: #A8A29E; }
  .reader-feed .r-asst hr { border: 0; border-top: 1px solid #292524; margin: 8px 0; }
  .reader-feed .r-asst strong { color: #FAFAF9; }
  .reader-feed .r-tools { margin: 3px 0; }
  .reader-feed .r-tool { color: #A8A29E; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }
  .reader-feed .r-tool .tn { color: #FAFAF9; }
  .reader-feed .r-tool::before { content: "⏺ "; color: #65A30D; }
  .reader-feed .r-err { color: #EF4444; margin: 2px 0 2px 14px; }
  .reader-feed .r-err::before { content: "⎿ ✗ "; }
  .reader-feed .r-note { color: #57534E; text-align: center; padding: 14px 0; }
  .reader-status { flex-shrink: 0; border-top: 1px solid #292524;
    background: #0C0A09; padding: 3px 10px 4px;
    font-family: var(--font-mono); font-size: 11px; line-height: 1.4;
    color: #A8A29E; white-space: pre-wrap; word-break: break-word; }
  @media (min-width: 769px) { .reader-status { font-size: 12.5px; } }
  .reader-status:empty { display: none; }
  /* Tool results (11a): one collapsed ⎿ line per body; tap to expand full text. */
  .reader-feed .r-result { margin: 3px 0; }
  .reader-feed .r-res { margin: 2px 0 2px 14px; cursor: pointer; }
  .reader-feed .r-res-head { color: #78716C; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }
  .reader-feed .r-res-head::before { content: "⎿ "; color: #57534E; }
  .reader-feed .r-res.err .r-res-head { color: #EF4444; }
  .reader-feed .r-res.err .r-res-head::before { content: "⎿ ✗ "; color: #EF4444; }
  .reader-feed .r-res.open .r-res-head { white-space: normal; overflow: visible; }
  .reader-feed .r-res-full { display: none; background: #1C1917; border: 1px solid #292524;
    padding: 6px 8px; margin: 3px 0 4px; white-space: pre-wrap; word-break: break-word;
    color: #D6D3D1; font-family: inherit; font-size: inherit; overflow-x: auto; }
  .reader-feed .r-res.open .r-res-full { display: block; }
  /* In-session search (11b): a 🔍 toggle at the feed top, dark/mono like the pane. */
  .reader-search { flex-shrink: 0; display: flex; align-items: center;
    justify-content: flex-end; gap: 6px; padding: 3px 8px; background: #0C0A09;
    border-bottom: 1px solid #1C1917; }
  .reader-search .rs-toggle { background: none; border: 0; color: #A8A29E;
    font-size: 14px; cursor: pointer; padding: 2px 4px; line-height: 1; }
  .reader-search.open .rs-toggle { color: #F59E0B; }
  .reader-search .rs-panel { display: none; flex: 1; align-items: center; gap: 6px; }
  .reader-search.open .rs-panel { display: flex; }
  .reader-search .rs-input { flex: 1; min-width: 0; background: #1C1917;
    border: 1px solid #292524; color: #FAFAF9; font-family: var(--font-mono);
    font-size: 12px; padding: 3px 6px; }
  .reader-search .rs-input:focus { outline: none; border-color: #C2410C; }
  .reader-search .rs-count { color: #78716C; font-size: 11px; white-space: nowrap; }
  .reader-search .rs-panel button { background: #1C1917; border: 1px solid #292524;
    color: #A8A29E; font-family: var(--font-mono); font-size: 12px; cursor: pointer;
    padding: 2px 7px; line-height: 1.2; }
  .reader-search .rs-panel button:hover { border-color: #C2410C; color: #FAFAF9; }
  .reader-feed .r-hit { background: #C2410C33; }
  .reader-feed .r-hit.r-hit-cur { box-shadow: inset 0 0 0 1px #C2410C; }
  `;

  let feedEl = null, statusEl = null, pollTimer = null, offset = null;
  let stickBottom = true, emptyPolls = 0, lastStatus = '';
  // Backward pagination state (full-session scrollback, 2026-07-19):
  // earliest = byte offset of the first loaded record; scroll-to-top pages
  // back window by window until the session's very first message.
  let earliest = null, atStart = false, loadingOlder = false, readerName = null;
  // In-session search (11b) state: the toggle bar, its input, the match-count
  // label, the current match list and cursor.
  let searchBar = null, searchInput = null, searchCount = null;
  let searchHits = [], searchIdx = -1, searchOpen = false;

  function ensureStyles() {
    if (document.getElementById('reader-styles')) return;
    const s = document.createElement('style');
    s.id = 'reader-styles';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function esc(t) {
    const d = document.createElement('div');
    d.textContent = t == null ? '' : String(t);
    return d.innerHTML;
  }

  function renderMd(text) {
    try {
      if (window.marked && window.DOMPurify) {
        return DOMPurify.sanitize(marked.parse(text, { breaks: false }));
      }
    } catch (e) { /* fall through to escaped plaintext */ }
    return '<p>' + esc(text) + '</p>';
  }

  function unitNode(u) {
    const wrap = document.createElement('div');
    if (u.role === 'user') {
      wrap.className = 'r-user';
      wrap.textContent = u.text;
    } else if (u.role === 'result') {
      // One collapsed ⎿ line per tool_result body (11a), Claude Code-faithful:
      // dim first line, red ✗ on error. Tap toggles a full <pre>, built lazily.
      wrap.className = 'r-result';
      const bodies = (u.bodies && u.bodies.length) ? u.bodies
        : [{ ok: !u.errors, text: (u.errors || 1) + ' tool error(s)' }];
      bodies.forEach(b => {
        const row = document.createElement('div');
        row.className = 'r-res' + (b.ok ? '' : ' err');
        const full = b.text == null ? '' : String(b.text);
        const head = document.createElement('div');
        head.className = 'r-res-head';
        head.textContent = full.split('\n')[0] || (b.ok ? '(no output)' : 'error');
        row.appendChild(head);
        let built = false;
        row.addEventListener('click', () => {
          if (!built) {                       // lazy: build the <pre> on first tap
            const pre = document.createElement('pre');
            pre.className = 'r-res-full';
            pre.textContent = full;
            row.appendChild(pre);
            built = true;
          }
          row.classList.toggle('open');
        });
        wrap.appendChild(row);
      });
    } else {
      wrap.className = 'r-asst';
      if (u.text) wrap.innerHTML = renderMd(u.text);
      if (u.tools && u.tools.length) {
        const block = document.createElement('div');
        block.className = 'r-tools';
        u.tools.forEach(t => {
          // Claude Code's own shape: ⏺ Bash(pytest tests/ -q)
          const line = document.createElement('div');
          line.className = 'r-tool';
          const name = document.createElement('span');
          name.className = 'tn';
          name.textContent = t.name;
          line.appendChild(name);
          line.appendChild(document.createTextNode(
            t.detail ? '(' + t.detail + ')' : ''));
          line.title = t.detail || t.name;
          block.appendChild(line);
        });
        wrap.appendChild(block);
      }
    }
    return wrap;
  }

  function append(units) {
    if (!feedEl || !units.length) return;
    const nearBottom = feedEl.scrollHeight - feedEl.scrollTop - feedEl.clientHeight < 40;
    units.forEach(u => feedEl.appendChild(unitNode(u)));
    // Bound the DOM only while following the live tail — a reader paged up
    // into history keeps everything they loaded (cap is generous; trimming
    // the top would eat the history they scrolled up for).
    if (nearBottom || stickBottom) {
      while (feedEl.childNodes.length > 3000) feedEl.removeChild(feedEl.firstChild);
      feedEl.scrollTop = feedEl.scrollHeight;
    }
    stickBottom = false;
  }

  async function loadOlder() {
    if (loadingOlder || atStart || earliest == null || earliest <= 0 || !feedEl) return;
    loadingOlder = true;
    const marker = document.createElement('div');
    marker.className = 'r-note';
    marker.textContent = 'loading earlier…';
    feedEl.insertBefore(marker, feedEl.firstChild);
    try {
      const r = await fetch('/api/reader/' + encodeURIComponent(readerName)
        + '?before=' + earliest);
      if (!r.ok) return;
      const data = await r.json();
      marker.remove();
      const prevH = feedEl.scrollHeight;
      const frag = document.createDocumentFragment();
      (data.units || []).forEach(u => frag.appendChild(unitNode(u)));
      if (data.at_start) {
        const top = document.createElement('div');
        top.className = 'r-note';
        top.textContent = '· session start ·';
        frag.insertBefore(top, frag.firstChild);
      }
      feedEl.insertBefore(frag, feedEl.firstChild);
      // keep the viewport anchored on what the user was reading
      feedEl.scrollTop += feedEl.scrollHeight - prevH;
      earliest = data.start;
      atStart = !!data.at_start;
    } catch (e) {
      marker.remove();
    } finally {
      loadingOlder = false;
    }
  }

  // ── In-session search (11b): case-insensitive substring over the textContent
  // of LOADED units. Matches get .r-hit; prev/next step between them; 'earlier'
  // pages history backward via loadOlder() up to 15 windows looking for older
  // matches. Fully client-side — no server search endpoint. ──
  function scrollToHit() {
    searchHits.forEach((n, i) => n.classList.toggle('r-hit-cur', i === searchIdx));
    const n = searchHits[searchIdx];
    if (n && n.scrollIntoView) n.scrollIntoView({ block: 'center' });
  }
  function updateSearchCount() {
    if (!searchCount) return;
    const n = searchHits.length;
    const q = searchInput && searchInput.value.trim();
    searchCount.textContent = n ? ((searchIdx + 1) + '/' + n + ' matches')
      : (q ? '0 matches' : '');
  }
  // Re-scan the feed for the current query, (re)applying highlights. `scroll`
  // moves the viewport to the active match (skipped while paging older history).
  function applySearch(scroll) {
    if (!feedEl || !searchInput) return;
    const q = searchInput.value.trim().toLowerCase();
    searchHits.forEach(n => n.classList.remove('r-hit', 'r-hit-cur'));
    searchHits = [];
    if (q) {
      for (const n of feedEl.children) {
        if (n.classList && n.classList.contains('r-note')) continue;   // skip markers
        if ((n.textContent || '').toLowerCase().indexOf(q) !== -1) {
          n.classList.add('r-hit');
          searchHits.push(n);
        }
      }
    }
    if (searchIdx >= searchHits.length) searchIdx = searchHits.length - 1;
    if (searchIdx < 0 && searchHits.length) searchIdx = 0;
    if (scroll && searchIdx >= 0) scrollToHit();
    else searchHits.forEach((n, i) => n.classList.toggle('r-hit-cur', i === searchIdx));
    updateSearchCount();
  }
  function searchStep(dir) {
    if (!searchHits.length) return;
    searchIdx = (searchIdx + dir + searchHits.length) % searchHits.length;
    scrollToHit();
    updateSearchCount();
  }
  async function searchEarlier() {
    if (!searchInput || !searchInput.value.trim()) return;
    const before = searchHits.length;
    for (let i = 0; i < 15; i++) {
      if (atStart || earliest == null || earliest <= 0) break;
      await loadOlder();
      applySearch(false);
      if (searchHits.length > before) break;   // older matches appeared — stop
    }
    if (searchHits.length) { searchIdx = 0; scrollToHit(); }
    updateSearchCount();
  }
  function toggleSearch(force) {
    searchOpen = force == null ? !searchOpen : !!force;
    if (searchBar) searchBar.classList.toggle('open', searchOpen);
    if (searchOpen) { if (searchInput) searchInput.focus(); }
    else if (searchInput) { searchInput.value = ''; searchIdx = -1; applySearch(false); }
  }
  function buildSearchBar() {
    const bar = document.createElement('div');
    bar.className = 'reader-search';
    const toggle = document.createElement('button');
    toggle.className = 'rs-toggle'; toggle.type = 'button';
    toggle.textContent = '🔍'; toggle.title = 'Search transcript';
    toggle.addEventListener('click', () => toggleSearch());
    const panel = document.createElement('div');
    panel.className = 'rs-panel';
    const input = document.createElement('input');
    input.className = 'rs-input'; input.type = 'text';
    input.placeholder = 'search loaded transcript…';
    input.setAttribute('autocomplete', 'off'); input.setAttribute('autocapitalize', 'off');
    input.setAttribute('autocorrect', 'off'); input.setAttribute('spellcheck', 'false');
    input.addEventListener('input', () => { searchIdx = 0; applySearch(true); });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); searchStep(e.shiftKey ? -1 : 1); }
      else if (e.key === 'Escape') { e.preventDefault(); toggleSearch(false); }
    });
    const count = document.createElement('span');
    count.className = 'rs-count';
    const mk = (cls, label, title, fn) => {
      const b = document.createElement('button');
      b.className = cls; b.type = 'button'; b.textContent = label; b.title = title;
      b.addEventListener('click', fn);
      return b;
    };
    panel.appendChild(input);
    panel.appendChild(count);
    panel.appendChild(mk('rs-prev', '‹', 'Previous match', () => searchStep(-1)));
    panel.appendChild(mk('rs-next', '›', 'Next match', () => searchStep(1)));
    panel.appendChild(mk('rs-earlier', 'earlier', 'Search earlier history', searchEarlier));
    panel.appendChild(mk('rs-close', '✕', 'Close search', () => toggleSearch(false)));
    bar.appendChild(toggle);
    bar.appendChild(panel);
    searchBar = bar; searchInput = input; searchCount = count;
    return bar;
  }

  async function poll(name) {
    try {
      const q = offset == null ? '' : ('?offset=' + offset);
      const r = await fetch('/api/reader/' + encodeURIComponent(name) + q);
      if (!r.ok) return;
      const data = await r.json();
      if (data.reset && feedEl && offset != null) {
        feedEl.innerHTML = '';   // transcript rotated/rewritten — restart clean
        earliest = null; atStart = false;
      }
      offset = data.offset;
      if (earliest == null && typeof data.start === 'number') {
        earliest = data.start;
        atStart = data.start === 0;
      }
      if (data.units && data.units.length) {
        emptyPolls = 0;
        append(data.units);
      } else if (++emptyPolls === 1 && feedEl && !feedEl.childNodes.length) {
        const n = document.createElement('div');
        n.className = 'r-note';
        n.textContent = data.error === 'no-transcript'
          ? 'No transcript found for this session yet.'
          : 'Waiting for activity…';
        feedEl.appendChild(n);
      }
    } catch (e) { /* offline poll — reconnect logic lives on the input WS */ }
  }

  // A6: self-rescheduling poll (replaced the fixed 3s setInterval). 3s while the
  // session is producing units; backs off to 5s after >=5 consecutive empty polls
  // (an idle/finished session), and snaps back to 3s the moment new units arrive
  // (poll() resets emptyPolls). Hidden tabs skip the fetch but keep the timer.
  function schedulePoll(name) {
    if (pollTimer) clearTimeout(pollTimer);
    const delay = emptyPolls >= 5 ? 5000 : 3000;
    pollTimer = setTimeout(async () => {
      if (!document.hidden) await poll(name);
      schedulePoll(name);
    }, delay);
  }

  // The /ws/view frames stream through live.js while reader is open (input
  // socket). Extract the pane's OWN status block — EVERYTHING Claude Code
  // renders below the input area — and pin it, ANSI colors intact. This is
  // the literal bottom of the tmux pane, not a reconstruction.
  //
  // Pane structure (verified against a live capture, 2026-07-19): the input
  // area is a bare '❯' prompt line between two full-width '─' rules — NOT a
  // ╰-cornered box (that was the old heuristic's anchor, which is why the
  // strip showed only the last hint line and dropped the host/ctx/rate-bucket
  // lines). Anchor: last '❯' line → skip the rule under it → keep the rest.
  const ANSI_RE = /\x1b\[[0-9;]*m/g;
  const plain = (l) => l.replace(ANSI_RE, '');
  window.readerFrame = function (frame) {
    if (!statusEl || !frame) return;
    const NL = String.fromCharCode(10);
    const lines = frame.split(NL);
    while (lines.length && !plain(lines[lines.length - 1]).trim()) lines.pop();
    let start = -1;
    for (let i = lines.length - 1; i >= 0; i--) {
      if (plain(lines[i]).trimStart().startsWith('❯')) { start = i + 1; break; }
    }
    if (start === -1) {
      for (let i = lines.length - 1; i >= 0; i--) {
        if (plain(lines[i]).indexOf('╰') !== -1) { start = i + 1; break; }
      }
    }
    let status;
    if (start > 0 && start < lines.length) {
      const rest = lines.slice(start);
      // drop the horizontal rule(s) directly under the prompt
      while (rest.length && /^[─\s]*$/.test(plain(rest[0]))) rest.shift();
      status = rest.filter(l => plain(l).trim()).join(NL);
    } else {
      // no anchor (dialog/TUI state): show the last 3 non-empty lines
      status = lines.filter(l => plain(l).trim()).slice(-3).join(NL);
    }
    if (status === lastStatus) return;
    lastStatus = status;
    try {
      statusEl.innerHTML = (typeof renderAnsiToLines === 'function')
        ? renderAnsiToLines(status) : esc(status);
    } catch (e) { statusEl.textContent = status; }
  };

  // Public: build the reader into #terminal-body. `connectInput` is live.js's
  // connectView (input + keepalive over /ws/view; frames feed the status line).
  window.buildReaderTerminal = function (name, connectInput) {
    ensureStyles();
    const container = document.getElementById('terminal-body');
    const wrap = document.createElement('div');
    wrap.className = 'reader-wrap';
    feedEl = document.createElement('div');
    feedEl.className = 'reader-feed';
    feedEl.id = 'reader-feed';
    statusEl = document.createElement('div');
    statusEl.className = 'reader-status';
    statusEl.id = 'reader-status';
    wrap.appendChild(buildSearchBar());   // 🔍 toggle strip pinned above the feed
    wrap.appendChild(feedEl);
    wrap.appendChild(statusEl);
    container.appendChild(wrap);
    offset = null; stickBottom = true; emptyPolls = 0; lastStatus = '';
    earliest = null; atStart = false; loadingOlder = false; readerName = name;
    searchHits = []; searchIdx = -1; searchOpen = false;
    feedEl.addEventListener('scroll', () => {
      if (feedEl && feedEl.scrollTop < 200) loadOlder();
    }, { passive: true });
    poll(name);
    schedulePoll(name);
    if (typeof connectInput === 'function') connectInput(name);
  };

  window.teardownReader = function () {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    feedEl = null; statusEl = null; offset = null; lastStatus = '';
    earliest = null; atStart = false; loadingOlder = false; readerName = null;
    searchBar = null; searchInput = null; searchCount = null;
    searchHits = []; searchIdx = -1; searchOpen = false;
  };
})();
