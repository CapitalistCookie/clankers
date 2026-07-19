"""Syntax gate for the dashboard SPA's JavaScript.

The SPA's JS used to be inline JS assembled inside Python strings — a class of
bug (Python consuming a backslash and splitting a JS regex literal) shipped
invisibly unless the REAL rendered page was checked. The JS now lives in real
files under lib/web/ (served at /app/), so we node --check those directly. We
also re-check whatever inline <script> blocks survive in the rendered page —
which, post-extraction, must be ONLY the dynamic data bootstrap. Caught a live
bug on 2026-07-19.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

# Import-time isolation dance (see test_webauth.py / test_serve_state.py):
# serve pulls in webauth, which captures CLANKER_DATA at import.
_OLD_DATA = os.environ.get("CLANKER_DATA")
os.environ["CLANKER_DATA"] = tempfile.mkdtemp(prefix="clk-spajs-test-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from dashboard import _build_html  # noqa: E402
import serve  # noqa: E402

if _OLD_DATA is None:
    os.environ.pop("CLANKER_DATA", None)
else:
    os.environ["CLANKER_DATA"] = _OLD_DATA

_WEB = os.path.join(os.path.dirname(__file__), "..", "lib", "web")


def _node_check(source, label):
    with tempfile.NamedTemporaryFile(
            "w", suffix=f"-{label}.js", delete=False) as f:
        f.write(source)
        path = f.name
    try:
        r = subprocess.run(["node", "--check", path],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{label} fails node --check:\n{r.stderr[:800]}"
    finally:
        os.unlink(path)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_all_inline_scripts_parse():
    # 1) The extracted SPA files (lib/web/*.js), syntax-checked directly.
    web_js = sorted(glob.glob(os.path.join(_WEB, "*.js")))
    assert web_js, "no lib/web/*.js files found — extraction missing?"
    for path in web_js:
        with open(path) as f:
            _node_check(f.read(), os.path.basename(path))

    # 2) Whatever inline <script> blocks survive in the rendered page. After the
    #    extraction that must be ONLY the data bootstrap (const D = {...}); the
    #    rest is now loaded via <script defer src="/app/..."> (which carry a src=
    #    attribute and so don't match the bare-<script> regex below).
    html = _build_html("{}").replace(
        "</body>", serve._live_features_html() + "\n</body>")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(blocks) == 1, (
        f"expected exactly one inline <script> (the data bootstrap), got "
        f"{len(blocks)} — did inline JS creep back in?")
    assert blocks[0].strip().startswith("const D ="), (
        f"the lone inline script should be the data bootstrap, got: "
        f"{blocks[0].strip()[:80]!r}")
    _node_check(blocks[0], "inline-bootstrap")
