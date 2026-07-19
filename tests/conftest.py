"""Shared test environment: isolate the WHOLE suite from the live /data/clanker store.

2026-07-19: the suite inherited the default CLANKER_DATA, so every pytest run
(pre-push AND post-commit via localci) appended fixture sessions to the
production store — 165 of 338 sessions in one 30-day window were test rows, and
a hardcoded-timestamp fixture grew raw/sessions/2026-04-07.jsonl on every run.
This mirrors the CLANKER_REGISTRY fixture-pinning (2026-07-05) for the data dir.

autouse + os.environ (not monkeypatch): most tests shell out to bin/clanker
with {**os.environ, ...}, and two ingest tests spawn subprocess.run directly —
the env var must be inherited by every child process, not just this one.
"""

import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_clanker_data():
    with tempfile.TemporaryDirectory(prefix="clanker-test-data-") as d:
        for sub in (
            "raw/sessions",
            "raw/health",
            "proposals",
            "alerts",
            "reports",
            "audit",
            "wiki",
            "plugins/analyzers",
            "plugins/health-checks",
        ):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        os.environ["CLANKER_DATA"] = d
        yield d
