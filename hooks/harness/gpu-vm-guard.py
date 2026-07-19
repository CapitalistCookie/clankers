#!/usr/bin/env python3
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
"""GPU guard hook — health-checks the shared GPU before kicking off remote work.

Post-containerization reality (see memory/gpu-usage.md):
- GPU is on CT 200 (host from research.env GPU_HOST), shared with Frigate via MPS.
- This CT (112) has no direct GPU access — training runs remotely via
  the `gpu-train` wrapper which SSHes to CT 200.
- MPS enforces per-client share via CUDA_MPS_ACTIVE_THREAD_PERCENTAGE;
  do NOT stop Frigate to make room — use smaller GPU_SHARE instead.

Fires as PreToolUse on Bash when the command targets the GPU host.
"""

import json
import os
import re
import subprocess
import sys

# ── Load config from research.env ──────────────────────────
_ENV_PATH = os.path.expanduser("~/.claude/research.env")
_config = {}
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                _config[k.strip()] = v.strip()

GPU_HOST = _config.get("GPU_HOST", "")  # de-personalized: no baked-in LAN IP
GPU_USER = _config.get("GPU_USER", "root")
GPU_SSH_KEY = _config.get("GPU_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519"))

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
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None

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
    except Exception:
        return  # malformed/missing stdin: never crash the session
    if not isinstance(hook_input, dict):
        return
    tool_input = hook_input.get("tool_input") or {}
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(cmd, str) or not cmd:
        return

    # Activate only on commands that target the GPU host. When GPU_HOST is unset
    # (generic copy, no research.env), the host-match is skipped so the guard is
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
            except json.JSONDecodeError:
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
    except Exception:
        # Internal error → fail OPEN silently (block()/advise() exit via
        # SystemExit, which is not caught here, so gate decisions still work).
        pass
