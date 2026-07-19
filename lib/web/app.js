// ─── State ───
let activeProject = null;
let dateRange = 30;

// ─── Helpers ───
function getTimeline() {
  let tl = activeProject && D.per_project_timeline[activeProject]
    ? D.per_project_timeline[activeProject]
    : D.sessions_timeline;
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - dateRange);
  const cutStr = cutoff.toISOString().slice(0, 10);
  return tl.filter(d => d.date >= cutStr);
}

function getTools() {
  if (activeProject && D.per_project_tools[activeProject]) {
    return D.per_project_tools[activeProject];
  }
  return D.tool_breakdown;
}

function getTemporal() {
  if (activeProject && D.per_project_temporal[activeProject]) {
    return D.per_project_temporal[activeProject];
  }
  return D.temporal.by_hour;
}

function getProjectData() {
  if (activeProject) {
    const p = D.projects.find(p => p.name === activeProject);
    if (p) return { sessions: p.sessions, hours: p.hours, errors: p.errors, tools: p.tools, cost: p.cost, tokens_m: 0, projects: 1 };
  }
  return D.summary;
}

// ─── Render: Meta ───
function renderMeta() {
  document.getElementById('meta').innerHTML = `
    Generated ${D.generated_at.slice(0,16).replace('T',' ')} UTC<br>
    ${D.summary.total_sessions} sessions / ${D.summary.projects} projects
  `;
}

// ─── Render: Filter Bar ───
function renderFilterBar() {
  document.querySelectorAll('.filter-btn').forEach(btn => {
    const days = parseInt(btn.textContent);
    btn.classList.toggle('active', days === dateRange);
  });
  const tag = document.getElementById('project-filter');
  if (activeProject) {
    tag.classList.add('visible');
    document.getElementById('filter-name').textContent = activeProject;
  } else {
    tag.classList.remove('visible');
  }
}

// ─── Render: Stats ───
function formatDelta(d) {
  if (d === null || d === undefined) return '';
  const sign = d > 0 ? '+' : '';
  const cls = d > 0 ? 'delta-up' : d < 0 ? 'delta-down' : 'delta-flat';
  return `<span class="delta ${cls}">${sign}${d}%</span>`;
}

function fmtNum(n) { return (+n || 0).toLocaleString(undefined, {maximumFractionDigits: 0}); }
function fmtMoney(n) {
  n = +n || 0;
  if (n >= 1e6) return '$' + (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return '$' + (n/1e3).toFixed(1) + 'k';
  return '$' + n.toFixed(0);
}
function fmtTokM(m) {  // value is already in millions
  m = +m || 0;
  if (m >= 1e3) return (m/1e3).toFixed(1) + 'B';
  if (m >= 1) return Math.round(m) + 'M';
  return Math.round(m*1e3) + 'k';
}
function renderStats() {
  const s = getProjectData();
  const isFiltered = !!activeProject;
  const d = (!isFiltered && s.deltas) ? s.deltas : {};
  document.getElementById('stats').innerHTML = [
    ['SESSIONS', fmtNum(isFiltered ? s.sessions : s.total_sessions), '', d.sessions],
    ['HOURS', fmtNum(isFiltered ? s.hours : s.total_hours), '', d.hours],
    ['ERRORS', fmtNum(isFiltered ? s.errors : s.total_errors), (isFiltered ? s.errors : s.total_errors) > 100 ? 'alert' : '', d.errors],
    ['EST. COST', fmtMoney(isFiltered ? s.cost : s.total_cost || 0), (isFiltered ? s.cost : s.total_cost || 0) > 500 ? 'warn' : '', d.cost],
    ['TOKENS', fmtTokM(isFiltered ? s.tokens_m : (s.total_tokens_m || 0)), ''],
    ['PROJECTS', isFiltered ? 1 : s.projects, isFiltered ? '' : 'good'],
  ].map(([l,v,c,delta]) => `
    <div class="stat-cell">
      <div class="label">${l}</div>
      <div class="value ${c}">${v}</div>
      ${delta !== undefined ? formatDelta(delta) : ''}
    </div>
  `).join('');
}

// ─── Render: Projects Table (sortable) ───
let projectSort = { col: 'errors', dir: -1 };

function sortProjects(col) {
  if (projectSort.col === col) projectSort.dir *= -1;
  else { projectSort.col = col; projectSort.dir = -1; }
  renderProjects();
}

function renderProjects() {
  const cols = { name: 'name', archetype: 'archetype', sessions: 'sessions', error_rate: 'error_rate', hours: 'hours', cost: 'cost' };
  const sorted = [...D.projects].sort((a, b) => {
    const av = a[projectSort.col], bv = b[projectSort.col];
    if (typeof av === 'string') return projectSort.dir * av.localeCompare(bv);
    return projectSort.dir * (av - bv);
  });

  // Update header sort indicators
  document.querySelectorAll('#project-header th').forEach(th => {
    const col = th.dataset.col;
    if (!col) return;
    const arrow = col === projectSort.col ? (projectSort.dir > 0 ? ' \u25B4' : ' \u25BE') : '';
    th.textContent = th.dataset.label + arrow;
  });

  document.getElementById('projects').innerHTML = sorted.map(p => `
    <tr class="clickable ${activeProject === p.name ? 'active-project' : ''}"
        onclick="setProject('${p.name}')">
      <td style="color:var(--accent-cream)">${p.name}</td>
      <td style="color:var(--text-muted);font-size:10px">${p.archetype}</td>
      <td class="num">${p.sessions}</td>
      <td class="num ${p.error_rate > 20 ? 'err' : p.error_rate > 10 ? 'wrn' : ''}">${p.error_rate}</td>
      <td class="num">${p.hours}</td>
      <td class="num ${p.cost > 500 ? 'err' : p.cost > 100 ? 'wrn' : ''}">${p.cost.toFixed(0)}</td>
    </tr>
  `).join('');
}

// ─── Render: Tool Usage ───
function renderTools() {
  const tools = getTools();
  const entries = Object.entries(tools).sort((a, b) => b[1] - a[1]).slice(0, 10);
  const maxTool = entries.length ? entries[0][1] : 1;
  document.getElementById('tools').innerHTML = entries.map(([t, c]) => `
    <div class="bar-row">
      <div class="bar-label">${t}</div>
      <div class="bar-track"><div class="bar-fill tool" style="width:${c/maxTool*100}%"></div></div>
      <div class="bar-value">${c.toLocaleString()}</div>
    </div>
  `).join('') || '<div class="no-data">No tool data</div>';
}

// ─── Render: Timeline (sqrt scaling) ───
function renderTimeline() {
  const tl = getTimeline();
  document.getElementById('timeline-range').textContent = '(' + dateRange + 'd)';
  if (!tl.length) {
    document.getElementById('timeline').innerHTML = '<div class="no-data">No sessions in range</div>';
    document.getElementById('timeline-labels').innerHTML = '';
    return;
  }
  const maxSess = Math.max(...tl.map(d => d.sessions), 1);
  const sqrtMax = Math.sqrt(maxSess);
  document.getElementById('timeline').innerHTML = tl.map(d => {
    const pct = (Math.sqrt(d.sessions) / sqrtMax) * 100;
    return `<div class="timeline-bar" style="height:${Math.max(pct, 3)}%"
         data-tip="${d.date}: ${d.sessions} sess, ${d.errors} err, ${d.hours}h"></div>`;
  }).join('');
  const step = Math.max(1, Math.ceil(tl.length / 7));
  document.getElementById('timeline-labels').innerHTML = tl
    .filter((_, i) => i % step === 0 || i === tl.length - 1)
    .map(d => `<span>${d.date.slice(5)}</span>`).join('');
}

// ─── Render: Error Trend (SVG) ───
function renderErrorTrend() {
  const tl = getTimeline();
  document.getElementById('error-range').textContent = '(' + dateRange + 'd)';
  if (tl.length < 2) {
    document.getElementById('error-trend').innerHTML = '<div class="no-data">Need 2+ days of data</div>';
    return;
  }

  const w = 600, h = 180;
  const pad = { t: 15, r: 10, b: 28, l: 42 };
  const pw = w - pad.l - pad.r;
  const ph = h - pad.t - pad.b;

  const errors = tl.map(d => d.errors);
  const maxErr = Math.max(...errors, 1);

  const points = tl.map((d, i) => ({
    x: pad.l + (i / (tl.length - 1)) * pw,
    y: pad.t + ph - (d.errors / maxErr) * ph
  }));

  const linePath = points.map((p, i) => (i === 0 ? 'M' : 'L') + p.x.toFixed(1) + ',' + p.y.toFixed(1)).join(' ');
  const areaPath = linePath + ' L' + points[points.length-1].x.toFixed(1) + ',' + (pad.t + ph) + ' L' + points[0].x.toFixed(1) + ',' + (pad.t + ph) + ' Z';

  const gridLines = [0, 0.25, 0.5, 0.75, 1].map(frac => {
    const y = pad.t + ph * (1 - frac);
    const val = Math.round(maxErr * frac);
    return `<line x1="${pad.l}" y1="${y}" x2="${w - pad.r}" y2="${y}" stroke="var(--border)" stroke-opacity="0.3" stroke-dasharray="${frac === 0 ? 'none' : '2,3'}"/>
      <text x="${pad.l - 5}" y="${y + 3}" text-anchor="end" fill="var(--text-muted)" font-size="9">${val}</text>`;
  }).join('');

  const step = Math.max(1, Math.ceil(tl.length / 6));
  const xLabels = tl.map((d, i) => {
    if (i % step !== 0 && i !== tl.length - 1) return '';
    const x = pad.l + (i / (tl.length - 1)) * pw;
    return `<text x="${x}" y="${h - 4}" text-anchor="middle" fill="var(--text-muted)" font-size="8">${d.date.slice(5)}</text>`;
  }).join('');

  const dots = points.map((p, i) =>
    `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.5" fill="var(--accent-red)" opacity="0.6">
      <title>${tl[i].date}: ${tl[i].errors} errors</title>
    </circle>`
  ).join('');

  document.getElementById('error-trend').innerHTML = `
    <svg class="svg-chart" viewBox="0 0 ${w} ${h}">
      <defs>
        <linearGradient id="err-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--accent-red)" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="var(--accent-red)" stop-opacity="0.03"/>
        </linearGradient>
      </defs>
      ${gridLines}
      <path d="${areaPath}" fill="url(#err-grad)"/>
      <path d="${linePath}" fill="none" stroke="var(--accent-red)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      ${dots}
      ${xLabels}
    </svg>
  `;
}

// ─── Render: Cost Chart ───
function renderCostChart() {
  const projects = activeProject
    ? D.projects.filter(p => p.name === activeProject)
    : D.projects.filter(p => p.cost > 0).sort((a, b) => b.cost - a.cost).slice(0, 10);
  const maxCost = projects.length ? Math.max(...projects.map(p => p.cost)) : 1;
  document.getElementById('cost-chart').innerHTML = projects.map(p => `
    <div class="bar-row">
      <div class="bar-label" style="width:100px">${p.name}</div>
      <div class="bar-track"><div class="bar-fill cost" style="width:${p.cost / maxCost * 100}%"></div></div>
      <div class="bar-value">${p.cost.toFixed(0)}</div>
    </div>
  `).join('') || '<div class="no-data">No cost data</div>';
}

// ─── Render: Proposals ───
function renderProposals() {
  const filtered = activeProject
    ? D.proposals.filter(p => p.project === activeProject || p.project?.toLowerCase() === activeProject?.toLowerCase())
    : D.proposals;
  document.getElementById('proposals').innerHTML = filtered.slice(0, 8).map(p => `
    <div class="proposal">
      <span class="proposal-status ${p.status}">${p.status}</span>
      <div>
        <div class="proposal-text">${p.description}</div>
        <div class="proposal-meta">${p.project} · ${p.type} · ${p.timestamp}</div>
      </div>
    </div>
  `).join('') || '<div class="no-data">No proposals</div>';
}

// ─── Render: Recent Sessions ───
function renderRecent() {
  const filtered = activeProject
    ? D.recent_sessions.filter(s => s.project === activeProject)
    : D.recent_sessions;
  document.getElementById('recent').innerHTML = filtered.map(s => `
    <tr>
      <td style="font-size:10px">${s.date.slice(5,16)}</td>
      <td style="color:var(--accent-cream)">${s.project}</td>
      <td class="num">${s.duration_m}m</td>
      <td class="num ${s.errors > 5 ? 'err' : ''}">${s.errors}</td>
      <td style="font-size:10px;color:var(--text-muted)">${s.outcome}</td>
    </tr>
  `).join('') || '<tr><td colspan="5" class="no-data">No sessions</td></tr>';
}

// ─── Render: Temporal ───
function renderTemporal() {
  const hours = getTemporal();
  const maxH = Math.max(...Object.values(hours), 1);
  document.getElementById('temporal').innerHTML = Array.from({length: 24}, (_, i) => {
    const c = hours[i] || hours[String(i)] || 0;
    return `<div class="bar-row">
      <div class="bar-label" style="width:24px">${String(i).padStart(2, '0')}</div>
      <div class="bar-track"><div class="bar-fill hour" style="width:${c / maxH * 100}%"></div></div>
      <div class="bar-value">${c}</div>
    </div>`;
  }).join('');
}

// ─── Render: Token Breakdown ───
function renderTokens() {
  const tt = D.token_types;
  const entries = Object.entries(tt);
  if (!entries.length) {
    document.getElementById('tokens').innerHTML = '<div class="no-data">No token data yet</div>';
    return;
  }
  const labels = { input: 'Input', output: 'Output', cache_read: 'Cache Read', cache_create: 'Cache Create' };
  const sorted = entries.sort((a, b) => b[1] - a[1]);
  const maxVal = sorted[0][1];
  const total = sorted.reduce((s, [, v]) => s + v, 0);
  document.getElementById('tokens').innerHTML = sorted.map(([k, v]) => {
    const pct = total > 0 ? (v / total * 100).toFixed(1) : 0;
    const label = labels[k] || k;
    const display = v > 1e9 ? (v / 1e9).toFixed(1) + 'B' : v > 1e6 ? (v / 1e6).toFixed(1) + 'M' : v > 1e3 ? (v / 1e3).toFixed(1) + 'K' : v;
    return `<div class="bar-row">
      <div class="bar-label" style="width:90px">${label}</div>
      <div class="bar-track"><div class="bar-fill token" style="width:${v / maxVal * 100}%"></div></div>
      <div class="bar-value">${display} (${pct}%)</div>
    </div>`;
  }).join('');
}

// ─── Render: Versions ───
function renderVersions() {
  const entries = Object.entries(D.model_versions);
  const maxV = entries.length ? Math.max(...entries.map(e => e[1])) : 1;
  document.getElementById('versions').innerHTML = entries.map(([v, c]) => `
    <div class="bar-row">
      <div class="bar-label">${v}</div>
      <div class="bar-track"><div class="bar-fill highlight" style="width:${c / maxV * 100}%"></div></div>
      <div class="bar-value">${c}</div>
    </div>
  `).join('') || '<div class="no-data">No version data</div>';
}

// ─── Render: Wiki ───
function renderWiki() {
  const w = D.wiki;
  document.getElementById('wiki').innerHTML = `
    <div style="font-family:var(--font-mono);font-size:13px;color:var(--text-secondary);line-height:2">
      Articles: <span style="color:var(--accent-cream)">${w.total_articles}</span><br>
      Words: <span style="color:var(--accent-cream)">${w.total_words.toLocaleString()}</span><br>
      ${Object.entries(w.sections || {}).map(([s, d]) =>
        `${s}: ${d.articles} articles`
      ).join('<br>')}
    </div>
  `;
}

// ─── Animate bars ───
function animateBars() {
  setTimeout(() => {
    document.querySelectorAll('.bar-fill').forEach(el => {
      const w = el.style.width;
      el.style.width = '0%';
      requestAnimationFrame(() => el.style.width = w);
    });
  }, 50);
}

// ─── Event Handlers ───
function setProject(name) {
  activeProject = activeProject === name ? null : name;
  renderAll();
}

function setDateRange(days) {
  dateRange = days;
  renderAll();
}

// ─── Render All ───
function renderAll() {
  renderMeta();
  renderFilterBar();
  renderStats();
  renderProjects();
  renderTools();
  renderErrorTrend();
  renderCostChart();
  renderTimeline();
  renderProposals();
  renderRecent();
  renderTemporal();
  renderTokens();
  renderVersions();
  renderWiki();
  animateBars();
}

renderAll();
