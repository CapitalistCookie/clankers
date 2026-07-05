"""Archetype-aware statusline generator."""

import os
import json
import subprocess
from collections import Counter


def generate_statusline(cwd, model="", remaining=""):
    """Generate a project-aware statusline for Claude Code.

    Reads CLANKER_PROJECT and CLANKER_ARCHETYPE from environment.
    Falls back to detecting from cwd.
    """
    project = os.environ.get("CLANKER_PROJECT", "")
    archetype = os.environ.get("CLANKER_ARCHETYPE", "")

    if not project and cwd:
        if "/home/user/projects/" in cwd:
            project = cwd.split("/home/user/projects/")[-1].split("/")[0]

    # Line 1: identity
    user = os.environ.get("USER", "user")
    hostname = subprocess.check_output(["hostname", "-s"], text=True).strip()
    line1 = f"\033[01;32m{user}@{hostname}\033[00m:\033[01;34m{cwd}\033[00m"
    if model:
        line1 += f" | {model}"
    if remaining:
        line1 += f" | ctx:{remaining}%"

    # Line 2: project-specific
    line2_parts = []

    if cwd and os.path.isdir(os.path.join(cwd, ".git")):
        try:
            branch = subprocess.check_output(
                ["git", "-C", cwd, "branch", "--show-current"],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
            line2_parts.append(f"\033[33m{branch}\033[0m")
        except:
            pass

        try:
            uncommitted = int(subprocess.check_output(
                ["git", "-C", cwd, "status", "--porcelain"],
                stderr=subprocess.DEVNULL, text=True
            ).count('\n'))
            if uncommitted > 0:
                line2_parts.append(f"{uncommitted} uncommitted")
        except:
            pass

    if project:
        line2_parts.append(f"[{project}:{archetype}]")

    # Archetype-specific metrics
    data_dir = os.environ.get("CLANKER_DATA", "/data/clanker")
    alerts_dir = os.path.join(data_dir, "alerts")
    alert_count = 0
    if os.path.isdir(alerts_dir):
        alert_count = len([f for f in os.listdir(alerts_dir) if f.endswith(".json")])
    if alert_count > 0:
        line2_parts.append(f"\033[31m{alert_count} alerts\033[0m")

    line2 = " | ".join(line2_parts)

    # Line 3: GPU status (cached — read from health check, not SSH per-session)
    line3 = ""
    gpu_cache = "/tmp/.claude-gpu-status"
    if os.path.exists(gpu_cache):
        try:
            age = os.time() - os.path.getmtime(gpu_cache)
        except:
            age = 999
        try:
            with open(gpu_cache) as f:
                gpu_status = f.read().strip()
            if gpu_status and gpu_status != "idle" and gpu_status != "offline":
                line3 = f"\033[35mGPU:\033[0m {gpu_status[-80:]}"
            elif gpu_status == "offline":
                line3 = "\033[31mGPU:offline\033[0m"
            else:
                line3 = "\033[32mGPU:idle\033[0m"
        except:
            line3 = "\033[32mGPU:idle\033[0m"
    else:
        line3 = "\033[32mGPU:idle\033[0m"

    return f"{line1}\n{line2}\n{line3}"
