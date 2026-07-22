"""P12: alerts carry a project field + ignored-days escalation (audit §7 —
the unpushed-commits warning sat active 3 days, visible only inside the alert
file). Hermetic: alerts.ALERTS_DIR + briefing.DATA_DIR monkeypatched to tmp;
_ntfy replaced by a recorder (no network)."""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import alerts    # noqa: E402
import briefing  # noqa: E402


@pytest.fixture
def adir(tmp_path, monkeypatch):
    d = tmp_path / "alerts"
    monkeypatch.setattr(alerts, "ALERTS_DIR", str(d))
    return d


def _age(adir, alert_id, days):
    """Backdate an alert's first_seen."""
    p = adir / f"{alert_id}.json"
    a = json.loads(p.read_text())
    a["first_seen"] = (datetime.utcnow() - timedelta(days=days, hours=1)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text(json.dumps(a))


def test_create_alert_stores_project_and_preserves_escalation_state(adir):
    alerts._create_alert("ci-red", "warning", "ci", "suite red", project="clanker")
    a = json.loads((adir / "ci-red.json").read_text())
    assert a["project"] == "clanker"
    # simulate an escalation stamp, then a cron re-raise
    a.update({"escalated_at": "2026-07-19T00:00:00Z", "escalation_delivered": True,
              "ignored_days": 3})
    (adir / "ci-red.json").write_text(json.dumps(a))
    alerts._create_alert("ci-red", "warning", "ci", "suite red", project="clanker")
    a2 = json.loads((adir / "ci-red.json").read_text())
    assert a2["escalated_at"] == "2026-07-19T00:00:00Z"   # survived the rewrite
    assert a2["escalation_delivered"] is True
    assert a2["first_seen"] == a["first_seen"]


def test_ignored_warning_escalates_once(adir, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "_ntfy", lambda *a, **kw: sent.append(a) or True)
    alerts._create_alert("old-warn", "warning", "t", "ignored warning")
    alerts._create_alert("fresh-warn", "warning", "t", "fresh warning")
    alerts._create_alert("old-info", "info", "t", "old info")
    _age(adir, "old-warn", 4)
    _age(adir, "old-info", 10)

    esc = alerts._escalate_ignored(days=3)
    assert esc == ["old-warn"]                            # warning + old → escalated
    assert len(sent) == 1 and "4d" in sent[0][0]
    a = json.loads((adir / "old-warn.json").read_text())
    assert a["ignored_days"] == 4 and a["escalated_at"] and a["escalation_delivered"]
    # info is stamped but never escalated; fresh warning untouched
    assert json.loads((adir / "old-info.json").read_text())["ignored_days"] == 10
    assert "escalated_at" not in json.loads((adir / "old-info.json").read_text())
    assert "escalated_at" not in json.loads((adir / "fresh-warn.json").read_text())
    # second pass: one-time semantics — nothing new fires
    assert alerts._escalate_ignored(days=3) == []
    assert len(sent) == 1
    # and a cron re-raise of the alert must NOT reset that (create preserves it)
    alerts._create_alert("old-warn", "warning", "t", "ignored warning")
    assert alerts._escalate_ignored(days=3) == []


def test_escalation_without_ntfy_still_stamps_once(adir, monkeypatch):
    monkeypatch.delenv("CLANKER_NTFY_TOPIC", raising=False)
    alerts._create_alert("lonely", "critical", "t", "no ntfy configured")
    _age(adir, "lonely", 5)
    assert alerts._escalate_ignored(days=3) == ["lonely"]
    a = json.loads((adir / "lonely.json").read_text())
    assert a["escalated_at"] and a["escalation_delivered"] is False
    assert alerts._escalate_ignored(days=3) == []         # still one-time


def test_briefing_scopes_project_alerts(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "alerts").mkdir(parents=True)
    (data / "alerts" / "a.json").write_text(json.dumps(
        {"project": "projX", "message": "projX suite red", "ignored_days": 5,
         "severity": "warning"}))
    (data / "alerts" / "b.json").write_text(json.dumps(
        {"message": "disk filling", "severity": "warning"}))
    monkeypatch.setattr(briefing, "DATA_DIR", str(data))

    out_x = briefing.generate_briefing("projX", str(tmp_path)) or ""
    assert "Alerts (projX)" in out_x
    assert "[ignored 5d] projX suite red" in out_x
    assert "disk filling" in out_x                        # global still shown
    out_y = briefing.generate_briefing("projY", str(tmp_path)) or ""
    assert "projX suite red" not in out_y                 # scoped away
    assert "disk filling" in out_y
