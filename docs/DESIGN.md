# Clanker — Self-Improving Development Harness

**Design Document — 2026-04-07 02:55 UTC (v2 — incorporates 46 issues from 11 sweep rounds)**
**Repo:** `CapitalistCookie/clanker` → `~/projects/clanker`

---

## 1. Vision

Clanker is a Unix-style development harness that sits alongside Claude Code, providing automated session tracking, project management, intelligent alerting, and a self-improving feedback loop. It combines a **deterministic pipeline** (shell scripts, cron, hooks) for real-time signal collection with an **LLM-maintained knowledge base** for synthesis, pattern detection, and institutional memory.

Over time, Clanker learns what works, what fails, and what costs the most — then proposes concrete improvements to the harness itself.

---

## 2. Architecture

### Dual System Design

```
PIPELINE (automated, deterministic)              KNOWLEDGE BASE (LLM-maintained, queryable)
────────────────────────────────────              ──────────────────────────────────────────

SessionEnd hook → session metrics
Health cron → resource/repo/service checks
                    │
                    ▼
        /data/clanker/raw/                ──→  clanker compile ──→ /data/clanker/wiki/
        (JSONL: sessions, alerts,                (weekly LLM job)   (project articles,
         health checks, proposals)               with redaction      pattern catalog,
                    │                            filter for           decision log,
                    ▼                            credentials)        failure taxonomy)
        clanker analyze (scripts)                                         │
        → cost-weighted rankings                                          ▼
        → anomaly detection                                     Q&A in meta session
        → regression detection                                  → "what's costing most?"
        → threshold alerts                                      → "what changed since Monday?"
                    │                                            → "summarize the billing system"
                    ▼                                                      │
        /data/clanker/alerts/                                              ▼
        → surfaced at SessionStart                              Outputs filed back into wiki
```

**Pipeline** = nervous system. Collects signals, detects anomalies, triggers reflexes. Fast, reliable, no LLM in the loop.

**Knowledge base** = brain. Synthesizes understanding, answers complex questions, makes cross-project connections. LLM-maintained markdown wiki that grows richer over time. A redaction filter strips credentials/API keys before the LLM sees raw session data.

### Three Feedback Loops

| Loop | Speed | Trigger | Action |
|------|-------|---------|--------|
| **Fast** | Per-session | SessionEnd hook | Log metrics, fire alerts if critical |
| **Medium** | Weekly | Cron (Sunday 6 AM UTC) | Deep analysis, cost-weighted proposals, regression detection |
| **Slow** | Monthly | Manual review in meta session | Archetype evolution, harness restructuring, knowledge base curation |

### Plugin Architecture

Clanker is a **Claude Code plugin** (not loose scripts in settings.json). It uses the native plugin format:

```
~/projects/clanker/                  # The clanker repo (self-tracked)
  .claude-plugin/
    plugin.json                      # name, version, author, description
  hooks/
    hooks.json                       # Declares SessionStart + SessionEnd hooks
    session-start.sh                 # Alert injection + CLAUDE_ENV_FILE setup
    session-end.sh                   # Metrics extraction (reads stdin JSON)
  skills/
    clanker-review/SKILL.md          # /review-harness skill for proposal review
    clanker-briefing/SKILL.md        # Project briefing skill (Tier 2)
  bin/                               # CLI tools (symlinked to ~/bin/)
    clanker                          # Main CLI entrypoint (Python + argparse)
  lib/                               # Shared Python library
    metrics.py                       # Session metrics extraction
    registry.py                      # Registry reader/querier
    analyze.py                       # Analysis pipeline
    propose.py                       # Proposal generator
    alerts.py                        # Alert management
    redact.py                        # Credential redaction for KB compile
  tests/                             # Test suite
    fixtures/                        # Sample data
    test_cli.py                      # Smoke tests
    test_metrics.py                  # Metrics extraction tests
    test_analyze.py                  # Analysis tests
    test_registry.py                 # Registry tests
    test_pipeline.py                 # Integration tests
  docs/                              # Documentation
  plugins/                           # Built-in analyzers and health checks
```

**Dual packaging:** The plugin provides hooks + skills (auto-registered by Claude Code). The CLI provides user-facing commands (needs `~/projects/clanker/bin` in PATH or symlinked to `~/bin/`). Same repo, two entry points.

**Install globally:** `npx skills add CapitalistCookie/clanker -g -y`. Global scope ensures hooks fire in all projects (project-scoped plugins may not fire outside their project).

### Hook Input/Output

Hooks receive JSON on **stdin** with these fields:

```json
{
  "session_id": "abc123",
  "transcript_path": "/path/to/transcript.jsonl",
  "cwd": "/home/user/projects/zergrush",
  "permission_mode": "allow",
  "hook_event_name": "SessionEnd"
}
```

- **SessionEnd** (not Stop) is used for metrics logging — it's guaranteed to run and cannot block the session
- **SessionStart** uses `$CLAUDE_ENV_FILE` to persist `CLANKER_PROJECT` and `CLANKER_ARCHETYPE` for downstream hooks
- **`transcript_path`** provides direct access to the session .jsonl — no scanning needed

### Configuration Layers

```
Layer 1: ~/.claude/settings.json
  → Universal hooks (git integrity, pre-commit, etc.)
  → User-managed, not owned by clanker

Layer 2: ~/projects/clanker/hooks/hooks.json (plugin format)
  → Clanker-specific hooks (SessionStart alerts, SessionEnd logging)
  → Auto-registered when plugin is installed

Layer 3: ~/.claude/projects/<path-encoded>/settings.json (per-project)
  → Archetype-specific hooks (deploy-gate, gpu-guard, etc.)
  → Generated by `clanker init`, native Claude Code format

Layer 4: <project>/.claude/clanker.local.md (per-project plugin config)
  → Archetype declaration, hook overrides, custom settings
  → Native Claude Code plugin settings pattern (YAML frontmatter + markdown)
```

### Directory Layout

```
~/projects/.clanker.yaml             # Global registry + archetype definitions

~/projects/<name>/.claude/
  clanker.local.md                   # Per-project clanker config (optional, native pattern)

~/.claude/projects/<path>/
  settings.json                      # Per-project hooks (generated by clanker init)

/data/clanker/                       # All runtime data (not in git)
  raw/
    sessions/                        # Per-session JSONL (from SessionEnd hook)
    health/                          # Health check results (from cron)
  wiki/                              # LLM-compiled knowledge base
    projects/                        # One article per project
    patterns/                        # Reusable patterns catalog
    decisions/                       # Architectural decision log
    failures/                        # Failure taxonomy
    index.md                         # Auto-maintained master index
  proposals/
    ledger.jsonl                     # All proposals (append-only, last-write-wins by ID)
  alerts/                            # Active alerts (one JSON file each, deleted on dismiss)
  reports/                           # Weekly/monthly analysis reports
  audit/                             # Immutable change log
```

---

## 3. Feature Inventory

### Tier 1 — Foundation

**Sub-tier 1a: Day 1 (repo setup, basic infrastructure)**

| # | Feature | Description | Depends on |
|---|---------|-------------|------------|
| 1.1 | **Clanker repo + plugin setup** | Clone `CapitalistCookie/clanker`, create `.claude-plugin/plugin.json`, `hooks/hooks.json`, CLI entrypoint in `bin/clanker` (Python + argparse). Install globally. Symlink `bin/clanker` to `~/bin/`. | — |
| 1.2 | **Meta tmux session** | 6th tmux session (`meta`) pointed at `~/projects/clanker`. Added to `.tmux-startup.sh`. SessionStart shows dashboard: project health, pending alerts, unpushed commits, proposal count. | 1.1 |
| 1.3 | **Project registry** | `~/projects/.clanker.yaml` — lists all projects (including clanker itself), archetypes, remotes. `clanker registry list` shows status (branch, last commit, dirty files). | 1.1 |

**Sub-tier 1b: Days 2-3 (data collection)**

| # | Feature | Description | Depends on |
|---|---------|-------------|------------|
| 1.4 | **Session metrics collection** | `SessionEnd` hook (in `hooks/hooks.json`). `session-end.sh` reads `session_id`, `transcript_path`, `cwd` from stdin JSON. Extracts from .jsonl: duration, error count, tool usage breakdown (Bash/Read/Edit/Write/Grep/Glob/Agent), files touched (capped at top 50 by edit count), user corrections (rejected tool calls), session outcome (commit/deploy/abandoned), Claude Code version. Appends to `/data/clanker/raw/sessions/YYYY-MM-DD.jsonl`. Uses `flock` for concurrent session safety. | 1.1 |
| 1.5 | **Alert system — health checks** | Cron (every 15 min): disk >80%, git repos with >20 unpushed commits, stale tmux sessions (>6h no activity), dead systemd services (pgbouncer-style: running but port unreachable), cloudflared tunnel status, stale lock files. Writes alerts to `/data/clanker/alerts/<id>.json`. High-signal only — start strict, loosen as trust builds. | 1.1 |
| 1.6 | **Alert system — SessionStart injection** | `SessionStart` hook (in `hooks/hooks.json`). `session-start.sh` reads `/data/clanker/alerts/`, outputs active alerts as `additionalContext` JSON. Also reads registry, sets `CLANKER_PROJECT` and `CLANKER_ARCHETYPE` in `$CLAUDE_ENV_FILE` for downstream hooks. | 1.5 |
| 1.7 | **Historical data bootstrap** | One-time script ingests existing 460 sessions from `/data/meta-analysis/conversations.json` AND `~/.claude/history.jsonl` (11,401 user inputs) AND `~/.claude/stats-cache.json` (daily aggregates through Feb 17) into `/data/clanker/raw/sessions/` as baseline data. | 1.4 |

**Sub-tier 1c: Days 4-5 (analysis + proposals)**

| # | Feature | Description | Depends on |
|---|---------|-------------|------------|
| 1.8 | **Weekly analysis** | `clanker analyze weekly`: cost-weighted error/time rankings by project, compare to prior week, detect anomalies (>2x change in any metric). `clanker analyze daily`, `clanker analyze errors`, `clanker analyze slow` for ad-hoc. All output JSONL to stdout. | 1.4 |
| 1.9 | **Proposal ledger** | JSONL at `/data/clanker/proposals/ledger.jsonl`. Schema: `{id, source, timestamp, project, type, description, expected_impact, status, decided_at, implemented_at, baseline_metric, actual_metric, notes}`. Status updates: append new line with same ID, readers use last-write-wins. `clanker propose` generates from analysis. `clanker review` lists pending, accepts/rejects interactively (interactive CLI with numbered choices). | 1.8 |
| 1.10 | **Retrospective integration** | Update `session-retrospective` skill to write structured proposals into the ledger via `clanker propose --from-retro`. Skill checks ledger for duplicates (same project + type + similar description) before proposing. | 1.9 |

**Sub-tier 1d: Week 2 (hook dispatch + index ownership)**

| # | Feature | Description | Depends on |
|---|---------|-------------|------------|
| 1.11 | **Archetype-based hook dispatch** | `session-start.sh` reads archetype from registry via `$CLAUDE_PROJECT_DIR`, sets `CLANKER_ARCHETYPE` in `$CLAUDE_ENV_FILE`. Existing hooks in `settings.json` stay (they self-filter already). `clanker init` generates per-project `settings.json` with archetype-appropriate hooks for NEW projects. No migration of existing hooks. | 1.3 |
| 1.12 | **Codebase index ownership** | `session-start.sh` detects if current project has an indexer (checks for `docs/codebase-index/generate.py` or `generate-sdk-reference.sh`). Runs it if found. This SUPPLEMENTS (not replaces) the existing hardcoded quanta-ai/eigenstate hooks in settings.json — those stay until the dynamic indexer is proven stable. | 1.3, 1.11 |
| 1.13 | **CLI composability** | All `clanker` subcommands: read stdin when piped, write JSONL to stdout, accept `--json` flag for machine output. `clanker sessions --last 7d | clanker analyze --by project | clanker propose`. Separate `clanker sessions` (query) from `clanker log` (extract from session — called by hook). | 1.1 |

**Sub-tier 1e: Week 2 (testing + self-tracking)**

| # | Feature | Description | Depends on |
|---|---------|-------------|------------|
| 1.14 | **Test suite** | Smoke tests for each CLI subcommand (exit 0, valid output). Unit tests with fixtures: sample session .jsonl, sample metrics, sample registry, sample proposals. Integration test: full pipeline from session log → analyze → propose. Run with `cd ~/projects/clanker && python3 -m pytest tests/ -v`. | 1.4, 1.8, 1.9 |
| 1.15 | **Self-tracking** | Clanker registered in `.clanker.yaml` as archetype `tool`. Its own sessions tracked. Its own error rate measured. If clanker sessions have high error rates, the weekly analysis surfaces it. | 1.3, 1.4 |

---

### Tier 2 — Intelligence (build after Tier 1 runs for 2+ weeks)

| # | Feature | Description | Depends on |
|---|---------|-------------|------------|
| 2.1 | **Knowledge base — structure** | `/data/clanker/wiki/` with subdirs: `projects/`, `patterns/`, `decisions/`, `failures/`, `index.md`. LLM-maintained, human-queryable. Schema is intentionally loose (free-form markdown) to avoid premature structuring. | 1.4, 1.7 |
| 2.2 | **Knowledge base — compile** | `clanker compile`: invokes Claude Code in the meta tmux session (not API directly — uses the existing session for context). Reads new raw data since last compile, updates wiki articles. Runs weekly after `clanker analyze`. Redaction filter (`lib/redact.py`) strips API keys, passwords, tokens from raw data before LLM sees it. Incremental, not full rebuild. | 2.1 |
| 2.3 | **Knowledge base — Q&A** | In the meta session, ask questions against the wiki. Claude reads relevant articles via `index.md` summaries, researches answers, optionally files outputs back into the wiki. No special tooling — just Claude reading markdown files. | 2.1, 2.2 |
| 2.4 | **Session handoffs** | On session end, auto-generate structured handoff: git status, last commit, what was accomplished, what's next, open questions. Saved to `/data/clanker/wiki/projects/<name>/handoff.md`. Overwritten each session (only latest matters). Next session's briefing includes it. | 2.1 |
| 2.5 | **Project briefings** | Dynamic SessionStart injection: auto-generated from git log (last 5 commits), recent sessions (last 3), open alerts, pending proposals. Injected via `session-start.sh` `additionalContext`. Does NOT require knowledge base — uses raw session data + git directly. Can launch in late Tier 1. | 1.4, 1.5 |
| 2.6 | **Conversation-to-knowledge extraction** | After session end, `session-end.sh` flags sessions containing architecture keywords ("design", "decided", "because", "trade-off"). `clanker compile` scans flagged sessions and extracts explanations into wiki articles. | 2.2 |
| 2.7 | **Regression detection** | Track metrics in windows around harness changes. When a proposal is implemented, record baseline metrics. After 2 weeks, compare. Auto-update proposal `actual_metric` in ledger. "Hook X added → zergrush errors 41.8→15.2." | 1.9, 1.8 |
| 2.8 | **Archetype auto-detection** | For unconfigured projects dropped into `~/projects/`: scan repo (Dockerfile? package.json? pyproject.toml? Cargo.toml? services/ dir?) and SUGGEST archetype. "This looks like a frontend project. Set archetype to `frontend`?" Never auto-apply. | 1.3 |
| 2.9 | **New project onboarding** | `clanker init <name>`: scaffold `.claude/clanker.local.md` (archetype + overrides), generate per-project `settings.json` with archetype hooks, optionally pre-populate CLAUDE.md with patterns from similar projects in the knowledge base. | 1.3, 2.1 |
| 2.10 | **CLAUDE.md refactoring** | Migrate `~/.claude/CLAUDE.md` — universal rules stay global, project-specific config (credentials, service URLs, deployment commands) moves to per-project CLAUDE.md files. Careful standalone migration with rollback plan. NOT bundled with other Tier 2 work. | 1.3 |
| 2.11 | **Knowledge base linting** | `clanker lint-wiki`: find inconsistent data, detect stale info (references to files that no longer exist), suggest missing articles, find broken cross-references. Part of monthly slow loop. | 2.1, 2.2 |
| 2.12 | **Archetype-aware statusline** | Replace hardcoded eigenstate content in `~/.claude/statusline-command.sh` with project-aware display. Read `CLANKER_PROJECT` and `CLANKER_ARCHETYPE` from env. Show relevant metrics per archetype. Centralize GPU VM SSH polling in health check cron (cache result) instead of polling per-session. | 1.6, 1.11 |

---

### Tier 3 — Advanced (build when concrete need arises)

| # | Feature | Description | Depends on |
|---|---------|-------------|------------|
| 3.1 | **Multi-agent orchestration** | `clanker orchestrate`: reads a plan, splits into agent-sized tasks, assigns to worktree-isolated agents with contracts, monitors progress (via session metrics), detects stuck agents (error rate spike, no commits for >30 min), calls human at phase checkpoints. | 1.4, 2.5 |
| 3.2 | **Contract-aware dispatch** | When orchestrating: define TypeScript interfaces / API contracts upfront, distribute to agents, test agent validates contracts at integration. Uses existing `integration-contracts` skill. | 3.1 |
| 3.3 | **Cross-project intelligence** | Detect when a pattern from one project could help another. "zergrush's pre-commit type check reduced errors 40% — eigenstate has similar TypeScript but no type check hook." | 2.1, 2.7 |
| 3.4 | **Resource monitoring** | Extend health check cron: track VM CPU, RAM, swap, disk I/O. Warn before OOM. Correlate resource spikes with active sessions. | 1.5 |
| 3.5 | **Contract/dependency tracking** | Alert when a change in one service might violate cross-service contracts (uses codebase index contract data). | 2.1, 1.12 |
| 3.6 | **Prompt decomposition** | Compare incoming task to historical sessions by keywords/files. "Tasks involving configLoader.ts average 3.2 hours. Consider breaking into smaller steps." Could use `UserPromptSubmit` hook event. | 2.1, 1.4 |
| 3.7 | **Skill effectiveness scoring** | Track which skills fire, which catch real issues, which are overhead. Effectiveness = (issues caught × severity) / (times fired × overhead). | 1.4, 2.7 |
| 3.8 | **Model version tracking** | Claude Code version already in session metrics. Detect behavioral changes after updates: "Error rate +20% after 2.1.92." | 1.4 |
| 3.9 | **Harness versioning** | Snapshot harness state periodically: hook count, skill count, archetype definitions. Track evolution over time. | 1.9, 2.7 |
| 3.10 | **Temporal patterns** | Analyze by time-of-day, day-of-week. "Error rates 3x higher after 10 PM." | 1.4 |
| 3.11 | **Portability** | `clanker export` / `clanker import` — full harness state snapshot for VM migration. | all |
| 3.12 | **Plugin architecture** | Drop scripts in `/data/clanker/plugins/analyzers/` or `plugins/health-checks/`. Standard interface: read JSONL stdin, write findings stdout. | 1.8, 1.5 |
| 3.13 | **Audit trail** | Immutable append-only log at `/data/clanker/audit/changelog.jsonl`. Every harness change traceable to source. | 1.9 |
| 3.14 | **Garbage collection** | `clanker gc`: archive sessions >90 days, expire resolved alerts >7 days, prune rejected proposals >30 days, compact knowledge base. | 2.1 |
| 3.15 | **Skills.sh distribution** | Structure repo for `npx skills add CapitalistCookie/clanker@<skill>`. | 1.1 |
| 3.16 | **Ecosystem discovery** | `clanker suggest-skills`: search skills.sh by archetype/tech stack. | 1.3 |
| 3.17 | **Context window intelligence** | Track file read frequency per project. Suggest high-frequency files for SessionStart injection. | 1.4, 2.1 |

---

## 4. Dependency Graph

```
1.1 Repo + plugin setup
 ├── 1.2 Meta tmux
 ├── 1.3 Registry
 │    ├── 1.11 Hook dispatch (reads registry, sets CLAUDE_ENV_FILE)
 │    │    └── 1.12 Index ownership (supplements existing hooks)
 │    ├── 2.8 Archetype auto-detect
 │    ├── 2.9 Onboarding (generates per-project settings.json)
 │    ├── 2.10 CLAUDE.md refactor (standalone migration)
 │    ├── 2.12 Statusline (reads CLANKER_ARCHETYPE from env)
 │    └── 1.15 Self-tracking
 ├── 1.4 Session metrics (SessionEnd hook, stdin JSON, flock)
 │    ├── 1.7 Bootstrap (ingest history.jsonl + stats-cache + conversations.json)
 │    │    └── 2.1 Knowledge base structure
 │    │         ├── 2.2 Compile (with redaction filter)
 │    │         │    ├── 2.3 Q&A (just Claude reading markdown)
 │    │         │    ├── 2.6 Knowledge extraction (flagged sessions)
 │    │         │    └── 2.11 Wiki linting
 │    │         ├── 2.4 Handoffs
 │    │         └── 2.9 Onboarding (patterns from KB)
 │    ├── 2.5 Briefings (late Tier 1 possible — no KB needed)
 │    ├── 1.8 Weekly analysis
 │    │    ├── 1.9 Proposal ledger (append-only, last-write-wins by ID)
 │    │    │    ├── 1.10 Retrospective integration (dedup check)
 │    │    │    ├── 2.7 Regression detection (baseline/actual metrics)
 │    │    │    └── 3.9 Harness versioning
 │    │    └── 3.10 Temporal patterns
 │    ├── 3.1 Orchestration
 │    │    └── 3.2 Contract dispatch
 │    ├── 3.6 Decomposition (UserPromptSubmit hook)
 │    ├── 3.7 Skill scoring
 │    └── 3.8 Model version tracking
 ├── 1.5 Health checks (cron: disk, git, services, tunnel, locks)
 │    ├── 1.6 Alert injection (SessionStart, CLAUDE_ENV_FILE)
 │    └── 3.4 Resource monitoring
 └── 1.13 CLI composability (clanker sessions vs clanker log)
      └── 1.14 Test suite
```

---

## 5. Data Schemas

### Session Metrics (SessionEnd hook output)

```jsonl
{
  "timestamp": "2026-04-07T02:30:00Z",
  "session_id": "e255075d-12a1-4980-9074-a2ce437379bf",
  "project": "zergrush",
  "cwd": "/home/user/projects/zergrush",
  "duration_s": 3847,
  "claude_version": "2.1.92",
  "tool_uses": {"Bash": 45, "Read": 23, "Edit": 12, "Write": 3, "Grep": 8, "Glob": 5, "Agent": 4},
  "errors": 7,
  "error_tools": {"Bash": 5, "Edit": 2},
  "files_touched": ["zergrush/api.py", "ui-react/src/App.tsx"],
  "files_touched_count": 15,
  "user_corrections": 2,
  "subagent_count": 4,
  "outcome": "commit",
  "commit_sha": "abc1234",
  "flags": ["architecture-discussion"]
}
```

Note: `files_touched` capped at top 50 by edit count. Full list in raw .jsonl if needed.

### Proposal Ledger Entry

```jsonl
{
  "id": "prop-2026-04-07-001",
  "timestamp": "2026-04-07T06:00:00Z",
  "source": "weekly-analysis",
  "source_session": null,
  "project": "zergrush",
  "type": "hook",
  "description": "Add pre-commit TypeScript type check — 60% of errors are tsc failures",
  "expected_impact": "Reduce zergrush errors by ~25 per session",
  "status": "pending",
  "decided_at": null,
  "decided_by": null,
  "implemented_at": null,
  "baseline_metric": {"error_rate": 41.8, "window": "2026-03-31..2026-04-07"},
  "actual_metric": null,
  "notes": ""
}
```

Status updates: append a new line with the same `id` and updated fields. Readers use last-write-wins (last line with matching ID is the current state).

### Alert

```jsonl
{
  "id": "alert-2026-04-07-disk",
  "timestamp": "2026-04-07T03:15:00Z",
  "type": "health-check",
  "severity": "warning",
  "source": "disk-check",
  "message": "Disk usage at 82% (98G/119G).",
  "details": {"usage_pct": 82, "used_gb": 98, "total_gb": 119},
  "status": "active"
}
```

Stored as individual files: `/data/clanker/alerts/alert-2026-04-07-disk.json`. Dismissed by deleting the file.

### Registry (~/projects/.clanker.yaml)

```yaml
defaults:
  index_on_session_start: true
  retrospective_on_stop: true
  track_errors: true

archetypes:
  production:
    hooks:
      - deploy-gate
      - pre-commit-verification
      - post-deploy-screenshot
      - check-git-target
    index_generator: python
    deploy_target: eigenstate-vm

  research:
    hooks:
      - gpu-vm-guard
      - check-compute
      - check-git-target
      - backfill-safety
    index_generator: python
    data_dir: /data/research

  frontend:
    hooks:
      - pre-commit-verification
    index_generator: typescript

  tool:
    hooks: []
    index_generator: none

  infra:
    hooks:
      - deploy-gate
    index_generator: none

projects:
  quanta-ai:
    archetype: production
    remote: CapitalistCookie/quanta-ai
  eigenstate:
    archetype: production
    remote: CapitalistCookie/eigenstate
  eigenstateresearch:
    archetype: research
    remote: CapitalistCookie/eigenstateresearch
  zergrush:
    archetype: frontend
    notes: "Also has Python backend + Rust Tauri + CMake. Single-archetype approximation."
    remote: CapitalistCookie/zergrush
  flowstudio:
    archetype: frontend
    remote: CapitalistCookie/flowstudio
  polymarket:
    archetype: research
    remote: null
  macmini:
    archetype: infra
    remote: CapitalistCookie/macminidev
  titrin:
    archetype: frontend
    remote: CapitalistCookie/titrin
  clanker:
    archetype: tool
    remote: CapitalistCookie/clanker
```

### Per-Project Config (<project>/.claude/clanker.local.md)

```markdown
---
archetype: frontend
hooks_add:
  - branded-pdf-guard
hooks_remove:
  - deploy-gate
settings:
  index_on_session_start: false
---

# Project-specific clanker notes

Any additional context for clanker can go here as markdown.
```

Uses the native Claude Code `.claude/plugin-name.local.md` pattern (YAML frontmatter + markdown body).

---

## 6. Testing Strategy

### Test Fixtures

```
tests/
  fixtures/
    sample_conversation.jsonl    # One realistic session conversation (~100KB)
    sample_metrics.jsonl         # Pre-extracted metrics for 20 sessions
    sample_registry.yaml         # Test registry with 3 projects
    sample_proposals.jsonl       # 5 proposals in various states (pending, accepted, implemented)
    sample_alerts/               # 3 sample alert files
  test_cli.py                   # Smoke: each subcommand exits 0, produces valid output
  test_metrics.py               # Unit: extraction produces correct schema from sample .jsonl
  test_analyze.py               # Unit: analysis produces correct rankings from sample metrics
  test_propose.py               # Unit: proposals generated from analysis data
  test_alerts.py                # Unit: health checks detect known conditions (disk >80%, unpushed commits)
  test_registry.py              # Unit: registry reads/queries correctly, handles malformed YAML gracefully
  test_redact.py                # Unit: redaction strips API keys, passwords, tokens from text
  test_pipeline.py              # Integration: session log → analyze → propose end-to-end
  test_concurrent.py            # Concurrency: two clanker log calls with flock don't corrupt JSONL
```

### Running Tests

```bash
cd ~/projects/clanker && python3 -m pytest tests/ -v
```

---

## 7. CLI Reference

```
clanker <subcommand> [options]

Data Collection:
  log             Extract metrics from current session (called by SessionEnd hook)
  sessions        Query historical session data
                  --last 7d | --project zergrush | --json

Analysis:
  analyze         Run analysis on session data
                  daily | weekly | errors | slow | --by project
  propose         Generate improvement proposals from latest analysis
                  --from-retro (from retrospective skill output)
  review          List and act on pending proposals (interactive CLI)

Infrastructure:
  registry        list | archetype <name> | hooks <name> | path <name>
  alert           list | check [--cron] | dismiss <id> | create <message>
  init            <name> — scaffold new project config + per-project settings.json

Knowledge Base (Tier 2):
  compile         Run LLM knowledge base compilation (with redaction)
  lint-wiki       Check knowledge base consistency

Advanced (Tier 3):
  orchestrate     <plan-file> — dispatch multi-agent work
  export          Snapshot harness state to tarball
  import          Restore harness state from tarball
  gc              Archive old data, expire alerts, prune proposals

Meta:
  version         Show clanker version + harness state summary
  doctor          Run self-diagnostics (registry valid, hooks installed, data dirs exist)

Composability:
  clanker sessions --last 7d | clanker analyze --by project
  clanker analyze errors | head -10
  clanker alert list --json | jq '.[] | select(.severity == "critical")'

Environment:
  CLANKER_DATA=/data/clanker                     # Data directory
  CLANKER_REGISTRY=~/projects/.clanker.yaml      # Registry file
  CLANKER_PROJECT (set by SessionStart hook)      # Current project name
  CLANKER_ARCHETYPE (set by SessionStart hook)    # Current project archetype
```

---

## 8. Hook Wiring

### Plugin hooks (~/projects/clanker/hooks/hooks.json)

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh",
            "timeout": 5000
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/session-end.sh",
            "timeout": 15000
          }
        ]
      }
    ]
  }
}
```

These merge with user's settings.json hooks and run in parallel. No settings.json edits needed.

### Cron jobs (added to user crontab)

```crontab
# Clanker health checks (every 15 min)
*/15 * * * * /home/user/bin/clanker alert check --cron 2>&1 | head -100 >> /data/clanker/raw/health/$(date +\%Y-\%m-\%d).jsonl

# Clanker weekly analysis (Sunday 6 AM UTC)
0 6 * * 0 /home/user/bin/clanker analyze weekly > /data/clanker/reports/week-$(date +\%Y-\%m-\%d).md 2>&1; /home/user/bin/clanker propose >> /data/clanker/reports/week-$(date +\%Y-\%m-\%d).md 2>&1
```

---

## 9. Pre-Existing Issues to Fix (discovered during audit)

These are not clanker features — they're operational issues discovered during the 11 sweep rounds. Fix before or alongside Tier 1.

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | `daily_data_collector` cron broken (path deleted in cleanup) | **Critical** | Update cron path to `/home/user/projects/eigenstateresearch/research/scripts/daily_data_collector.py` |
| 2 | 26 prediction crons silently failing (~200 curl/hour to nonexistent localhost:8082) | **High** | Ask user: remove, or relocate to eigenstate-vm? |
| 3 | pgbouncer running but dead (port 5434 doesn't exist) | Medium | `sudo systemctl disable pgbouncer && sudo systemctl stop pgbouncer` |
| 4 | Dead `~/bin/claude-sessions.sh` could kill sessions if accidentally run | Medium | Rename to `claude-sessions.sh.deprecated` |
| 5 | `scheduled_tasks.lock` stale (357 hours, PID dead) | Low | Delete lock file |
| 6 | `~/titrin-email-worker/` orphaned (not in projects, not version-controlled) | Low | Move to `~/projects/titrin/email-worker/` or `~/projects/titrin-email-worker/` |
| 7 | `cloud-sql-proxy` binary in ~/ (30MB, post-migration leftover) | Low | Ask user: still needed? |
| 8 | `Market-Master-*` in ~/ (557MB, stale since Nov 2025) | Low | Ask user: move to projects or delete? |

---

## 10. Implementation Priorities

### Tier 1 order

1. Fix pre-existing issues (#1-5 from section 9)
2. **1.1** Repo + plugin setup, CLI skeleton
3. **1.2** Meta tmux session
4. **1.3** Registry
5. **1.4** Session metrics (SessionEnd hook + flock)
6. **1.5 + 1.6** Health checks + alert injection
7. **1.7** Historical bootstrap
8. **1.8** Analysis
9. **1.9** Proposal ledger
10. **1.10** Retrospective integration
11. **1.11** Archetype dispatch (via CLAUDE_ENV_FILE)
12. **1.12** Codebase index (supplement, don't replace)
13. **1.13** CLI composability
14. **1.14** Test suite
15. **1.15** Self-tracking verification

### Tier 2 prerequisites

- 2+ weeks of session data in the pipeline
- Knowledge base compile needs enough raw data to be useful
- Project briefings (2.5) can ship in late Tier 1 without the KB
- CLAUDE.md refactoring (2.10) is a standalone migration — don't bundle

### Tier 3 triggers

- Orchestration (3.1): when a multi-agent project surfaces
- Portability (3.11): when a VM migration is planned
- Plugins (3.12): when someone else wants to extend clanker
- GC (3.14): when /data/clanker/ exceeds 1GB

---

## 11. Success Criteria

### Tier 1 is successful when:

- [ ] `clanker registry list` shows all 9+ projects with archetypes
- [ ] Every session automatically logs metrics via SessionEnd hook
- [ ] Health check cron runs every 15 min, alerts surface at SessionStart
- [ ] `clanker analyze weekly` produces a cost-weighted report
- [ ] `clanker review` shows pending proposals from both analysis and retrospective
- [ ] Proposal ledger tracks status changes (pending → accepted → implemented)
- [ ] `clanker doctor` passes (registry valid, hooks installed, dirs exist, no stale locks)
- [ ] Test suite passes
- [ ] Clanker's own sessions are tracked and visible in analysis

### The harness is working when:

- Error rates trend down across projects over 4 weeks
- Proposals are generated, reviewed, and implemented with measurable outcomes
- Alerts catch real problems before users notice
- New projects onboarded with `clanker init` in under 5 minutes
- The weekly report surfaces actionable insights (not noise)

---

## 12. Open Questions (Resolved and Remaining)

### Resolved

| # | Question | Resolution |
|---|----------|------------|
| 1 | How does `clanker log` find the session? | Stdin JSON provides `session_id`, `transcript_path`, `cwd`. No scanning needed. |
| 2 | Which hook event for logging? | `SessionEnd` (guaranteed to run, doesn't block). Not `Stop` (can block session). |
| 3 | How to detect project archetype at runtime? | SessionStart hook reads registry via `$CLAUDE_PROJECT_DIR`, writes to `$CLAUDE_ENV_FILE`. |
| 4 | Per-project config format? | Native `.claude/clanker.local.md` (YAML frontmatter). Not custom `.clanker/config.yaml`. |
| 5 | How to handle concurrent Stop hooks? | `flock` on the daily JSONL file. |
| 6 | Plugin vs loose scripts? | Claude Code plugin (hooks/hooks.json). Not settings.json edits. |
| 7 | Global vs project-scoped install? | Global. Project-scoped plugins may not fire outside their project. |

### Remaining

| # | Question | When to resolve |
|---|----------|-----------------|
| 1 | Token cost of `clanker compile`? | When building Tier 2. Profile with a small wiki first. |
| 2 | Wiki schema evolution strategy? | When the wiki has 50+ articles. Keep loose until then. |
| 3 | Alert fatigue threshold? | After 2 weeks of health check data. Start strict. |
| 4 | Orchestration contract format? | When building Tier 3. Depends on project type. |
| 5 | How to invoke `clanker compile` LLM step? | Meta session Claude, API call, or subprocess? Profile cost of each. |
| 6 | zergrush archetype — is "frontend" correct? | It has Python + Rust + CMake + React. May need multi-archetype or custom archetype. |

---

## 13. Audit Trail — Issues Discovered

46 issues found across 11 sweep rounds. All traced to endpoints.

<details>
<summary>Full issue list (click to expand)</summary>

### Migration sweep (issues 1-30)

1. Active processes in zergrush/macmini dirs → kill before moving
2. zergrush pip editable install hardcodes path → `pip install -e` after move
3. zergrush git worktree has absolute gitdir → remove before move
4. zergrush `.git/config` has absolute hooksPath → unset
5. `.claude.json` project keys + githubRepoPaths → JSON surgery
6. Claude project dirs encoded by path → don't rename (breaks resume)
7. 55 polymarket Python scripts with 120 hardcoded paths → two sed patterns
8. `.tmux-startup.sh` hardcoded paths → update 3 lines
9. 7 memory files with path references → sed
10. titrin `serve.py` + `visual-audit.py` → 3 path fixes
11. FlowStudio deploy docs → 80+ path occurrences, one sed job
12. zergrush Rust target/ → 1.5GB, sed .d files, rebuild for .o/.so
13. zergrush CMake builds → sed text files, rebuild for binaries
14. zergrush node_modules → npm install after move
15. FlowStudio .next cache → rebuild
16. pnpm store refs → pnpm install after move
17. titrin images-original symlink → recreate
18. FlowStudio ARCHITECTURE.md + plan docs → 70+ refs
19. tmux-resurrect snapshots → self-heals
20. Claude session .json cwd refs → cosmetic
21. zergrush e2e test artifacts → 54 stale paths
22. polymarket `__pycache__` → compileall regenerates
23. polymarket log files → historical
24. `.claude/plans/` archived plans → stale but not loaded
25. `.claude/file-history/` snapshots → read-only
26. Subagent meta.json worktree path → cosmetic
27. Docker buildx cache → `docker buildx prune`
28. `.claude.json` polymarket project key → JSON surgery
29. FlowStudio → flowstudio casing change → safe (imports already lowercase)
30. `clanker review` not a separate feature → part of 1.9

### Architecture discoveries (issues 31-39)

31. Dead `~/bin/claude-sessions.sh` → rename to .deprecated
32. `~/bin/claude-tmux` helper → no conflict
33. `daily_data_collector` cron path broken by cleanup → **fix immediately**
34. Same as 33 (traced to endpoint)
35. SQLite rsync cron → investigate-later
36. Per-project settings.json exists natively → use instead of custom config
37. Plugin scope unclear → install globally
38. Statusline has hardcoded eigenstate content → Tier 2 fix
39. Statusline SSH polling per-session → centralize in health check cron

### System audit (issues 40-46)

40. pgbouncer connects to dead port 5434 → disable service
41. Entire local DB stack dead (no 5432/5433/5434) → expected post-migration
42. `~/titrin-email-worker/` orphaned → move to projects
43. `~/.claude/history.jsonl` (11K entries) → additional data source for bootstrap
44. `~/.claude/stats-cache.json` → cross-check for metrics
45. `scheduled_tasks.lock` stale → delete
46. No MCP servers configured → informational

</details>
