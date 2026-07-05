"""Dashboard generator — creates a self-contained HTML dashboard from clanker data."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from dashboard_data import generate_dashboard_data


def generate_dashboard(output_path=None, watch=False):
    """Generate a self-contained HTML dashboard."""
    if watch:
        _watch_and_regenerate(output_path)
        return

    data = generate_dashboard_data()
    data_json = json.dumps(data, indent=None)

    if not output_path:
        output_path = os.path.join(
            os.environ.get("CLANKER_DATA", "/data/clanker"),
            "dashboard.html"
        )

    html = _build_html(data_json)

    with open(output_path, "w") as f:
        f.write(html)

    print(f"Dashboard written to: {output_path}")
    print(f"Open in browser: file://{os.path.abspath(output_path)}")
    return output_path


def _watch_and_regenerate(output_path):
    """Watch data directory for changes and regenerate dashboard."""
    import hashlib

    data_dir = os.path.join(
        os.environ.get("CLANKER_DATA", "/data/clanker"),
        "raw"
    )
    if not output_path:
        output_path = os.path.join(
            os.environ.get("CLANKER_DATA", "/data/clanker"),
            "dashboard.html"
        )

    print(f"Watching {data_dir} for changes...")
    print(f"Output: {output_path}")
    print("Press Ctrl+C to stop.")

    last_hash = None
    while True:
        current_hash = _dir_hash(data_dir)
        if current_hash != last_hash:
            if last_hash is not None:
                print(f"\n[{time.strftime('%H:%M:%S')}] Changes detected, regenerating...")
            generate_dashboard(output_path=output_path, watch=False)
            last_hash = current_hash
        time.sleep(2)


def _dir_hash(path):
    """Compute a hash of all file mtimes in a directory tree."""
    import hashlib
    h = hashlib.md5()
    for root, dirs, files in os.walk(path):
        for f in sorted(files):
            fp = os.path.join(root, f)
            try:
                h.update(f"{fp}:{os.path.getmtime(fp)}".encode())
            except OSError:
                pass
    return h.hexdigest()


def _build_html(data_json):
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<!-- interactive-widget=resizes-content: the soft keyboard shrinks the LAYOUT
     viewport (Chrome 108+), so the fixed terminal overlay ends exactly at the
     keyboard's top edge — no gap, no JS pinning races. iOS ignores it (the
     visualViewport handler in serve.py covers it there). -->
<meta name="viewport" content="width=device-width, initial-scale=1.0, interactive-widget=resizes-content">
<title>CLANKER — Harness Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@300;400;500;700&family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg-void: #0C0A09;
  --bg-deep: #1C1917;
  --bg-panel: #292524;
  --bg-surface: #44403C;
  --border: #57534E;
  --text-primary: #FAFAF9;
  --text-secondary: #A8A29E;
  --text-muted: #78716C;
  --accent-red: #DC2626;
  --accent-terracotta: #C2410C;
  --accent-amber: #D97706;
  --accent-olive: #65A30D;
  --accent-cream: #FEF3C7;
  --accent-blue: #2563EB;
  --font-display: 'Instrument Serif', Georgia, serif;
  --font-mono: 'JetBrains Mono', 'Courier New', monospace;
  --font-body: 'Outfit', system-ui, sans-serif;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  background: var(--bg-void);
  color: var(--text-primary);
  font-family: var(--font-body);
  font-size: 16px;
  line-height: 1.5;
  min-height: 100vh;
  position: relative;
}}

body::before {{
  content: '';
  position: fixed;
  inset: 0;
  background: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='256' height='256' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");
  pointer-events: none;
  z-index: 9999;
}}

.dashboard {{
  max-width: 1440px;
  margin: 0 auto;
  padding: 32px 24px;
}}

/* ─── Header ─── */
.header {{
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  padding-bottom: 24px;
  border-bottom: 2px solid var(--accent-terracotta);
  margin-bottom: 16px;
}}

.header h1 {{
  font-family: var(--font-display);
  font-size: 3.2rem;
  font-weight: 400;
  letter-spacing: -0.03em;
  color: var(--accent-cream);
  line-height: 1;
}}

.header h1 span {{
  color: var(--accent-terracotta);
}}

.header-meta {{
  text-align: right;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.8;
}}

/* ─── Filter Bar ─── */
.filter-bar {{
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 0;
  margin-bottom: 16px;
  border-bottom: 1px solid rgba(87, 83, 78, 0.3);
}}

.filter-group {{
  display: flex;
  align-items: center;
  gap: 4px;
}}

.filter-label {{
  font-family: var(--font-mono);
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--text-muted);
  margin-right: 8px;
}}

.filter-btn {{
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 4px 12px;
  background: var(--bg-panel);
  color: var(--text-muted);
  border: 1px solid transparent;
  cursor: pointer;
  transition: all 0.2s;
}}

.filter-btn:hover {{
  color: var(--text-secondary);
  border-color: var(--border);
}}

.filter-btn.active {{
  background: var(--accent-terracotta);
  color: var(--bg-void);
  border-color: var(--accent-terracotta);
}}

.filter-tag {{
  display: none;
  align-items: center;
  gap: 8px;
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 4px 12px;
  background: rgba(194, 65, 12, 0.15);
  border: 1px solid var(--accent-terracotta);
  color: var(--accent-cream);
}}

.filter-tag.visible {{
  display: flex;
}}

.filter-tag .close {{
  cursor: pointer;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1;
}}

.filter-tag .close:hover {{
  color: var(--accent-red);
}}

/* ─── Stat Row ─── */
.stat-row {{
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 2px;
  margin-bottom: 32px;
}}

.stat-cell {{
  background: var(--bg-deep);
  padding: 20px 16px;
  border-left: 3px solid var(--border);
  transition: border-color 0.2s;
}}

.stat-cell:hover {{
  border-left-color: var(--accent-terracotta);
}}

.stat-cell .label {{
  font-family: var(--font-mono);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--text-muted);
  margin-bottom: 8px;
}}

.stat-cell .value {{
  font-family: var(--font-mono);
  font-size: 2.4rem;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.02em;
}}

.stat-cell .value.alert {{ color: var(--accent-red); }}
.stat-cell .value.warn {{ color: var(--accent-amber); }}
.stat-cell .value.good {{ color: var(--accent-olive); }}

.delta {{
  font-family: var(--font-mono);
  font-size: 10px;
  margin-top: 4px;
  display: block;
}}
.delta-up {{ color: var(--accent-red); }}
.delta-down {{ color: var(--accent-olive); }}
.delta-flat {{ color: var(--text-muted); }}

/* ─── Grid ─── */
.grid {{
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 2px;
  margin-bottom: 2px;
}}

.grid-3 {{
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 2px;
  margin-bottom: 2px;
}}

.grid-2 {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px;
  margin-bottom: 2px;
}}

/* ─── Panels ─── */
.panel {{
  background: var(--bg-deep);
  padding: 24px;
  position: relative;
}}

.panel::before {{
  content: attr(data-label);
  position: absolute;
  top: 0;
  right: 0;
  font-family: var(--font-mono);
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  color: var(--text-muted);
  background: var(--bg-panel);
  padding: 4px 10px;
}}

.panel h2 {{
  font-family: var(--font-display);
  font-size: 1.6rem;
  font-weight: 400;
  color: var(--accent-cream);
  margin-bottom: 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}}

.panel h2 .sub {{
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
}}

/* ─── Tables ─── */
table {{
  width: 100%;
  border-collapse: collapse;
  font-family: var(--font-mono);
  font-size: 13px;
}}

th {{
  text-align: left;
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--text-muted);
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  font-weight: 500;
}}

td {{
  padding: 8px;
  border-bottom: 1px solid rgba(87, 83, 78, 0.3);
  color: var(--text-secondary);
}}

tr:hover td {{
  color: var(--text-primary);
  background: rgba(255,255,255,0.02);
}}

tr.clickable {{
  cursor: pointer;
}}

tr.active-project td {{
  background: rgba(194, 65, 12, 0.1);
  color: var(--accent-cream);
}}

tr.active-project td:first-child {{
  border-left: 2px solid var(--accent-terracotta);
}}

.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
th[data-col]:hover {{ color: var(--accent-cream); }}
.err {{ color: var(--accent-red); }}
.ok {{ color: var(--accent-olive); }}
.wrn {{ color: var(--accent-amber); }}

/* ─── Bar Charts ─── */
.bar-row {{
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}}

.bar-label {{
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-secondary);
  width: 80px;
  text-align: right;
  flex-shrink: 0;
}}

.bar-track {{
  flex: 1;
  height: 18px;
  background: var(--bg-panel);
  position: relative;
  overflow: hidden;
}}

.bar-fill {{
  height: 100%;
  background: var(--accent-terracotta);
  transition: width 0.8s cubic-bezier(0.16, 1, 0.3, 1);
}}

.bar-fill.tool {{ background: var(--accent-amber); }}
.bar-fill.hour {{ background: var(--accent-blue); }}
.bar-fill.cost {{ background: var(--accent-terracotta); }}
.bar-fill.token {{ background: var(--accent-olive); }}
.bar-fill.highlight {{ background: var(--accent-cream); }}

.bar-value {{
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  width: 50px;
  flex-shrink: 0;
}}

/* ─── Timeline ─── */
.timeline {{
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 120px;
  padding-top: 20px;
}}

.timeline-bar {{
  flex: 1;
  background: var(--accent-terracotta);
  min-height: 2px;
  transition: height 0.6s cubic-bezier(0.16, 1, 0.3, 1);
  position: relative;
  opacity: 0.7;
}}

.timeline-bar:hover {{
  opacity: 1;
}}

.timeline-bar:hover::after {{
  content: attr(data-tip);
  position: absolute;
  bottom: calc(100% + 4px);
  left: 50%;
  transform: translateX(-50%);
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-primary);
  background: var(--bg-void);
  padding: 4px 8px;
  white-space: nowrap;
  border: 1px solid var(--border);
  z-index: 10;
}}

.timeline-labels {{
  display: flex;
  gap: 2px;
  margin-top: 4px;
}}

.timeline-labels span {{
  flex: 1;
  font-family: var(--font-mono);
  font-size: 8px;
  color: var(--text-muted);
  text-align: center;
}}

/* ─── SVG Charts ─── */
.svg-chart {{
  width: 100%;
  height: auto;
}}

.svg-chart text {{
  font-family: var(--font-mono);
}}

/* ─── Proposals ─── */
.proposal {{
  padding: 10px 0;
  border-bottom: 1px solid rgba(87, 83, 78, 0.3);
  display: flex;
  gap: 12px;
  align-items: flex-start;
}}

.proposal-status {{
  font-family: var(--font-mono);
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 2px 6px;
  flex-shrink: 0;
  margin-top: 2px;
}}

.proposal-status.pending {{ background: var(--accent-amber); color: var(--bg-void); }}
.proposal-status.accepted {{ background: var(--accent-olive); color: var(--bg-void); }}
.proposal-status.rejected {{ background: var(--bg-surface); color: var(--text-muted); }}

.proposal-text {{
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.4;
}}

.proposal-meta {{
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 2px;
}}

/* ─── No Data ─── */
.no-data {{
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 20px 0;
  text-align: center;
}}

/* ─── Animations ─── */
@keyframes fadeUp {{
  from {{ opacity: 0; transform: translateY(12px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}

.panel {{ animation: fadeUp 0.4s ease-out both; }}
.stat-cell {{ animation: fadeUp 0.3s ease-out both; }}
.stat-cell:nth-child(1) {{ animation-delay: 0.05s; }}
.stat-cell:nth-child(2) {{ animation-delay: 0.1s; }}
.stat-cell:nth-child(3) {{ animation-delay: 0.15s; }}
.stat-cell:nth-child(4) {{ animation-delay: 0.2s; }}
.stat-cell:nth-child(5) {{ animation-delay: 0.25s; }}
.stat-cell:nth-child(6) {{ animation-delay: 0.3s; }}

.grid .panel:nth-child(1) {{ animation-delay: 0.2s; }}
.grid .panel:nth-child(2) {{ animation-delay: 0.3s; }}
.grid-3 .panel:nth-child(1) {{ animation-delay: 0.35s; }}
.grid-3 .panel:nth-child(2) {{ animation-delay: 0.4s; }}
.grid-3 .panel:nth-child(3) {{ animation-delay: 0.45s; }}

/* ─── Print ─── */
@media print {{
  body::before {{ display: none; }}
  .filter-bar {{ display: none; }}
  .dashboard {{ padding: 16px; }}
  .panel {{ break-inside: avoid; page-break-inside: avoid; }}
  .stat-cell {{ border-left-color: #ccc; }}
  .stat-cell .value {{ color: #111; }}
  .header {{ border-bottom-color: #999; }}
  .header h1 {{ color: #111; }}
  .header h1 span {{ color: #666; }}
  .panel h2 {{ color: #111; }}
  table, th, td {{ color: #333; }}
  .bar-fill {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
}}

/* ─── Responsive ─── */
@media (max-width: 1024px) {{
  .grid {{ grid-template-columns: 1fr; }}
  .grid-3 {{ grid-template-columns: 1fr 1fr; }}
  .stat-row {{ grid-template-columns: repeat(3, 1fr); }}
}}

@media (max-width: 640px) {{
  .stat-row {{ grid-template-columns: repeat(2, 1fr); }}
  .grid-3 {{ grid-template-columns: 1fr; }}
  .grid-2 {{ grid-template-columns: 1fr; }}
  .header h1 {{ font-size: 2rem; }}
  .header {{ flex-direction: column; align-items: flex-start; gap: 8px; }}
  .header-meta {{ text-align: left; }}
  .stat-cell .value {{ font-size: 1.8rem; }}
  .panel h2 {{ font-size: 1.3rem; }}
  .bar-label {{ width: 60px; font-size: 10px; }}
  table {{ font-size: 12px; min-width: 460px; }}
  td, th {{ padding: 7px 6px; white-space: nowrap; }}
  /* Tables scroll horizontally instead of cramming every column into the screen */
  .panel {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  .proposal-text {{ font-size: 13px; white-space: normal; }}
  .filter-bar {{ flex-wrap: wrap; }}
  .filter-btn {{ padding: 8px 13px; }}
}}
</style>
</head>
<body>
<div class="dashboard">

<header class="header">
  <h1>CL<span>A</span>NKER</h1>
  <a href="/wyc/" title="Spectate — watch Claude Code work live across every session (watchyourclankers)"
     style="align-self:center;margin-left:18px;color:#C2410C;text-decoration:none;font-size:11px;text-transform:uppercase;letter-spacing:0.12em;border:1px solid #57534E;padding:6px 12px">&#128065; Spectate</a>
  <div class="header-meta" id="meta" style="margin-left:auto"></div>
</header>

<div class="filter-bar">
  <div class="filter-group">
    <span class="filter-label">Range</span>
    <button class="filter-btn" onclick="setDateRange(7)" aria-label="Show last 7 days">7d</button>
    <button class="filter-btn" onclick="setDateRange(14)" aria-label="Show last 14 days">14d</button>
    <button class="filter-btn active" onclick="setDateRange(30)" aria-label="Show last 30 days">30d</button>
    <button class="filter-btn" onclick="setDateRange(90)" aria-label="Show last 90 days">90d</button>
  </div>
  <div class="filter-tag" id="project-filter">
    <span id="filter-name"></span>
    <span class="close" onclick="setProject(null)">&times;</span>
  </div>
</div>

<div class="stat-row" id="stats" role="region" aria-label="Key metrics"></div>

<div class="grid">
  <div class="panel" data-label="PROJECTS">
    <h2>Projects</h2>
    <table><thead><tr id="project-header">
      <th data-col="name" data-label="Name" onclick="sortProjects('name')" style="cursor:pointer">Name</th>
      <th data-col="archetype" data-label="Arch" onclick="sortProjects('archetype')" style="cursor:pointer">Arch</th>
      <th data-col="sessions" data-label="Sess" class="num" onclick="sortProjects('sessions')" style="cursor:pointer">Sess</th>
      <th data-col="error_rate" data-label="Err/S" class="num" onclick="sortProjects('error_rate')" style="cursor:pointer">Err/S</th>
      <th data-col="hours" data-label="Hours" class="num" onclick="sortProjects('hours')" style="cursor:pointer">Hours</th>
      <th data-col="cost" data-label="Cost" class="num" onclick="sortProjects('cost')" style="cursor:pointer">Cost</th>
    </tr></thead><tbody id="projects"></tbody></table>
  </div>
  <div class="panel" data-label="TOOLS">
    <h2>Tool Usage</h2>
    <div id="tools"></div>
  </div>
</div>

<div class="grid-2">
  <div class="panel" data-label="ERRORS">
    <h2>Error Trend <span class="sub" id="error-range"></span></h2>
    <div id="error-trend"></div>
  </div>
  <div class="panel" data-label="COST">
    <h2>Cost by Project</h2>
    <div id="cost-chart"></div>
  </div>
</div>

<div class="grid" style="grid-template-columns:1fr;">
  <div class="panel" data-label="TIMELINE">
    <h2>Session Activity <span class="sub" id="timeline-range"></span></h2>
    <div class="timeline" id="timeline"></div>
    <div class="timeline-labels" id="timeline-labels"></div>
  </div>
</div>

<div class="grid-3">
  <div class="panel" data-label="PROPOSALS">
    <h2>Proposals</h2>
    <div id="proposals"></div>
  </div>
  <div class="panel" data-label="RECENT">
    <h2>Recent Sessions</h2>
    <table><thead><tr>
      <th>Time</th><th>Project</th><th class="num">Dur</th><th class="num">Err</th><th>Out</th>
    </tr></thead><tbody id="recent"></tbody></table>
  </div>
  <div class="panel" data-label="TEMPORAL">
    <h2>By Hour <span class="sub">(UTC)</span></h2>
    <div id="temporal"></div>
  </div>
</div>

<div class="grid-3">
  <div class="panel" data-label="TOKENS">
    <h2>Token Breakdown</h2>
    <div id="tokens"></div>
  </div>
  <div class="panel" data-label="VERSIONS">
    <h2>Claude Versions</h2>
    <div id="versions"></div>
  </div>
  <div class="panel" data-label="WIKI">
    <h2>Knowledge Base</h2>
    <div id="wiki"></div>
  </div>
</div>

</div>

<script>
const D = {data_json};

// ─── State ───
let activeProject = null;
let dateRange = 30;

// ─── Helpers ───
function getTimeline() {{
  let tl = activeProject && D.per_project_timeline[activeProject]
    ? D.per_project_timeline[activeProject]
    : D.sessions_timeline;
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - dateRange);
  const cutStr = cutoff.toISOString().slice(0, 10);
  return tl.filter(d => d.date >= cutStr);
}}

function getTools() {{
  if (activeProject && D.per_project_tools[activeProject]) {{
    return D.per_project_tools[activeProject];
  }}
  return D.tool_breakdown;
}}

function getTemporal() {{
  if (activeProject && D.per_project_temporal[activeProject]) {{
    return D.per_project_temporal[activeProject];
  }}
  return D.temporal.by_hour;
}}

function getProjectData() {{
  if (activeProject) {{
    const p = D.projects.find(p => p.name === activeProject);
    if (p) return {{ sessions: p.sessions, hours: p.hours, errors: p.errors, tools: p.tools, cost: p.cost, tokens_m: 0, projects: 1 }};
  }}
  return D.summary;
}}

// ─── Render: Meta ───
function renderMeta() {{
  document.getElementById('meta').innerHTML = `
    Generated ${{D.generated_at.slice(0,16).replace('T',' ')}} UTC<br>
    ${{D.summary.total_sessions}} sessions / ${{D.summary.projects}} projects
  `;
}}

// ─── Render: Filter Bar ───
function renderFilterBar() {{
  document.querySelectorAll('.filter-btn').forEach(btn => {{
    const days = parseInt(btn.textContent);
    btn.classList.toggle('active', days === dateRange);
  }});
  const tag = document.getElementById('project-filter');
  if (activeProject) {{
    tag.classList.add('visible');
    document.getElementById('filter-name').textContent = activeProject;
  }} else {{
    tag.classList.remove('visible');
  }}
}}

// ─── Render: Stats ───
function formatDelta(d) {{
  if (d === null || d === undefined) return '';
  const sign = d > 0 ? '+' : '';
  const cls = d > 0 ? 'delta-up' : d < 0 ? 'delta-down' : 'delta-flat';
  return `<span class="delta ${{cls}}">${{sign}}${{d}}%</span>`;
}}

function fmtNum(n) {{ return (+n || 0).toLocaleString(undefined, {{maximumFractionDigits: 0}}); }}
function fmtMoney(n) {{
  n = +n || 0;
  if (n >= 1e6) return '$' + (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return '$' + (n/1e3).toFixed(1) + 'k';
  return '$' + n.toFixed(0);
}}
function fmtTokM(m) {{  // value is already in millions
  m = +m || 0;
  if (m >= 1e3) return (m/1e3).toFixed(1) + 'B';
  if (m >= 1) return Math.round(m) + 'M';
  return Math.round(m*1e3) + 'k';
}}
function renderStats() {{
  const s = getProjectData();
  const isFiltered = !!activeProject;
  const d = (!isFiltered && s.deltas) ? s.deltas : {{}};
  document.getElementById('stats').innerHTML = [
    ['SESSIONS', fmtNum(isFiltered ? s.sessions : s.total_sessions), '', d.sessions],
    ['HOURS', fmtNum(isFiltered ? s.hours : s.total_hours), '', d.hours],
    ['ERRORS', fmtNum(isFiltered ? s.errors : s.total_errors), (isFiltered ? s.errors : s.total_errors) > 100 ? 'alert' : '', d.errors],
    ['EST. COST', fmtMoney(isFiltered ? s.cost : s.total_cost || 0), (isFiltered ? s.cost : s.total_cost || 0) > 500 ? 'warn' : '', d.cost],
    ['TOKENS', fmtTokM(isFiltered ? s.tokens_m : (s.total_tokens_m || 0)), ''],
    ['PROJECTS', isFiltered ? 1 : s.projects, isFiltered ? '' : 'good'],
  ].map(([l,v,c,delta]) => `
    <div class="stat-cell">
      <div class="label">${{l}}</div>
      <div class="value ${{c}}">${{v}}</div>
      ${{delta !== undefined ? formatDelta(delta) : ''}}
    </div>
  `).join('');
}}

// ─── Render: Projects Table (sortable) ───
let projectSort = {{ col: 'errors', dir: -1 }};

function sortProjects(col) {{
  if (projectSort.col === col) projectSort.dir *= -1;
  else {{ projectSort.col = col; projectSort.dir = -1; }}
  renderProjects();
}}

function renderProjects() {{
  const cols = {{ name: 'name', archetype: 'archetype', sessions: 'sessions', error_rate: 'error_rate', hours: 'hours', cost: 'cost' }};
  const sorted = [...D.projects].sort((a, b) => {{
    const av = a[projectSort.col], bv = b[projectSort.col];
    if (typeof av === 'string') return projectSort.dir * av.localeCompare(bv);
    return projectSort.dir * (av - bv);
  }});

  // Update header sort indicators
  document.querySelectorAll('#project-header th').forEach(th => {{
    const col = th.dataset.col;
    if (!col) return;
    const arrow = col === projectSort.col ? (projectSort.dir > 0 ? ' \\u25B4' : ' \\u25BE') : '';
    th.textContent = th.dataset.label + arrow;
  }});

  document.getElementById('projects').innerHTML = sorted.map(p => `
    <tr class="clickable ${{activeProject === p.name ? 'active-project' : ''}}"
        onclick="setProject('${{p.name}}')">
      <td style="color:var(--accent-cream)">${{p.name}}</td>
      <td style="color:var(--text-muted);font-size:10px">${{p.archetype}}</td>
      <td class="num">${{p.sessions}}</td>
      <td class="num ${{p.error_rate > 20 ? 'err' : p.error_rate > 10 ? 'wrn' : ''}}">${{p.error_rate}}</td>
      <td class="num">${{p.hours}}</td>
      <td class="num ${{p.cost > 500 ? 'err' : p.cost > 100 ? 'wrn' : ''}}">${{p.cost.toFixed(0)}}</td>
    </tr>
  `).join('');
}}

// ─── Render: Tool Usage ───
function renderTools() {{
  const tools = getTools();
  const entries = Object.entries(tools).sort((a, b) => b[1] - a[1]).slice(0, 10);
  const maxTool = entries.length ? entries[0][1] : 1;
  document.getElementById('tools').innerHTML = entries.map(([t, c]) => `
    <div class="bar-row">
      <div class="bar-label">${{t}}</div>
      <div class="bar-track"><div class="bar-fill tool" style="width:${{c/maxTool*100}}%"></div></div>
      <div class="bar-value">${{c.toLocaleString()}}</div>
    </div>
  `).join('') || '<div class="no-data">No tool data</div>';
}}

// ─── Render: Timeline (sqrt scaling) ───
function renderTimeline() {{
  const tl = getTimeline();
  document.getElementById('timeline-range').textContent = '(' + dateRange + 'd)';
  if (!tl.length) {{
    document.getElementById('timeline').innerHTML = '<div class="no-data">No sessions in range</div>';
    document.getElementById('timeline-labels').innerHTML = '';
    return;
  }}
  const maxSess = Math.max(...tl.map(d => d.sessions), 1);
  const sqrtMax = Math.sqrt(maxSess);
  document.getElementById('timeline').innerHTML = tl.map(d => {{
    const pct = (Math.sqrt(d.sessions) / sqrtMax) * 100;
    return `<div class="timeline-bar" style="height:${{Math.max(pct, 3)}}%"
         data-tip="${{d.date}}: ${{d.sessions}} sess, ${{d.errors}} err, ${{d.hours}}h"></div>`;
  }}).join('');
  const step = Math.max(1, Math.ceil(tl.length / 7));
  document.getElementById('timeline-labels').innerHTML = tl
    .filter((_, i) => i % step === 0 || i === tl.length - 1)
    .map(d => `<span>${{d.date.slice(5)}}</span>`).join('');
}}

// ─── Render: Error Trend (SVG) ───
function renderErrorTrend() {{
  const tl = getTimeline();
  document.getElementById('error-range').textContent = '(' + dateRange + 'd)';
  if (tl.length < 2) {{
    document.getElementById('error-trend').innerHTML = '<div class="no-data">Need 2+ days of data</div>';
    return;
  }}

  const w = 600, h = 180;
  const pad = {{ t: 15, r: 10, b: 28, l: 42 }};
  const pw = w - pad.l - pad.r;
  const ph = h - pad.t - pad.b;

  const errors = tl.map(d => d.errors);
  const maxErr = Math.max(...errors, 1);

  const points = tl.map((d, i) => ({{
    x: pad.l + (i / (tl.length - 1)) * pw,
    y: pad.t + ph - (d.errors / maxErr) * ph
  }}));

  const linePath = points.map((p, i) => (i === 0 ? 'M' : 'L') + p.x.toFixed(1) + ',' + p.y.toFixed(1)).join(' ');
  const areaPath = linePath + ' L' + points[points.length-1].x.toFixed(1) + ',' + (pad.t + ph) + ' L' + points[0].x.toFixed(1) + ',' + (pad.t + ph) + ' Z';

  const gridLines = [0, 0.25, 0.5, 0.75, 1].map(frac => {{
    const y = pad.t + ph * (1 - frac);
    const val = Math.round(maxErr * frac);
    return `<line x1="${{pad.l}}" y1="${{y}}" x2="${{w - pad.r}}" y2="${{y}}" stroke="var(--border)" stroke-opacity="0.3" stroke-dasharray="${{frac === 0 ? 'none' : '2,3'}}"/>
      <text x="${{pad.l - 5}}" y="${{y + 3}}" text-anchor="end" fill="var(--text-muted)" font-size="9">${{val}}</text>`;
  }}).join('');

  const step = Math.max(1, Math.ceil(tl.length / 6));
  const xLabels = tl.map((d, i) => {{
    if (i % step !== 0 && i !== tl.length - 1) return '';
    const x = pad.l + (i / (tl.length - 1)) * pw;
    return `<text x="${{x}}" y="${{h - 4}}" text-anchor="middle" fill="var(--text-muted)" font-size="8">${{d.date.slice(5)}}</text>`;
  }}).join('');

  const dots = points.map((p, i) =>
    `<circle cx="${{p.x.toFixed(1)}}" cy="${{p.y.toFixed(1)}}" r="2.5" fill="var(--accent-red)" opacity="0.6">
      <title>${{tl[i].date}}: ${{tl[i].errors}} errors</title>
    </circle>`
  ).join('');

  document.getElementById('error-trend').innerHTML = `
    <svg class="svg-chart" viewBox="0 0 ${{w}} ${{h}}">
      <defs>
        <linearGradient id="err-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--accent-red)" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="var(--accent-red)" stop-opacity="0.03"/>
        </linearGradient>
      </defs>
      ${{gridLines}}
      <path d="${{areaPath}}" fill="url(#err-grad)"/>
      <path d="${{linePath}}" fill="none" stroke="var(--accent-red)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      ${{dots}}
      ${{xLabels}}
    </svg>
  `;
}}

// ─── Render: Cost Chart ───
function renderCostChart() {{
  const projects = activeProject
    ? D.projects.filter(p => p.name === activeProject)
    : D.projects.filter(p => p.cost > 0).sort((a, b) => b.cost - a.cost).slice(0, 10);
  const maxCost = projects.length ? Math.max(...projects.map(p => p.cost)) : 1;
  document.getElementById('cost-chart').innerHTML = projects.map(p => `
    <div class="bar-row">
      <div class="bar-label" style="width:100px">${{p.name}}</div>
      <div class="bar-track"><div class="bar-fill cost" style="width:${{p.cost / maxCost * 100}}%"></div></div>
      <div class="bar-value">${{p.cost.toFixed(0)}}</div>
    </div>
  `).join('') || '<div class="no-data">No cost data</div>';
}}

// ─── Render: Proposals ───
function renderProposals() {{
  const filtered = activeProject
    ? D.proposals.filter(p => p.project === activeProject || p.project?.toLowerCase() === activeProject?.toLowerCase())
    : D.proposals;
  document.getElementById('proposals').innerHTML = filtered.slice(0, 8).map(p => `
    <div class="proposal">
      <span class="proposal-status ${{p.status}}">${{p.status}}</span>
      <div>
        <div class="proposal-text">${{p.description}}</div>
        <div class="proposal-meta">${{p.project}} · ${{p.type}} · ${{p.timestamp}}</div>
      </div>
    </div>
  `).join('') || '<div class="no-data">No proposals</div>';
}}

// ─── Render: Recent Sessions ───
function renderRecent() {{
  const filtered = activeProject
    ? D.recent_sessions.filter(s => s.project === activeProject)
    : D.recent_sessions;
  document.getElementById('recent').innerHTML = filtered.map(s => `
    <tr>
      <td style="font-size:10px">${{s.date.slice(5,16)}}</td>
      <td style="color:var(--accent-cream)">${{s.project}}</td>
      <td class="num">${{s.duration_m}}m</td>
      <td class="num ${{s.errors > 5 ? 'err' : ''}}">${{s.errors}}</td>
      <td style="font-size:10px;color:var(--text-muted)">${{s.outcome}}</td>
    </tr>
  `).join('') || '<tr><td colspan="5" class="no-data">No sessions</td></tr>';
}}

// ─── Render: Temporal ───
function renderTemporal() {{
  const hours = getTemporal();
  const maxH = Math.max(...Object.values(hours), 1);
  document.getElementById('temporal').innerHTML = Array.from({{length: 24}}, (_, i) => {{
    const c = hours[i] || hours[String(i)] || 0;
    return `<div class="bar-row">
      <div class="bar-label" style="width:24px">${{String(i).padStart(2, '0')}}</div>
      <div class="bar-track"><div class="bar-fill hour" style="width:${{c / maxH * 100}}%"></div></div>
      <div class="bar-value">${{c}}</div>
    </div>`;
  }}).join('');
}}

// ─── Render: Token Breakdown ───
function renderTokens() {{
  const tt = D.token_types;
  const entries = Object.entries(tt);
  if (!entries.length) {{
    document.getElementById('tokens').innerHTML = '<div class="no-data">No token data yet</div>';
    return;
  }}
  const labels = {{ input: 'Input', output: 'Output', cache_read: 'Cache Read', cache_create: 'Cache Create' }};
  const sorted = entries.sort((a, b) => b[1] - a[1]);
  const maxVal = sorted[0][1];
  const total = sorted.reduce((s, [, v]) => s + v, 0);
  document.getElementById('tokens').innerHTML = sorted.map(([k, v]) => {{
    const pct = total > 0 ? (v / total * 100).toFixed(1) : 0;
    const label = labels[k] || k;
    const display = v > 1e9 ? (v / 1e9).toFixed(1) + 'B' : v > 1e6 ? (v / 1e6).toFixed(1) + 'M' : v > 1e3 ? (v / 1e3).toFixed(1) + 'K' : v;
    return `<div class="bar-row">
      <div class="bar-label" style="width:90px">${{label}}</div>
      <div class="bar-track"><div class="bar-fill token" style="width:${{v / maxVal * 100}}%"></div></div>
      <div class="bar-value">${{display}} (${{pct}}%)</div>
    </div>`;
  }}).join('');
}}

// ─── Render: Versions ───
function renderVersions() {{
  const entries = Object.entries(D.model_versions);
  const maxV = entries.length ? Math.max(...entries.map(e => e[1])) : 1;
  document.getElementById('versions').innerHTML = entries.map(([v, c]) => `
    <div class="bar-row">
      <div class="bar-label">${{v}}</div>
      <div class="bar-track"><div class="bar-fill highlight" style="width:${{c / maxV * 100}}%"></div></div>
      <div class="bar-value">${{c}}</div>
    </div>
  `).join('') || '<div class="no-data">No version data</div>';
}}

// ─── Render: Wiki ───
function renderWiki() {{
  const w = D.wiki;
  document.getElementById('wiki').innerHTML = `
    <div style="font-family:var(--font-mono);font-size:13px;color:var(--text-secondary);line-height:2">
      Articles: <span style="color:var(--accent-cream)">${{w.total_articles}}</span><br>
      Words: <span style="color:var(--accent-cream)">${{w.total_words.toLocaleString()}}</span><br>
      ${{Object.entries(w.sections || {{}}).map(([s, d]) =>
        `${{s}}: ${{d.articles}} articles`
      ).join('<br>')}}
    </div>
  `;
}}

// ─── Animate bars ───
function animateBars() {{
  setTimeout(() => {{
    document.querySelectorAll('.bar-fill').forEach(el => {{
      const w = el.style.width;
      el.style.width = '0%';
      requestAnimationFrame(() => el.style.width = w);
    }});
  }}, 50);
}}

// ─── Event Handlers ───
function setProject(name) {{
  activeProject = activeProject === name ? null : name;
  renderAll();
}}

function setDateRange(days) {{
  dateRange = days;
  renderAll();
}}

// ─── Render All ───
function renderAll() {{
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
}}

renderAll();
</script>
</body>
</html>'''


if __name__ == "__main__":
    generate_dashboard()
