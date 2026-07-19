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
    padding: 8px 10px 12px; font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 12.5px; line-height: 1.5; color: #FAFAF9; }
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
    padding: 3px 7px; white-space: nowrap; }
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
    background: #0C0A09; padding: 3px 8px 4px;
    font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 11.5px;
    line-height: 1.45; color: #A8A29E; white-space: pre; overflow-x: auto;
    -webkit-overflow-scrolling: touch; }
  .reader-status:empty { display: none; }
  `;

  let feedEl = null, statusEl = null, pollTimer = null, offset = null;
  let stickBottom = true, emptyPolls = 0, lastStatus = '';

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
      wrap.className = 'r-err';
      wrap.textContent = (u.errors || 1) + ' tool error(s) — self-corrected or pending';
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
    // bound the DOM: keep the last ~220 nodes
    while (feedEl.childNodes.length > 220) feedEl.removeChild(feedEl.firstChild);
    if (nearBottom || stickBottom) feedEl.scrollTop = feedEl.scrollHeight;
    stickBottom = false;
  }

  async function poll(name) {
    try {
      const q = offset == null ? '' : ('?offset=' + offset);
      const r = await fetch('/api/reader/' + encodeURIComponent(name) + q);
      if (!r.ok) return;
      const data = await r.json();
      if (data.reset && feedEl && offset != null) {
        feedEl.innerHTML = '';   // transcript rotated/rewritten — restart clean
      }
      offset = data.offset;
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

  // The /ws/view frames stream through live.js while reader is open (input
  // socket). Extract the pane's OWN status line — everything Claude Code
  // renders below the input box — and pin it, ANSI colors intact. This is the
  // literal status line from tmux, not a reconstruction.
  window.readerFrame = function (frame) {
    if (!statusEl || !frame) return;
    const NL = String.fromCharCode(10);
    const lines = frame.split(NL);
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    let start = -1;
    for (let i = lines.length - 1; i >= 0; i--) {
      if (lines[i].indexOf('╰') !== -1) { start = i + 1; break; }
    }
    let status;
    if (start > 0 && start < lines.length) {
      status = lines.slice(start).filter(l => l.trim()).join(NL);
    } else {
      status = lines.length ? lines[lines.length - 1] : '';
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
    wrap.appendChild(feedEl);
    wrap.appendChild(statusEl);
    container.appendChild(wrap);
    offset = null; stickBottom = true; emptyPolls = 0; lastStatus = '';
    poll(name);
    pollTimer = setInterval(() => { if (!document.hidden) poll(name); }, 3000);
    if (typeof connectInput === 'function') connectInput(name);
  };

  window.teardownReader = function () {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    feedEl = null; statusEl = null; offset = null; lastStatus = '';
  };
})();
