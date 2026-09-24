#!/usr/bin/env python3
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
"""GPU guard hook — health-checks the shared GPU before kicking off remote work.

Post-containerization reality (see memory/gpu-usage.md):
- GPU is on CT 200 (host from GPU_HOST in ~/.claude/harness.env), shared with Frigate via MPS.
- This CT (112) has no direct GPU access — training runs remotely via
  the `gpu-train` wrapper which SSHes to CT 200.
- MPS enforces per-client share via CUDA_MPS_ACTIVE_THREAD_PERCENTAGE;
  do NOT stop Frigate to make room — use smaller GPU_SHARE instead.

Fires as PreToolUse on Bash when the command targets the GPU host.
An internal failure fails open and lands in the hook-error log.
"""
# ---- hook-error log: the same block in every clanker python hook ----------------
# A failure that the hook swallows (the hook stays fail-open for the session)
# appends one JSON line {ts, hook, session_id, cwd, rc, stderr_tail} to
# $CLANKER_DATA/raw/health/hook-errors-<UTC day>.jsonl (default /data/clanker),
# where `clanker doctor --harness` counts it. hook_err never raises, never
# blocks and never writes to stdout. Usage: hook_err(rc, step, error text or
# exception); stderr_tail is "<step>: " plus the end of the text (of the
# traceback, for an exception), 300 characters at most. Set
# HOOK_ERR["session_id"] and HOOK_ERR["cwd"] once the payload is parsed; a
# script read from stdin has no __file__ and sets HOOK_ERR["hook"] as well.
import os as _he_os
import sys as _he_sys

HOOK_ERR = {"hook": _he_os.path.basename(globals().get("__file__") or "") or "?",
            "session_id": "", "cwd": ""}


def hook_err(rc, step, err=""):
    try:
        import json
        import time
        import traceback
        if isinstance(err, BaseException):
            err = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        step, err = str(step), str(err or "").strip()
        room = 298 - len(step)
        msg = (step + ": " + err[-room:]) if err and room > 0 else step
        try:
            cwd = HOOK_ERR.get("cwd") or _he_os.getcwd()
        except OSError:
            cwd = ""
        rc = int(rc) if str(rc).lstrip("-").isdigit() else 1
        now = time.gmtime()
        d = _he_os.path.join(_he_os.environ.get("CLANKER_DATA") or "/data/clanker",
                             "raw", "health")
        _he_os.makedirs(d, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", now),
               "hook": str(HOOK_ERR.get("hook") or "?"),
               "session_id": str(HOOK_ERR.get("session_id") or ""), "cwd": str(cwd),
               "rc": rc, "stderr_tail": msg[:300]}
        path = _he_os.path.join(d, "hook-errors-" + time.strftime("%Y-%m-%d", now) + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass
# ---- end of hook-error log --------------------------------------------------------

import json
import os
import re
import subprocess
import sys

# ── Config from the environment ────────────────────────────
# GPU_HOST / GPU_USER come from ~/.claude/harness.env, which the Bash dispatcher
# sources and exports to this child. research.env is deliberately NOT read
# (2026-09-24): it also carries third-party API keys that must not enter the
# gate process. GPU_SSH_KEY defaults to the key research.env used to name.
GPU_HOST = os.environ.get("GPU_HOST", "")  # de-personalized: no baked-in LAN IP
GPU_USER = os.environ.get("GPU_USER", "root")
GPU_SSH_KEY = os.environ.get("GPU_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519"))

SSH_BASE = [
    "ssh", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10",
    "-o", "StrictHostKeyChecking=no", "-o", "LogLevel=ERROR",
    "-i", GPU_SSH_KEY, f"{GPU_USER}@{GPU_HOST}",
]

# Packages that may need installing on CT 200 before training
OPTIONAL_PACKAGES = {
    "numba", "sympy", "pytz", "scipy", "sklearn", "joblib",
    "networkx", "pyarrow", "fastparquet", "databento", "requests",
    "torch", "xgboost", "lightgbm",
}

# ---------------------------------------------------------------------------

def block(reason: str) -> None:
    # permissionDecision form — legacy top-level {"decision":"block"} is no
    # longer in the documented PreToolUse contract (2.1.x).
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout)
    sys.exit(0)

def advise(msg: str) -> None:
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": msg,
    }}, sys.stdout)
    sys.exit(0)

def ssh_run(cmd: str, timeout: int = 8) -> str | None:
    try:
        r = subprocess.run(
            SSH_BASE + [cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        hook_err(1, "ssh health check (skipped)", e)
        return None
    if r.returncode != 0:
        hook_err(r.returncode, "ssh health check (skipped)", r.stderr)
        return None
    return r.stdout.strip()

def get_script_imports(path: str) -> set[str]:
    pkgs = set()
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("import "):
                    for mod in line[7:].split(","):
                        pkgs.add(mod.strip().split(".")[0])
                elif line.startswith("from ") and " import " in line:
                    pkgs.add(line.split()[1].split(".")[0])
    except OSError:
        pass
    return pkgs

# ---------------------------------------------------------------------------

def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception as e:
        hook_err(1, "parse hook input", e)
        return  # malformed/missing stdin: never crash the session
    if not isinstance(hook_input, dict):
        hook_err(1, "parse hook input", "payload is not a JSON object")
        return
    HOOK_ERR.update(session_id=str(hook_input.get("session_id") or ""), cwd=str(hook_input.get("cwd") or ""))
    tool_input = hook_input.get("tool_input") or {}
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(cmd, str) or not cmd:
        return

    # Activate only on commands that target the GPU host. When GPU_HOST is unset
    # (generic copy, no harness.env), the host-match is skipped so the guard is
    # inert except for the `gpu-train` wrapper path — never fires on every command.
    if (not GPU_HOST or GPU_HOST not in cmd) and "gpu-train" not in cmd:
        return

    # ── 1. Block nohup/setsid on remote ────────────────────────
    if re.search(r"\b(nohup|setsid)\b", cmd):
        block(
            "BLOCKED: nohup/setsid dies on CT 200 "
            "(systemd-logind KillUserProcesses=yes). Use inline SSH:\n\n"
            f"  ssh -o ServerAliveInterval=10 -o ServerAliveCountMax=60 "
            f"-i {GPU_SSH_KEY} {GPU_USER}@{GPU_HOST} "
            "'python3 -u /data/path/script.py'\n\n"
            "Or preferably: gpu-train python /data/path/script.py\n"
            "Set Bash timeout to 600000ms."
        )

    # ── 2. Block scp to /data/ ─────────────────────────────────
    if re.search(r"\bscp\b.*" + re.escape(GPU_HOST) + r".*(:/data|/data/)", cmd):
        block(
            "BLOCKED: /data/ is bind-mounted on both sides. "
            "No scp needed. Reference /data/ paths directly."
        )

    # ── 3. Block advice that used to apply (pre-migration) ─────
    if re.search(r"docker\s+stop\s+frigate", cmd):
        block(
            "BLOCKED: Don't stop Frigate to free GPU — MPS handles sharing.\n"
            "Use `GPU_SHARE=N gpu-train ...` to limit your training's SM share.\n"
            "Default 30% leaves Frigate plenty of room."
        )

    # ── 4. Python script run → health check via SSH ────────────
    script_match = re.search(r"(/\S+\.py)", cmd)
    if script_match:
        script_path = script_match.group(1)
        imports = get_script_imports(script_path)
        to_check = sorted(imports & OPTIONAL_PACKAGES)

        check_script = (
            "import importlib, subprocess, json, os\n"
            "pkgs = %r\n"
            "missing = [p for p in pkgs if not importlib.util.find_spec(p)]\n"
            "disk_avail_kb = int(subprocess.check_output("
            "['df', '/', '--output=avail']).split()[-1])\n"
            "try:\n"
            "    vram_mb = int(subprocess.check_output("
            "['nvidia-smi', '--query-gpu=memory.free', "
            "'--format=csv,noheader,nounits']).strip())\n"
            "except Exception:\n"
            "    vram_mb = 0\n"
            "mps = os.path.exists('/tmp/nvidia-mps/control')\n"
            "json.dump({'missing': missing, 'disk_kb': disk_avail_kb, "
            "'vram_mb': vram_mb, 'mps': mps}, open('/dev/stdout', 'w'))\n"
        ) % to_check

        result = ssh_run(f"python3 -c {repr(check_script)}")
        if result:
            try:
                data = json.loads(result)
            except json.JSONDecodeError as e:
                hook_err(1, "ssh health check reply", e)
                data = None

            if data:
                # Missing deps → block, point at CT 200 install path
                if data.get("missing"):
                    pkgs = " ".join(data["missing"])
                    block(
                        f"BLOCKED: CT 200 missing packages: {pkgs}. Install:\n"
                        f"  ssh -i {GPU_SSH_KEY} {GPU_USER}@{GPU_HOST} "
                        f"'pip install {pkgs} --break-system-packages'"
                    )

                problems = []
                # Disk: CT 200 rootfs may fill if pip cache / logs grow
                # (.get defaults: a malformed remote reply must not crash the hook)
                if data.get("disk_kb", 1 << 30) < 2_097_152:
                    problems.append(
                        f"Disk: {data.get('disk_kb', 0) // 1_048_576}GB free (<2GB)"
                    )
                # VRAM: MPS still needs real space per client
                if data.get("vram_mb", 1 << 20) < 1024:
                    problems.append(
                        f"VRAM: {data.get('vram_mb', 0)}MB (<1GB). "
                        "Cap your run: GPU_MEM=1024 gpu-train ..."
                    )
                # MPS daemon must be running
                if not data.get("mps", True):
                    problems.append(
                        "MPS daemon not running on CT 200. "
                        f"ssh {GPU_USER}@{GPU_HOST} systemctl start nvidia-cuda-mps"
                    )

                if problems:
                    block(
                        "BLOCKED: CT 200 GPU endpoint unhealthy. "
                        + ". ".join(problems)
                        + ". Fix before running."
                    )

    # ── 5. Advisory ────────────────────────────────────────────
    if re.search(r"python3?\s", cmd) or "gpu-train" in cmd:
        advise(
            "GPU on CT 200 via MPS. Scripts under /data are visible both sides "
            "(bind mount). Prefer `gpu-train python /data/.../script.py` — "
            "wraps SSH + MPS share + nice/ionice. "
            "Default GPU_SHARE=30 (Frigate keeps the rest)."
        )
    elif "scp" in cmd:
        advise("/data is bind-mounted — don't scp, just use /data/ paths.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Internal error → fail OPEN, logged (block()/advise() exit via
        # SystemExit, which is not caught here, so gate decisions still work).
        hook_err(1, "main", e)
