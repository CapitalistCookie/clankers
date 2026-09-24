"""P12: alerts carry a project field + ignored-days escalation (audit §7 —
the unpushed-commits warning sat active 3 days, visible only inside the alert
file). Hermetic: alerts.ALERTS_DIR + briefing.DATA_DIR monkeypatched to tmp;
_ntfy replaced by a recorder (no network)."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

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


# ── 2026-09-24: write only on change; first_seen from the alert's own fields ──

def _backdate_mtime(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_unchanged_alert_is_not_rewritten(adir):
    """The 15-min pass rewrote every alert on every run, so every mtime stayed
    fresh and mtime-based gc never expired anything (1,154 files, one second)."""
    alerts._create_alert("steady", "info", "t", "steady info")
    alerts._escalate_ignored(days=3)                  # first pass stamps ignored_days
    p = adir / "steady.json"
    body = p.read_bytes()
    _backdate_mtime(p, 30)
    stamp = p.stat().st_mtime_ns
    assert alerts._escalate_ignored(days=3) == []
    assert p.stat().st_mtime_ns == stamp               # not even touched
    assert p.read_bytes() == body


def test_missing_first_seen_is_added_once_from_own_fields(adir):
    adir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    created = (now - timedelta(days=5, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (adir / "c.json").write_text(json.dumps(
        {"severity": "info", "message": "c", "created": created}))
    (adir / "t.json").write_text(json.dumps(
        {"severity": "info", "message": "t",
         "ts": (now - timedelta(days=2, hours=1)).timestamp()}))
    (adir / "m.json").write_text('{"severity":"info","message":"printf-style, no times"}')
    _backdate_mtime(adir / "m.json", 4.05)
    alerts._escalate_ignored(days=3)
    c = json.loads((adir / "c.json").read_text())
    assert c["first_seen"] == created and c["ignored_days"] == 5
    assert json.loads((adir / "t.json").read_text())["ignored_days"] == 2
    m = json.loads((adir / "m.json").read_text())
    assert m["first_seen"] and m["ignored_days"] == 4   # from the mtime, last resort
    stamps = {f: (adir / f).stat().st_mtime_ns for f in ("c.json", "t.json", "m.json")}
    for f in stamps:
        _backdate_mtime(adir / f, 1)
    stamps = {f: (adir / f).stat().st_mtime_ns for f in stamps}
    alerts._escalate_ignored(days=3)                  # now stable: no rewrite
    assert {f: (adir / f).stat().st_mtime_ns for f in stamps} == stamps


def test_unparseable_first_seen_is_left_alone(adir):
    adir.mkdir(parents=True)
    p = adir / "odd.json"
    p.write_text(json.dumps({"severity": "warning", "message": "odd",
                             "first_seen": "yesterday-ish"}))
    _backdate_mtime(p, 10)
    body, stamp = p.read_bytes(), p.stat().st_mtime_ns
    assert alerts._escalate_ignored(days=3) == []
    assert p.read_bytes() == body and p.stat().st_mtime_ns == stamp


def test_alert_birth_order_and_formats(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("{}")
    _backdate_mtime(p, 3)
    b = alerts.alert_birth
    assert b({"first_seen": "2026-09-01T00:00:00Z", "created": "2026-01-01T00:00:00Z"}) \
        == datetime(2026, 9, 1)
    assert b({"first_seen": "junk", "created": "2026-09-02T02:00:00+02:00"}) \
        == datetime(2026, 9, 2)                        # offset normalized to UTC
    assert b({"ts": 1790000000}) == datetime(2026, 9, 21, 14, 13, 20)
    assert b({"ts": 1790000000000}) == datetime(2026, 9, 21, 14, 13, 20)   # epoch ms
    assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - b({}, str(p)))
               - timedelta(days=3)) < timedelta(minutes=5)          # mtime fallback
    assert b({}) is None
