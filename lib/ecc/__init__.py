"""Optional ECC-derived features (ported from affaan-m/ECC) — OFF by default.

This whole subsystem is opt-in. The `clanker ecc ...` CLI commands always run
(invoking one is itself opting in), but the *automatic* integrations — the
dashboard risk indicator and ECC-derived health alerts — only activate when
enabled via either:
  - env:       CLANKER_ECC=1   (or on/true/yes/all)
  - registry:  defaults.ecc_features: true   in ~/projects/.clanker.yaml

When disabled, clanker behaves exactly as before — nothing in core paths calls
into ecc.* unless `ecc.enabled()` returns True. See docs/ECC-feature-mining.md
for provenance of each module.

Modules (each pure-stdlib, independently testable):
  risk    — per-tool-call danger score (allow/review/confirm/block)
  parked  — stuck / waiting-for-input session detector (transcript scan)
  trend   — 7d/30d success-rate trend + failure clustering (scoreboard upgrade)
  budget  — per-model cost incl. cache tiers + budget alert ladder
  overlap — files touched by >1 active session (collision detection)
  guard   — destructive-command + governance/secret + --no-verify classifier
"""

import os

__all__ = ["enabled", "risk", "parked", "trend", "budget", "overlap", "guard"]

_TRUE = {"1", "true", "on", "yes", "all"}
_FALSE = {"0", "false", "off", "no", ""}


def _registry_default():
    try:
        import yaml
        reg = os.environ.get("CLANKER_REGISTRY", os.path.expanduser("~/projects/.clanker.yaml"))
        with open(reg) as f:
            data = yaml.safe_load(f) or {}
        return bool(data.get("defaults", {}).get("ecc_features"))
    except Exception:
        return False


def enabled():
    """True only if the operator opted in (env or registry). Default: False."""
    raw = os.environ.get("CLANKER_ECC", "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return _registry_default() if raw == "" else False
    return _registry_default()
