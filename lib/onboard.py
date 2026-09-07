"""Project onboarding — clanker init, archetype detection, full scaffolding."""

import os
import json
import yaml
import subprocess

REGISTRY_PATH = os.environ.get("CLANKER_REGISTRY", os.path.expanduser("~/projects/.clanker.yaml"))

# Archetype → build/test/lint commands
ARCHETYPE_COMMANDS = {
    "production": {
        "test": "pytest tests/ -v",
        "lint": "ruff check .",
        "build": "docker compose build",
        "deploy": "See CLAUDE.md for deployment instructions",
    },
    "research": {
        "test": "pytest tests/ -v",
        "lint": "ruff check .",
        "build": None,
        "deploy": None,
    },
    "frontend": {
        "test": "npm test",
        "lint": "npm run lint",
        "build": "npm run build",
        "deploy": None,
    },
    "tool": {
        "test": "pytest tests/ -v",
        "lint": "ruff check .",
        "build": None,
        "deploy": None,
    },
    "infra": {
        "test": "terraform plan",
        "lint": "terraform validate",
        "build": "terraform apply",
        "deploy": None,
    },
}


def detect_archetype(project_path):
    """Scan a project directory and suggest an archetype."""
    indicators = {
        "production": 0,
        "research": 0,
        "frontend": 0,
        "tool": 0,
        "infra": 0,
    }

    files = os.listdir(project_path) if os.path.isdir(project_path) else []
    all_files_str = " ".join(files).lower()

    # Production indicators
    if "docker-compose.yml" in files or "docker-compose.yaml" in files:
        indicators["production"] += 3
    if any(f.startswith("Dockerfile") for f in files):
        indicators["production"] += 2
    if os.path.isdir(os.path.join(project_path, "services")):
        indicators["production"] += 3

    # Research indicators
    if any(f.endswith(".parquet") for f in files):
        indicators["research"] += 3
    if "research" in all_files_str or "notebooks" in all_files_str:
        indicators["research"] += 2
    if os.path.isdir(os.path.join(project_path, "data")):
        indicators["research"] += 1

    # Frontend indicators
    if "package.json" in files:
        indicators["frontend"] += 2
        try:
            with open(os.path.join(project_path, "package.json")) as f:
                pkg = json.load(f)
            deps = str(pkg.get("dependencies", {})) + str(pkg.get("devDependencies", {}))
            if "react" in deps or "next" in deps or "vue" in deps or "svelte" in deps:
                indicators["frontend"] += 3
        except:
            pass
    if "tsconfig.json" in files:
        indicators["frontend"] += 1

    # Tool indicators
    if "pyproject.toml" in files or "setup.py" in files:
        indicators["tool"] += 2
    if "Cargo.toml" in files:
        indicators["tool"] += 2
    if "Makefile" in files:
        indicators["tool"] += 1

    # Infra indicators
    if any(f.endswith(".tf") for f in files):
        indicators["infra"] += 3
    if "ansible" in all_files_str or "terraform" in all_files_str:
        indicators["infra"] += 2
    if os.path.isdir(os.path.join(project_path, "infra")):
        indicators["infra"] += 2

    best = max(indicators, key=indicators.get)
    if indicators[best] == 0:
        return "tool", indicators
    return best, indicators


def _detect_tech_stack(project_path):
    """Detect languages, frameworks, and tools used."""
    stack = {"languages": [], "frameworks": [], "tools": []}
    files = os.listdir(project_path) if os.path.isdir(project_path) else []

    # Languages
    if "package.json" in files or "tsconfig.json" in files:
        stack["languages"].append("TypeScript/JavaScript")
    if "pyproject.toml" in files or "setup.py" in files or any(f.endswith(".py") for f in files):
        stack["languages"].append("Python")
    if "Cargo.toml" in files:
        stack["languages"].append("Rust")
    if "go.mod" in files:
        stack["languages"].append("Go")

    # Frameworks
    if "package.json" in files:
        try:
            with open(os.path.join(project_path, "package.json")) as f:
                pkg = json.load(f)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            for fw in ["react", "next", "vue", "svelte", "express", "fastify", "vite"]:
                if fw in deps:
                    stack["frameworks"].append(fw)
        except:
            pass
    if "pyproject.toml" in files:
        try:
            with open(os.path.join(project_path, "pyproject.toml")) as f:
                content = f.read()
            for fw in ["fastapi", "flask", "django", "pytorch", "tensorflow", "pandas"]:
                if fw in content.lower():
                    stack["frameworks"].append(fw)
        except:
            pass

    # Tools
    if any(f.startswith("Dockerfile") for f in files):
        stack["tools"].append("Docker")
    if "docker-compose.yml" in files or "docker-compose.yaml" in files:
        stack["tools"].append("Docker Compose")
    if any(f.endswith(".tf") for f in files):
        stack["tools"].append("Terraform")
    if ".github" in files:
        stack["tools"].append("GitHub Actions")
    if "Makefile" in files:
        stack["tools"].append("Make")
    if "pnpm-lock.yaml" in files:
        stack["tools"].append("pnpm")
    elif "package-lock.json" in files:
        stack["tools"].append("npm")

    return stack


def _detect_commands(project_path, archetype):
    """Detect actual test/build/lint commands from project files."""
    commands = dict(ARCHETYPE_COMMANDS.get(archetype, {}))

    files = os.listdir(project_path) if os.path.isdir(project_path) else []

    # Check package.json scripts
    if "package.json" in files:
        try:
            with open(os.path.join(project_path, "package.json")) as f:
                pkg = json.load(f)
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                pm = "pnpm" if "pnpm-lock.yaml" in files else "npm"
                commands["test"] = f"{pm} test"
            if "lint" in scripts:
                pm = "pnpm" if "pnpm-lock.yaml" in files else "npm"
                commands["lint"] = f"{pm} run lint"
            if "build" in scripts:
                pm = "pnpm" if "pnpm-lock.yaml" in files else "npm"
                commands["build"] = f"{pm} run build"
            if "dev" in scripts:
                pm = "pnpm" if "pnpm-lock.yaml" in files else "npm"
                commands["dev"] = f"{pm} run dev"
        except:
            pass

    # Check for pytest
    if os.path.isdir(os.path.join(project_path, "tests")):
        commands["test"] = "pytest tests/ -v"
    elif os.path.isdir(os.path.join(project_path, "test")):
        commands["test"] = "pytest test/ -v"

    # Check Makefile targets
    if "Makefile" in files:
        try:
            with open(os.path.join(project_path, "Makefile")) as f:
                makefile = f.read()
            for target in ["test", "lint", "build", "dev"]:
                if f"\n{target}:" in makefile or f"\n.PHONY: {target}" in makefile:
                    commands[target] = f"make {target}"
        except:
            pass

    # Check Cargo
    if "Cargo.toml" in files:
        commands["test"] = "cargo test"
        commands["build"] = "cargo build"
        commands["lint"] = "cargo clippy"

    return {k: v for k, v in commands.items() if v}


def _read_project_description(project_path):
    """Read README to get project description."""
    for readme in ["README.md", "readme.md", "README", "README.rst"]:
        path = os.path.join(project_path, readme)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    content = f.read()
                # Extract first paragraph (skip title)
                lines = content.split("\n")
                desc_lines = []
                past_title = False
                for line in lines:
                    if line.startswith("#"):
                        past_title = True
                        continue
                    if past_title and line.strip():
                        desc_lines.append(line.strip())
                        if len(desc_lines) >= 3:
                            break
                    elif past_title and not line.strip() and desc_lines:
                        break
                return " ".join(desc_lines)[:300] if desc_lines else None
            except:
                pass
    return None


def init_project(name, archetype=None, project_path=None):
    """Full project onboarding: detect, scaffold, register."""
    if not project_path:
        project_path = os.path.expanduser(f"~/projects/{name}")

    if not os.path.isdir(project_path):
        print(f"Project directory not found: {project_path}")
        return False

    # Auto-detect archetype
    if not archetype:
        archetype, scores = detect_archetype(project_path)
        print(f"Detected archetype: {archetype} (scores: {scores})")

    # Detect tech stack and commands
    stack = _detect_tech_stack(project_path)
    commands = _detect_commands(project_path, archetype)
    description = _read_project_description(project_path)

    print(f"Tech stack: {stack}")
    print(f"Commands: {commands}")

    # 1. Create .claude/clanker.local.md
    claude_dir = os.path.join(project_path, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    local_md = os.path.join(claude_dir, "clanker.local.md")
    if not os.path.exists(local_md):
        with open(local_md, "w") as f:
            f.write(f"""---
archetype: {archetype}
hooks_add: []
hooks_remove: []
---

# {name} — Clanker Config

{f"Description: {description}" if description else ""}
Languages: {", ".join(stack["languages"]) or "unknown"}
Frameworks: {", ".join(stack["frameworks"]) or "none detected"}
Tools: {", ".join(stack["tools"]) or "none detected"}
""")
        print(f"Created: {local_md}")

    # 2. Create project CLAUDE.md if it doesn't exist
    project_claude = os.path.join(project_path, "CLAUDE.md")
    if not os.path.exists(project_claude):
        lines = [f"# {name}\n\n"]
        if description:
            lines.append(f"{description}\n\n")
        lines.append(f"**Archetype:** {archetype}\n")
        if stack["languages"]:
            lines.append(f"**Languages:** {', '.join(stack['languages'])}\n")
        if stack["frameworks"]:
            lines.append(f"**Frameworks:** {', '.join(stack['frameworks'])}\n")
        lines.append("\n## Commands\n\n")
        for cmd_name, cmd_val in commands.items():
            lines.append(f"- **{cmd_name}:** `{cmd_val}`\n")
        lines.append("\n## Project Rules\n\n")
        lines.append("- Follow existing code style and conventions\n")
        lines.append("- Run tests before committing\n")
        lines.append("- Keep commits focused and atomic\n")

        with open(project_claude, "w") as f:
            f.writelines(lines)
        print(f"Created: {project_claude}")

    # 3. Create per-project settings.json with archetype hooks
    from memoryns import slug as _ns_slug
    settings_dir = os.path.join(
        os.path.expanduser("~/.claude/projects"),
        _ns_slug(os.path.expanduser(f"~/projects/{name}")),
    )
    os.makedirs(settings_dir, exist_ok=True)
    settings_path = os.path.join(settings_dir, "settings.json")
    if not os.path.exists(settings_path):
        # Load archetype hooks from registry
        with open(REGISTRY_PATH) as f:
            reg = yaml.safe_load(f)
        arch_hooks = reg.get("archetypes", {}).get(archetype, {}).get("hooks", [])

        if arch_hooks:
            hook_entries = []
            for hook_name in arch_hooks:
                hook_path = os.path.expanduser(f"~/.claude/hooks/{hook_name}.sh")
                if os.path.exists(hook_path):
                    hook_entries.append({
                        "type": "command",
                        "command": f"bash {hook_path}",
                        "timeout": 5000,
                    })

            if hook_entries:
                settings = {
                    "hooks": {
                        "PreToolUse": [{
                            "matcher": "Bash",
                            "hooks": hook_entries,
                        }]
                    }
                }
                with open(settings_path, "w") as f:
                    json.dump(settings, f, indent=4)
                print(f"Created: {settings_path} ({len(hook_entries)} hooks)")
    else:
        print(f"Exists: {settings_path}")

    # 4. Set up .gitignore for clanker files
    gitignore = os.path.join(project_path, ".gitignore")
    clanker_ignore = ".claude/clanker.local.md"
    if os.path.exists(gitignore):
        with open(gitignore) as f:
            content = f.read()
        if clanker_ignore not in content:
            with open(gitignore, "a") as f:
                f.write(f"\n# Clanker local config\n{clanker_ignore}\n")
            print(f"Updated .gitignore")
    else:
        with open(gitignore, "w") as f:
            f.write(f"# Clanker local config\n{clanker_ignore}\n")
        print(f"Created .gitignore")

    # 5. Add to registry — via the single mutator (registry.write_entry)
    from registry import write_entry
    remote = None
    try:
        r = subprocess.check_output(
            ["git", "-C", project_path, "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
        if "github.com" in r:
            parts = r.replace("https://github.com/", "").replace(".git", "").split("/")
            if len(parts) >= 2:
                remote = f"{parts[0]}/{parts[1]}"
    except Exception:
        pass
    if write_entry(name, {"archetype": archetype, "remote": remote}, create_only=True):
        print(f"Added to registry: {name} ({archetype})")
    else:
        print(f"Already in registry: {name}")

    # 6. Install dependencies if needed
    if "package.json" in os.listdir(project_path):
        node_modules = os.path.join(project_path, "node_modules")
        if not os.path.isdir(node_modules):
            print(f"NOTE: Run `cd ~/projects/{name} && npm install` to install dependencies")

    if "pyproject.toml" in os.listdir(project_path) or "requirements.txt" in os.listdir(project_path):
        print(f"NOTE: Check Python dependencies for ~/projects/{name}")

    print(f"\nOnboarding complete: ~/projects/{name} ({archetype})")
    return True


def remove_project(name, force_schedules=False):
    """Remove a project from clanker (registry, settings, tmux). Does NOT delete the repo.

    DECOMMISSION GATE (2026-08-12): refuses while the project still owns ACTIVE
    scheduled work on this machine. Before this gate, removal cleaned clanker's
    bookkeeping and left the box running the project's cron forever — the yon
    repo's nightly CI burned 16 cores every 06:00 for 48 days after its last
    commit because retiring it never looked at /etc/cron.d. Override with
    force_schedules=True once each item is dispositioned.
    """
    # Resolve the real path from the registry; ~/projects/<name> is only the
    # default layout (yon lived at ~/yon, which is how its cron went unnoticed).
    from registry import Registry
    try:
        project_path = Registry().get_path(name) or os.path.expanduser(f"~/projects/{name}")
    except Exception:
        project_path = os.path.expanduser(f"~/projects/{name}")
    project_path = os.path.normpath(project_path)
    removed = []

    # 0. GATE: scheduled work must be dispositioned first.
    if not force_schedules:
        try:
            import schedules
            owned = [i for i in schedules.for_project(schedules.scan(), project_path)
                     if i["active"]]
            scan_ok = True
        except Exception as e:                      # noqa: BLE001
            owned, scan_ok = [], False
            scan_err = e
        if not scan_ok:
            print(f"remove: could not inventory scheduled work ({scan_err}). "
                  f"This gate fails CLOSED — re-run with --force-schedules once "
                  f"you have checked /etc/cron.d, `crontab -l` and systemd timers "
                  f"for anything referencing {project_path}.")
            return False
        if owned:
            cmds, needs_sudo = schedules.plan(owned, reason=f"removed-{name}")
            print(f"remove: '{name}' still owns {len(owned)} ACTIVE scheduled item(s) "
                  f"referencing {project_path}. Retiring it now would leave them "
                  f"running forever (this is exactly what happened to yon).\n")
            for i in owned:
                where = i["path"] or i["unit"]
                print(f"  [{i['source']}] {where}:{i['line_no']}  {i['line'][:96]}")
            print("\nDisable them first (nothing is deleted — files are renamed, "
                  "lines commented):")
            for c in cmds:
                print(f"  {c}")
            if needs_sudo:
                print("\n(root-owned; clanker never runs sudo for you)")
            print(f"\nThen re-run:  clanker remove {name} --force-schedules")
            return False

    # 1. Remove from registry — via the single mutator (registry.remove_entry)
    from registry import remove_entry
    if remove_entry(name):
        removed.append("registry")

    # 2. Remove per-project settings.json
    from memoryns import slug as _ns_slug
    settings_dir = os.path.join(
        os.path.expanduser("~/.claude/projects"),
        _ns_slug(os.path.expanduser(f"~/projects/{name}")),
    )
    settings_path = os.path.join(settings_dir, "settings.json")
    if os.path.exists(settings_path):
        os.remove(settings_path)
        removed.append("per-project settings.json")

    # 3. Remove clanker local config
    local_md = os.path.join(project_path, ".claude", "clanker.local.md")
    if os.path.exists(local_md):
        os.remove(local_md)
        removed.append(".claude/clanker.local.md")

    # 4. Remove tmux session
    from tmux_manager import remove_session
    remove_session(name)
    removed.append("tmux session")

    # 5. Remove wiki article
    wiki_article = os.path.join(
        os.environ.get("CLANKER_DATA", "/data/clanker"),
        "wiki", "projects", f"{name}.md"
    )
    if os.path.exists(wiki_article):
        os.remove(wiki_article)
        removed.append("wiki article")

    handoff = wiki_article.replace(".md", "-handoff.md")
    if os.path.exists(handoff):
        os.remove(handoff)
        removed.append("handoff")

    if removed:
        print(f"Removed: {', '.join(removed)}")
    else:
        print(f"Nothing to remove for '{name}'")

    print(f"NOTE: The repo at ~/projects/{name} was NOT deleted. Remove manually if desired.")
    return True
