// reader.js — Reader mode: the mobile flagship (2026-07-19).
// Renders the session TRANSCRIPT as real HTML (markdown via marked +
// DOMPurify, both vendored) instead of re-rendering terminal frames — a
// 107-col TUI table cannot reflow on a 45-col phone, but its markdown source
// can. Self-contained: injects its own styles; integration points in live.js
// are openTerminal's mode branch and sendText/wsKey treating 'reader' like
// 'view' (input still flows over the /ws/view socket + compose bar).
//
// Data: GET /api/reader/<session>?offset=N -> {units, offset, reset} —
// incremental byte-offset polling (3s), server bounds the tail window.

(function () {
  'use strict';

  const CSS = `
  .reader-feed { height: 100%; overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 10px 12px 18px; font-size: 15px; line-height: 1.45; }
  .reader-feed .r-user { background: var(--bg-surface); border-left: 3px solid #a33a0a;
    padding: 7px 10px; margin: 12px 0 8px; border-radius: 4px; white-space: pre-wrap;
    word-break: break-word; color: var(--text-primary); font-weight: 600; }
  .reader-feed .r-asst { color: var(--text-primary); word-break: break-word; }
  .reader-feed .r-asst p { margin: 7px 0; }
  .reader-feed .r-asst pre { background: var(--bg-deep); border: 1px solid var(--border);
    padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 12.5px; }
  .reader-feed .r-asst code { background: var(--bg-deep); padding: 1px 4px;
    border-radius: 3px; font-size: 0.9em; }
  .reader-feed .r-asst pre code { background: none; padding: 0; }
  .reader-feed .r-asst table { border-collapse: collapse; display: block;
    overflow-x: auto; max-width: 100%; margin: 8px 0; font-size: 13px; }
  .reader-feed .r-asst th, .reader-feed .r-asst td { border: 1px solid var(--border);
    padding: 4px 8px; white-space: nowrap; }
  .reader-feed .r-asst h1, .reader-feed .r-asst h2, .reader-feed .r-asst h3 {
    font-size: 1.05em; margin: 12px 0 4px; color: var(--accent-cream); }
  .reader-feed .r-asst ul, .reader-feed .r-asst ol { padding-left: 22px; margin: 6px 0; }
  .reader-feed .r-asst a { color: #d08770; }
  .reader-feed .r-tools { display: flex; flex-wrap: wrap; gap: 5px; margin: 6px 0; }
  .reader-feed .r-tool { background: var(--bg-deep); border: 1px solid var(--border);
    border-radius: 10px; padding: 2px 9px; font-size: 11.5px;
    color: var(--text-secondary); max-width: 100%; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .reader-feed .r-err { color: #d08770; font-size: 12px; margin: 4px 0; }
  .reader-feed .r-note { color: var(--text-secondary); font-size: 12px;
    text-align: center; padding: 14px 0; }
  `;

  let feedEl = null, pollTimer = null, offset = null, stickBottom = true;
  let emptyPolls = 0;

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
      wrap.textContent = '⚠ ' + (u.errors || 1) + ' tool error(s) — self-corrected or pending';
    } else {
      wrap.className = 'r-asst';
      if (u.text) wrap.innerHTML = renderMd(u.text);
      if (u.tools && u.tools.length) {
        const row = document.createElement('div');
        row.className = 'r-tools';
        u.tools.forEach(t => {
          const chip = document.createElement('span');
          chip.className = 'r-tool';
          chip.textContent = '⏺ ' + t.name + (t.detail ? ' — ' + t.detail : '');
          chip.title = t.detail || t.name;
          row.appendChild(chip);
        });
        wrap.appendChild(row);
      }
    }
    return wrap;
  }

  function append(units) {
    if (!feedEl || !units.length) return;
    const nearBottom = feedEl.scrollHeight - feedEl.scrollTop - feedEl.clientHeight < 40;
    units.forEach(u => feedEl.appendChild(unitNode(u)));
    // bound the DOM: keep the last ~200 nodes
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

  // Public: build the reader into #terminal-body. `connectInput` is live.js's
  // connectView (input + keepalive over /ws/view; reader ignores its frames).
  window.buildReaderTerminal = function (name, connectInput) {
    ensureStyles();
    const container = document.getElementById('terminal-body');
    feedEl = document.createElement('div');
    feedEl.className = 'reader-feed';
    feedEl.id = 'reader-feed';
    container.appendChild(feedEl);
    offset = null; stickBottom = true; emptyPolls = 0;
    poll(name);
    pollTimer = setInterval(() => { if (!document.hidden) poll(name); }, 3000);
    if (typeof connectInput === 'function') connectInput(name);
  };

  window.teardownReader = function () {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    feedEl = null; offset = null;
  };
})();
