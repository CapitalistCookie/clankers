# Clanker

Self-improving development harness for Claude Code. Tracks sessions, manages projects, detects patterns, and proposes improvements.

## Quick Start

```bash
# Install
npx skills add CapitalistCookie/clanker -g -y

# Symlink CLI
ln -sf ~/projects/clanker/bin/clanker ~/bin/clanker

# Check health
clanker doctor
clanker alert check
clanker registry list
```

## Architecture

```
Pipeline (deterministic)              Knowledge Base (LLM-maintained)
─────────────────────────             ──────────────────────────────
SessionEnd hook → metrics             clanker compile → wiki/
Health cron → alerts                   Q&A in meta session
clanker analyze → rankings             Outputs feed back into wiki
clanker propose → proposals
```

**Pipeline** = nervous system. Collects signals, detects anomalies.
**Knowledge base** = brain. Synthesizes understanding, answers questions.

## Commands

### Tier 1 — Foundation
| Command | Description |
|---------|-------------|
| `clanker registry list` | Show all projects with archetypes |
| `clanker sessions --last 7` | Recent session metrics |
| `clanker analyze weekly` | Cost-weighted analysis |
| `clanker propose` | Generate improvement proposals |
| `clanker review` | Interactive proposal review |
| `clanker alert check` | Run health checks |
| `clanker alert list` | Show active alerts |
| `clanker version` | Status overview |
| `clanker doctor` | Self-diagnostics |

### Tier 2 — Intelligence
| Command | Description |
|---------|-------------|
| `clanker compile` | Build knowledge base from session data |
| `clanker lint-wiki` | Check wiki consistency |
| `clanker briefing <project>` | Project status briefing |
| `clanker init <name>` | Onboard new project |

### Tier 3 — Advanced
| Command | Description |
|---------|-------------|
| `clanker orchestrate <plan>` | Multi-agent task dispatch |
| `clanker cross-project` | Cross-project pattern analysis |
| `clanker contracts <project>` | API contract scanning |
| `clanker context --project X` | File frequency analysis |
| `clanker temporal` | Time-of-day/day-of-week patterns |
| `clanker model-versions` | Analyze by Claude Code version |
| `clanker resources` | VM resource monitor |
| `clanker plugins list` | Drop-in plugin management |
| `clanker export` | Export harness state |
| `clanker gc` | Garbage collection |
| `clanker audit` | View change log |
| `clanker snapshot` | Harness version snapshot |
| `clanker suggest-skills` | Search skills.sh ecosystem |

## Composability

```bash
clanker sessions --last 7 --json | clanker analyze --by project
clanker alert list --json | jq '.[] | select(.severity == "critical")'
clanker analyze errors | head -10
```

## Project Registry

Projects are defined in `~/projects/.clanker.yaml` with archetypes:

- **production** — deploy-gate, pre-commit, post-deploy screenshot
- **research** — GPU guard, compute routing, git target check
- **frontend** — pre-commit verification
- **tool** — minimal hooks
- **infra** — deploy-gate

## Plugins

Drop scripts in `/data/clanker/plugins/`:
- `analyzers/` — read JSONL, write findings
- `health-checks/` — exit 0 = OK, non-zero = problem

## Data

All runtime data in `/data/clanker/`:
- `raw/sessions/` — per-session JSONL metrics
- `raw/health/` — health check logs
- `wiki/` — LLM-compiled knowledge base
- `proposals/ledger.jsonl` — improvement proposals
- `alerts/` — active alert files
- `reports/` — weekly analysis reports
- `audit/changelog.jsonl` — immutable change log

## Hooks

- **SessionStart**: alert injection, project briefing, archetype env vars, auto-resume queue surfacing
- **SubagentStop**: usage/rate-limit auto-resume — re-queue limit-killed subagents for context-aware re-dispatch (see [docs/AGENT_AUTO_RESUME.md](docs/AGENT_AUTO_RESUME.md); note the `run_in_background` caveat)
- **SessionEnd/Stop**: session metrics extraction, handoff generation

## License

MIT
