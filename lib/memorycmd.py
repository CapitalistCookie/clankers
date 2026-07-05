"""`clanker memory` — mechanical memory-file operations so weak models never
hand-roll the format (memory hardening, 2026-07-05).

  clanker memory new <slug> --type T --desc "..." [--project NAME] [--body "..."]
      Scaffold a lint-clean topic file + append the index pointer line, then
      run memory-lint on both. Global router dir by default; --project targets
      that project's namespace memory.
  clanker memory doctor [--project NAME]
      Full lint sweep of a memory dir (memory-lint --doctor) + index regen.

The lint script is the single source of truth for validity — this command
never bypasses it, it just makes compliance the path of least resistance.
"""
import os
import subprocess
import sys

LINT = "/home/user/.claude/hooks/memory-lint.sh"
GLOBAL_MEM = os.path.expanduser("~/.claude/projects/-home-user/memory")
VALID_TYPES = ("user", "feedback", "project", "reference")


def _mem_dir(project=None):
    if not project:
        return GLOBAL_MEM
    from registry import Registry
    from memoryns import ns_dir
    path = Registry().get_path(project)
    if not path or not os.path.isdir(path):
        print(f"memory: unknown project '{project}'", file=sys.stderr)
        return None
    d = os.path.join(ns_dir(path), "memory")
    os.makedirs(d, exist_ok=True)
    return d


def new(slug, mtype, desc, project=None, body=""):
    if mtype not in VALID_TYPES:
        print(f"memory new: --type must be one of {VALID_TYPES}", file=sys.stderr)
        return 1
    if not desc or not desc.strip():
        print("memory new: --desc is required (recall routes on it)", file=sys.stderr)
        return 1
    slug = slug.strip().removesuffix(".md")
    mem = _mem_dir(project)
    if not mem:
        return 1
    path = os.path.join(mem, f"{slug}.md")
    if os.path.exists(path):
        print(f"memory new: {path} already exists — edit it instead (lint runs on edit)",
              file=sys.stderr)
        return 1
    with open(path, "w") as f:
        f.write(f"---\nname: {slug}\ndescription: {desc.strip()}\nmetadata:\n"
                f"  type: {mtype}\n---\n\n{body.strip() or '<fill in the fact>'}\n")
    r = subprocess.run(["bash", LINT, "--doctor", mem], capture_output=True, text=True)
    index = os.path.join(mem, "MEMORY.md")
    line = f"- [{slug}]({slug}.md) — {desc.strip()[:180]}\n"
    if os.path.exists(index) and slug not in open(index, errors="ignore").read():
        with open(index, "a") as f:
            f.write(line)
    print(f"created {path}\nindexed in {index}")
    if r.returncode != 0:
        print(r.stderr.strip()[:500], file=sys.stderr)
        print("memory new: dir has OTHER lint violations (above) — the new file itself is clean",
              file=sys.stderr)
    return 0


def doctor(project=None):
    mem = _mem_dir(project)
    if not mem:
        return 1
    r = subprocess.run(["bash", LINT, "--doctor", mem])
    return r.returncode
