"""clanker wrap — run a scripted `claude -p` call pinned, isolated and accounted.

Scripted callers (cron jobs, batch backends) used to run a bare `claude -p`. That
inherits everything the interactive fleet loads: the user layer (global
CLAUDE.md, user skills, user hooks, the global model and effort), every MCP
server and claude.ai connector, a transcript per call, and the parent session's
own environment (CLAUDE_CODE_EFFORT_LEVEL overrides --effort in the child).

`clanker wrap` runs the same call with:

  * `--setting-sources project,local`  the user layer is not loaded
                                        (`--allow-user-layer` keeps it)
  * `--strict-mcp-config`               only MCP servers from --mcp-config
  * `--disable-slash-commands`          no skills are listed
  * `--no-session-persistence`          no transcript is written
  * a pinned `--model` and `--effort`   both are required
  * `--max-budget-usd`                  from --max-cost (default 5.00 USD,
                                        CLANKER_WRAP_MAX_COST; 0 = no cap)
  * a fixed per-caller working dir      $CLANKER_DATA/run/<caller>, so the
                                        prompt prefix stays cacheable
  * a hard `--timeout`                  the process group is killed; exit 124
  * a parent-session-free environment   CLAUDECODE, CLAUDE_EFFORT and the
                                        CLAUDE_CODE_* session variables are
                                        removed (auth/provider ones are kept)
  * the user layer's policy, re-applied auto-memory off, Workflows off,
                                        auto-updater off, no commit
                                        attribution trailer (see POLICY_*)

Why the policy: dropping the user layer also drops its `autoMemoryEnabled:
false` (measured: +2,982 first-turn tokens and a live memory dir), and with
skills disabled the Workflow tool inlines its whole authoring guide (+34,780
schema chars). A Haiku "Reply with exactly: OK" on 2.1.280 measured 22,122
first-turn tokens with the full harness, 23,536 isolated without the policy,
14,282 isolated with it.

It sets CLANKER_WRAP=1 and CLANKER_WRAP_CALLER=<caller> in the child and appends
one `kind: nested` row with Claude Code's own `total_cost_usd` to
$CLANKER_DATA/raw/sessions/<utc-date>.jsonl (the user-layer session hooks do not
run for a wrapped call, so this row is its only telemetry).

Stdout keeps the `claude -p` contract of the requested --output-format: text
(the default) prints `.result`, json prints claude's JSON verbatim, stream-json
streams claude's lines through. The exit code is claude's, 124 on timeout,
2 on a usage error, 127 when the claude binary is missing.

A command that is not claude (`clanker wrap make test`) runs as before: a
session_start/session_end pair is ingested around it. Its exit code is now the
command's exit code (it was always 0).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

EFFORTS = ("low", "medium", "high", "xhigh", "max")
OUTPUT_FORMATS = ("text", "json", "stream-json")
DEFAULT_MAX_COST_USD = 5.0
TIMEOUT_RC = 124
USAGE_RC = 2
NOT_FOUND_RC = 127
KILL_GRACE_S = 10.0

# Flags checked against `claude --help` on 2.1.280 (and the hidden --max-turns,
# which the SDK itself passes).
USER_LAYER_OFF = ["--setting-sources", "project,local"]
ISOLATION_FLAGS = ["--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]

# The user layer's policy that must survive dropping the layer.
POLICY_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",   # autoMemoryEnabled: false
    "CLAUDE_CODE_DISABLE_WORKFLOWS": "1",     # skills off inlines the Workflow guide
    "DISABLE_AUTOUPDATER": "1",               # the installed version stays pinned
}
POLICY_SETTINGS = {"attribution": {"commit": ""}}   # no Co-Authored-By trailer

# Claude options that wrap owns. A value given here AND in the passthrough args
# is a usage error, so no call runs with two different pins.
_VALUE_OPTS = {"--model", "--effort", "--max-budget-usd", "--max-turns",
               "--output-format", "--setting-sources", "--settings"}
_BOOL_OPTS = {"-p", "--print", "--strict-mcp-config", "--disable-slash-commands",
              "--no-session-persistence"}

# Environment a parent Claude Code session exports. None of it may reach the
# child: CLAUDE_CODE_EFFORT_LEVEL overrides --effort, CLAUDE_CODE_MESSAGING_*
# joins the parent's agent team, CLAUDE_CODE_MAX_OUTPUT_TOKENS caps the reply.
_STRIP_EXACT = {"CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_PID", "CLAUDE_ENV_FILE",
                "CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID"}
_KEEP_CLAUDE_CODE = ("CLAUDE_CODE_OAUTH", "CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_",
                     "CLAUDE_CODE_API_KEY", "CLAUDE_CODE_CLIENT_")

_CALLER_RE = re.compile(r"[^A-Za-z0-9._-]+")


class UsageError(Exception):
    """A wrap invocation that cannot run as given (exit 2)."""


# ─── helpers ─────────────────────────────────────────────────────────────────

def data_dir() -> str:
    """$CLANKER_DATA, read at call time (repo law 9)."""
    return os.environ.get("CLANKER_DATA", "/data/clanker")


def caller_slug(caller: str) -> str:
    """A caller name safe for a directory name."""
    slug = _CALLER_RE.sub("-", caller or "").strip("-.")
    return slug or "adhoc"


def is_claude_command(tokens: list[str], claude_flags_given: bool) -> bool:
    """True when `tokens` is a claude call: `claude ...`, bare claude args
    (`-p ...`), or anything at all once a claude-only wrap flag was given."""
    if not tokens:
        return True
    first = tokens[0]
    if os.path.basename(first) in ("claude", "claude.exe"):
        return True
    if first.startswith("-"):
        return True
    return claude_flags_given


def split_claude_args(args: list[str]) -> tuple[dict, list[str]]:
    """Separate the options wrap owns from the rest of a claude argv.

    Returns ({option: value or True}, remaining_args). `--opt value` and
    `--opt=value` are both read; a bare `--` ends option parsing, and the
    tokens after it pass through untouched.
    """
    owned: dict = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            rest.extend(args[i:])
            break
        name, eq, val = tok.partition("=")
        if name in _VALUE_OPTS:
            if not eq:
                if i + 1 >= len(args):
                    raise UsageError(f"{name} needs a value")
                val = args[i + 1]
                i += 1
            if name in owned:
                raise UsageError(f"{name} is given twice in the claude arguments")
            owned[name] = val
        elif tok in _BOOL_OPTS:
            owned[tok] = True
        else:
            rest.append(tok)
        i += 1
    return owned, rest


def child_env(base: dict, caller: str) -> tuple[dict, list[str]]:
    """The child's environment and the names removed from `base`."""
    env, removed = {}, []
    for k, v in base.items():
        if k in _STRIP_EXACT or (k.startswith("CLAUDE_CODE_")
                                 and not k.startswith(_KEEP_CLAUDE_CODE)):
            removed.append(k)
            continue
        env[k] = v
    env.update(POLICY_ENV)
    env["CLANKER_WRAP"] = "1"
    env["CLANKER_WRAP_CALLER"] = caller
    return env, sorted(removed)


def find_claude(env: dict) -> str | None:
    """CLANKER_WRAP_CLAUDE, else `claude` on PATH, else the usual install dirs
    (cron runs with a minimal PATH)."""
    explicit = env.get("CLANKER_WRAP_CLAUDE")
    if explicit:
        return explicit
    found = shutil.which("claude", path=env.get("PATH"))
    if found:
        return found
    for cand in ("~/.npm-global/bin/claude", "~/.local/bin/claude", "~/.claude/local/claude",
                 "/usr/local/bin/claude"):
        p = os.path.expanduser(cand)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def merged_settings(caller_value: str | None) -> str:
    """POLICY_SETTINGS merged with the caller's own --settings (inline JSON or
    a JSON file); the caller's keys win. Returned as inline JSON."""
    if caller_value is None:
        return json.dumps(POLICY_SETTINGS, separators=(",", ":"))
    try:
        theirs = json.loads(caller_value)
    except ValueError:
        path = os.path.expanduser(caller_value)
        try:
            with open(path) as f:
                theirs = json.load(f)
        except (OSError, ValueError) as e:
            raise UsageError(f"--settings is neither JSON nor a readable JSON file: {e}")
    if not isinstance(theirs, dict):
        raise UsageError("--settings must be a JSON object")
    return json.dumps(_deep_merge(POLICY_SETTINGS, theirs), separators=(",", ":"))


def build_plan(*, tokens: list[str], model: str | None, effort: str | None,
               max_cost: float | None, max_turns: int | None, caller: str,
               cwd: str | None, allow_user_layer: bool, base_env: dict) -> dict:
    """Resolve a claude-mode invocation into the exact argv, cwd and env.

    Raises UsageError for anything that must not run (missing pins, a pin
    given twice, a user-layer override without --allow-user-layer).
    """
    claude_bin = None
    if tokens and os.path.basename(tokens[0]) in ("claude", "claude.exe"):
        claude_bin, tokens = tokens[0], tokens[1:]
        if claude_bin == os.path.basename(claude_bin):
            claude_bin = None            # bare `claude`: resolve it below
    owned, rest = split_claude_args(tokens)

    def pin(flag, wrap_value, label):
        inner = owned.get(flag)
        if wrap_value is not None and inner is not None:
            raise UsageError(f"{label} is given to wrap and to claude ({flag}); give it once")
        return wrap_value if wrap_value is not None else inner

    model = pin("--model", model, "--model")
    effort = pin("--effort", effort, "--effort")
    cap = pin("--max-budget-usd", None if max_cost is None else str(max_cost), "--max-cost")
    turns = pin("--max-turns", None if max_turns is None else str(max_turns), "--max-turns")
    if not model:
        raise UsageError("--model is required: a wrapped call pins its model "
                         "(the user layer's default is not inherited)")
    if not effort:
        raise UsageError(f"--effort is required ({', '.join(EFFORTS)}): a wrapped call "
                         "pins its effort (the user layer's default is not inherited)")
    if effort not in EFFORTS:
        raise UsageError(f"--effort must be one of {', '.join(EFFORTS)} (got {effort!r})")
    if "--setting-sources" in owned:
        raise UsageError("--setting-sources is set by wrap; use --allow-user-layer "
                         "to keep the user layer")
    if cap is None:
        env_cap = base_env.get("CLANKER_WRAP_MAX_COST")
        cap = env_cap if env_cap not in (None, "") else str(DEFAULT_MAX_COST_USD)
    try:
        cap_f = float(cap)
    except ValueError:
        raise UsageError(f"--max-cost must be a number of USD (got {cap!r})")
    if cap_f < 0:
        raise UsageError("--max-cost must be 0 (no cap) or more")
    fmt = owned.get("--output-format") or "text"
    if fmt not in OUTPUT_FORMATS:
        raise UsageError(f"--output-format must be one of {', '.join(OUTPUT_FORMATS)}")

    caller = caller_slug(caller)
    run_cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else \
        os.path.join(data_dir(), "run", caller)
    env, removed = child_env(base_env, caller)
    claude_bin = claude_bin or find_claude(base_env)

    argv = [claude_bin or "claude", "-p"]
    if not allow_user_layer:
        argv += USER_LAYER_OFF
    argv += ISOLATION_FLAGS
    argv += ["--settings", merged_settings(owned.get("--settings"))]
    argv += ["--model", model, "--effort", effort]
    if cap_f > 0:
        argv += ["--max-budget-usd", _fmt_usd(cap_f)]
    if turns is not None:
        argv += ["--max-turns", str(turns)]
    # text is served from the JSON result, so the row always gets the usage.
    argv += ["--output-format", "stream-json" if fmt == "stream-json" else "json"]
    argv += rest
    return {"argv": argv, "cwd": run_cwd, "env": env, "env_removed": removed,
            "output_format": fmt, "model": model, "effort": effort,
            "max_cost_usd": cap_f, "caller": caller, "claude_found": claude_bin is not None,
            "user_layer": bool(allow_user_layer)}


def _fmt_usd(x: float) -> str:
    return ("%.4f" % x).rstrip("0").rstrip(".") if x != int(x) else str(int(x))


def parse_result(stdout: str, fmt: str) -> dict | None:
    """Claude's final result object from its stdout, or None."""
    if fmt == "stream-json":
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                return obj
        return None
    try:
        obj = json.loads(stdout)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def first_turn_context(result: dict) -> int | None:
    """Input-side tokens of the first API call, when the run made exactly one."""
    if not result or result.get("num_turns") != 1:
        return None
    u = result.get("usage") or {}
    return int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
        + int(u.get("cache_creation_input_tokens") or 0)


def build_row(plan: dict, result: dict | None, *, rc: int, timed_out: bool, wall_s: float,
              project: str, caller_cwd: str) -> dict:
    """The telemetry row for one wrapped call (raw/sessions schema + wrap fields)."""
    result = result or {}
    u = result.get("usage") or {}
    model_usage = result.get("modelUsage") or {}
    models = sorted(model_usage, key=lambda m: -float((model_usage[m] or {}).get("costUSD") or 0))
    cost = result.get("total_cost_usd")
    if timed_out:
        outcome = "timeout"
    elif rc != 0 or result.get("is_error"):
        outcome = "error"
    else:
        outcome = "ok"
    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": result.get("session_id") or f"wrap-{uuid.uuid4()}",
        "project": project,
        "cwd": plan["cwd"],
        "caller_cwd": caller_cwd,
        "kind": "nested",
        "nested": True,
        "entrypoint": "sdk-cli",
        "wrap": True,
        "caller": plan["caller"],
        "model": models[0] if models else plan["model"],
        "model_requested": plan["model"],
        "effort": plan["effort"],
        "user_layer": plan["user_layer"],
        "duration_s": int(round(wall_s)),
        "wall_clock_s": int(round(wall_s)),
        "outcome": outcome,
        "exit_code": rc,
        "is_error": bool(result.get("is_error")) if result else None,
        "subtype": result.get("subtype"),
        "stop_reason": result.get("stop_reason"),
        "num_turns": result.get("num_turns"),
        "first_call_ctx": first_turn_context(result),
        "permission_denials": len(result.get("permission_denials") or []),
        "tokens": {
            "input": int(u.get("input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_create": int(u.get("cache_creation_input_tokens") or 0),
        },
        "total_cost_usd": cost,
        "estimated_cost_usd": round(float(cost), 4) if cost is not None else None,
        "cost_source": "claude_code_total_cost_usd" if cost is not None else None,
        "max_cost_usd": plan["max_cost_usd"],
        "duration_api_ms": result.get("duration_api_ms"),
        "output_format": plan["output_format"],
    }


def append_row(row: dict) -> str:
    """Append `row` to $CLANKER_DATA/raw/sessions/<utc-date>.jsonl under the
    same `<file>.lock` flock the session hooks take. Returns the path."""
    import fcntl
    d = os.path.join(data_dir(), "raw", "sessions")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")
    with open(path + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
    return path


def resolve_project(explicit: str | None, caller_cwd: str, caller: str) -> str:
    """--project, else the project that owns the caller's cwd, else the caller."""
    if explicit:
        return explicit
    try:
        from projects import resolve_project as _resolve
        name = _resolve(caller_cwd)
        if name and name != "global":
            return name
    except Exception:
        pass
    return caller


# ─── process control ─────────────────────────────────────────────────────────

def _pdeathsig():
    """Child pre-exec: get SIGTERM if wrap dies (Linux; a no-op elsewhere)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's process group, then SIGKILL after a grace period."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_claude(plan: dict, timeout: float | None, out=None, err=None) -> tuple[int, str, bool, float]:
    """Run the planned claude call. Returns (rc, stdout_text, timed_out, wall_s).

    stream-json lines are forwarded to `out` as they arrive; other formats are
    collected and returned for the caller to print.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    os.makedirs(plan["cwd"], exist_ok=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(plan["argv"], cwd=plan["cwd"], env=plan["env"],
                                stdout=subprocess.PIPE, text=True, bufsize=1,
                                start_new_session=True, preexec_fn=_pdeathsig)
    except FileNotFoundError:
        print(f"[clanker wrap] claude not found: {plan['argv'][0]} "
              "(set CLANKER_WRAP_CLAUDE or put claude on PATH)", file=err)
        return NOT_FOUND_RC, "", False, 0.0

    timed_out = threading.Event()
    interrupted: list[int] = []

    def on_timeout():
        timed_out.set()
        _kill_group(proc)

    timer = threading.Timer(timeout, on_timeout) if timeout and timeout > 0 else None
    old_handlers = {}

    def forward(signum, _frame):
        # The caller stopped wrap: stop claude too (its own session would
        # otherwise outlive us), then let the read loop see EOF.
        interrupted.append(signum)
        _kill_group(proc)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            old_handlers[s] = signal.signal(s, forward)
        except (ValueError, OSError):
            pass
    chunks: list[str] = []
    try:
        if timer:
            timer.daemon = True
            timer.start()
        for line in proc.stdout:
            chunks.append(line)
            if plan["output_format"] == "stream-json":
                out.write(line)
                out.flush()
        rc = proc.wait()
    finally:
        if timer:
            timer.cancel()
        for s, h in old_handlers.items():
            try:
                signal.signal(s, h)
            except (ValueError, OSError):
                pass
    wall = time.monotonic() - t0
    if timed_out.is_set():
        return TIMEOUT_RC, "".join(chunks), True, wall
    if interrupted:
        print(f"[clanker wrap] stopped by signal {interrupted[0]}; claude was killed", file=err)
        return 128 + interrupted[0], "".join(chunks), False, wall
    return rc, "".join(chunks), False, wall


# ─── entry points ────────────────────────────────────────────────────────────

def run_wrap(*, tokens: list[str], model=None, effort=None, max_cost=None, max_turns=None,
             caller=None, cwd=None, timeout=None, allow_user_layer=False, project=None,
             dry_run=False, quiet=False, out=None, err=None, base_env=None) -> int:
    """Run one wrapped claude call end to end; returns the exit code."""
    out = out or sys.stdout
    err = err or sys.stderr
    base_env = dict(os.environ if base_env is None else base_env)
    caller_cwd = os.getcwd()
    caller = caller or os.path.basename(caller_cwd) or "adhoc"
    try:
        plan = build_plan(tokens=tokens, model=model, effort=effort, max_cost=max_cost,
                          max_turns=max_turns, caller=caller, cwd=cwd,
                          allow_user_layer=allow_user_layer, base_env=base_env)
    except UsageError as e:
        print(f"clanker wrap: {e}", file=err)
        return USAGE_RC
    if dry_run:
        added = {k: plan["env"][k] for k in ("CLANKER_WRAP", "CLANKER_WRAP_CALLER", *POLICY_ENV)}
        print(json.dumps({"argv": plan["argv"], "cwd": plan["cwd"], "env_added": added,
                          "env_removed": plan["env_removed"],
                          "output_format": plan["output_format"],
                          "claude_found": plan["claude_found"]}, indent=1), file=out)
        return 0
    if not plan["claude_found"]:
        print("[clanker wrap] claude not found on PATH or in the usual install dirs "
              "(set CLANKER_WRAP_CLAUDE)", file=err)
        return NOT_FOUND_RC

    rc, stdout, timed_out, wall = run_claude(plan, timeout, out=out, err=err)
    result = parse_result(stdout, plan["output_format"])
    fmt = plan["output_format"]
    if fmt == "json":
        out.write(stdout)
    elif fmt == "text":
        if result is not None and "result" in result:
            text = result.get("result")
            out.write(("" if text is None else str(text)).rstrip("\n") + "\n")
        elif stdout:
            out.write(stdout)
    out.flush()
    if rc == 0 and result is not None and result.get("is_error"):
        rc = 1
    if timed_out:
        print(f"[clanker wrap] timeout: killed claude after {timeout:g}s (exit {TIMEOUT_RC})",
              file=err)
    cost = (result or {}).get("total_cost_usd")
    if cost is not None and plan["max_cost_usd"] > 0 and float(cost) > plan["max_cost_usd"]:
        print(f"[clanker wrap] WARNING: cost ${float(cost):.4f} is over --max-cost "
              f"${plan['max_cost_usd']:.2f}", file=err)
    row = build_row(plan, result, rc=rc, timed_out=timed_out, wall_s=wall,
                    project=resolve_project(project, caller_cwd, plan["caller"]),
                    caller_cwd=caller_cwd)
    try:
        append_row(row)
    except OSError as e:
        print(f"[clanker wrap] telemetry row not written: {e}", file=err)
    if not quiet:
        cost_s = f"${float(cost):.4f}" if cost is not None else "n/a"
        print(f"[clanker wrap] caller={plan['caller']} model={row['model']} "
              f"effort={plan['effort']} rc={rc} turns={row['num_turns']} cost={cost_s} "
              f"wall={wall:.1f}s", file=err)
    return rc


def run_generic(command: list[str], project: str | None = None, err=None) -> int:
    """A non-claude command under session tracking (the original `wrap`).

    Ingests session_start, runs the command, ingests session_end with the
    git diff stats and outcome. Returns the command's exit code.
    """
    import re as _re
    err = err or sys.stderr
    project = project or os.path.basename(os.getcwd())
    agent = "generic"

    def git(*a):
        return subprocess.check_output(["git", *a], stderr=subprocess.DEVNULL, text=True).strip()

    before = None
    try:
        before = git("log", "--oneline", "-1")
    except Exception:
        pass
    from ingest import ingest_from_args
    ok, sid, _ = ingest_from_args("session_start", agent, project, cwd=os.getcwd())
    if ok:
        print(f"[clanker] Session {sid[:8]} started", file=err)
    start = time.time()
    try:
        exit_code = subprocess.run(command, shell=False).returncode
    except KeyboardInterrupt:
        exit_code = 130
    except FileNotFoundError:
        print(f"[clanker] command not found: {command[0]}", file=err)
        exit_code = NOT_FOUND_RC
    except Exception as e:
        print(f"[clanker] Command failed: {e}", file=err)
        exit_code = 1
    duration = int(time.time() - start)

    diff = {}
    try:
        stat = git("diff", "--shortstat", "HEAD")
        if stat:
            def n(pat):
                m = _re.search(pat, stat)
                return int(m.group(1)) if m else 0
            diff = {"insertions": n(r"(\d+) insertion"), "deletions": n(r"(\d+) deletion"),
                    "files": n(r"(\d+) file")}
    except Exception:
        pass
    outcome = "unknown"
    try:
        if before and git("log", "--oneline", "-1") != before:
            outcome = "commit"
    except Exception:
        pass
    if exit_code != 0:
        outcome = "abandoned"
    ok, _, _ = ingest_from_args("session_end", agent, project, cwd=os.getcwd(), session_id=sid,
                                duration=duration, outcome=outcome, exit_code=exit_code,
                                git_diff_stats=json.dumps(diff) if diff else None)
    if ok:
        print(f"[clanker] Session {sid[:8]} ended ({duration}s, exit={exit_code})", file=err)
    return exit_code


def main_from_args(args) -> int:
    """`clanker wrap` dispatch from the argparse namespace."""
    tokens = list(args.command or [])
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    claude_flags = any(getattr(args, a, None) not in (None, False) for a in
                       ("model", "effort", "max_cost", "max_turns", "caller",
                        "allow_user_layer"))
    if not is_claude_command(tokens, claude_flags):
        extra = [f for f, a in (("--timeout", "timeout"), ("--cwd", "cwd"),
                                ("--dry-run", "dry_run")) if getattr(args, a, None)]
        if extra:
            print(f"clanker wrap: {', '.join(extra)} apply to claude calls only "
                  f"({tokens[0]!r} is not claude)", file=sys.stderr)
            return USAGE_RC
        return run_generic(tokens, project=args.project)
    return run_wrap(tokens=tokens, model=args.model, effort=args.effort,
                    max_cost=args.max_cost, max_turns=args.max_turns, caller=args.caller,
                    cwd=args.cwd, timeout=args.timeout,
                    allow_user_layer=args.allow_user_layer, project=args.project,
                    dry_run=args.dry_run, quiet=args.quiet)
