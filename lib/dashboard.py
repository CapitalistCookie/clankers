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

    # Standalone file is opened over file:// — absolute /app/ URLs don't resolve
    # there, so inline the assets to keep it self-contained. The SERVED dashboard
    # (serve.py) keeps the <link>/<script defer> form and gets them from /app/.
    html = _inline_static_assets(_build_html(data_json))

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
<link rel="stylesheet" href="/app/app.css">
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
</script>
<script defer src="/app/app.js"></script>
</body>
</html>'''


def _inline_static_assets(html):
    """Inline the /app/ CSS+JS into the page so a standalone dashboard.html works
    over file:// (absolute /app/ URLs don't resolve there). Plain str.replace
    (never %/format/f-string) so the assets' own braces/backslashes go in verbatim."""
    web = os.path.join(os.path.dirname(__file__), "web")
    with open(os.path.join(web, "app.css")) as f:
        css = f.read()
    with open(os.path.join(web, "app.js")) as f:
        js = f.read()
    html = html.replace(
        '<link rel="stylesheet" href="/app/app.css">',
        "<style>\n" + css + "</style>")
    html = html.replace(
        '<script defer src="/app/app.js"></script>',
        "<script>\n" + js + "</script>")
    return html


if __name__ == "__main__":
    generate_dashboard()
