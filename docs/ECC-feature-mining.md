# ECC → Clanker feature-mining catalog

Exhaustive enumeration of liftable features from `affaan-m/ECC` ("Everything Claude Code"),
mined 2026-06-10 across three trees: the `ecc2/` Rust control-plane, the `scripts/` Node
toolchain (~170 files), and the skills/rules/config/docs/MCP layer. ECC's `ecc2/` aims at the
**same target as clanker** (observable, autonomous multi-agent coding), so its self-contained
algorithms port well even though the stack differs (Rust/TUI vs Python/web).

Verdicts: **🟢 LIFT** = port the algorithm ~as-is · **🟡 ADAPT** = rework for clanker's stack ·
**⭐** = highest leverage. Clanker already has: cost tracking, autonomy scoreboard, ntfy push,
health alerts, tmux PTY bridge, KB wiki, TOTP auth — so many items below are *upgrades*.

---

## A. Safety / guardrails (autonomy-critical)

| Feature | What | Verdict |
|---|---|---|
| ⭐ Tool-call risk scorer | Additive 0–1 score: base-tool + file-sensitivity (`.env`/`id_rsa`/`.pem`+0.25) + blast-radius (`push -f`/`rm -rf`+0.35) + irreversibility (`reset --hard`/`drop table`+0.45) → Allow/Review/Confirm/Block. `ecc2/src/observability/mod.rs` | 🟢 LIFT |
| ⭐ Destructive-command classifier | `isDestructiveGit/Rm` + subshell-BFS evasion-proofing. `scripts/hooks/gateguard-fact-force.js` | 🟢 LIFT |
| Governance event capture | Detects secret-leak / approval-needed / policy-violation / security-finding in tool I/O; emits structured events. `scripts/hooks/governance-capture.js` | 🟢 LIFT |
| block `--no-verify` | Shell tokenizer that blocks hook-bypass even across chains/subshells, no false-positive on `-m` body (25 tests). `scripts/hooks/block-no-verify.js` | 🟢 LIFT |
| Shell-substitution/split engine | `extractCommandSubstitutions`/`extractSubshellGroups`/`extractBraceGroups` — the parser that makes the above evasion-proof. `scripts/lib/shell-{substitution,split}.js` | 🟢 LIFT |
| config-protection | Block edits to linter/formatter configs (fix code, don't weaken rules); fail-closed on truncated input. `scripts/hooks/config-protection.js` | 🟡 ADAPT |
| Prompt Defense Baseline | A compact injection/secret-leak guard preamble stamped into every dispatched agent prompt. `CLAUDE.md` | 🟡 ADAPT |
| Supply-chain IoC + unicode-safety | Scan lockfiles for known-malicious versions; bidi/zero-width/homoglyph scan. `scripts/ci/{scan-supply-chain-iocs,check-unicode-safety}.js` | 🟡 ADAPT |

## B. Cost / token tracking (you have a version; these upgrade it)

| Feature | What | Verdict |
|---|---|---|
| Cost tracker (transcript-sum) | Sum `usage.*` across assistant turns from the transcript JSONL + per-model rates; prefer fresh `harness-cost-<id>.json`. **Independently confirms clanker's read-the-transcript approach.** `scripts/hooks/cost-tracker.js` | 🟢 LIFT |
| Cache-tier rate table | Per-1M rates incl. `cacheWrite` 1.25× / `cacheRead` 0.1× — matters for Claude's heavy prompt-cache. `scripts/hooks/cost-tracker.js:71` | 🟢 LIFT |
| ⭐ Budget hard-stop | Global + per-profile token/cost limits that **pause active sessions** when tripped. `ecc2/src/session/manager.rs:1162` | 🟢 LIFT |
| Budget meter + gradient ladder | used/budget → Normal/50/75/90/Over + green→red interpolation. `ecc2/src/tui/widgets.rs` | 🟡 ADAPT |
| "No-fabrication" rule | If cost DB missing, do NOT invent usage. (skill `cost-tracking`) | 🟡 ADAPT |

## C. Stuck / stale / loop detection (feeds ntfy + health — high value)

| Feature | What | Verdict |
|---|---|---|
| ⭐ Parked-session detector | Walk transcripts, find `tool_use` with no matching `tool_result` >30min (session waiting) + overdue `ScheduleWakeup`; per-session attention state. `scripts/loop-status.js` | 🟢 LIFT |
| Context-monitor + loop detector | Thresholds (ctx 35/25%, cost $5/10/50, files>20) + `detectLoop` (≥3 identical tool calls in last 5) + content-based dedupe that clears on resolve. `scripts/hooks/ecc-context-monitor.js` | 🟢 LIFT |
| Tool-call fingerprint | `hashToolCall` hashes the *change* (Bash prefix / Edit payload) not just the path — load-bearing for loop detection. `scripts/hooks/ecc-metrics-bridge.js` | 🟢 LIFT |
| Heartbeat staleness + crash recovery | Mark sessions Stale past timeout; on restart, reconcile orphans via `kill(pid,0)`. `manager.rs:787`, `daemon.rs:58` | 🟢 LIFT |

## D. Autonomy scoreboard upgrades (you have a basic one)

| Feature | What | Verdict |
|---|---|---|
| ⭐ Run-record schema | `{outcome: success/failure/partial, user_feedback: accepted/corrected/rejected, tokens, duration}` — `corrected` **is** corrections/session. `scripts/lib/skill-evolution/tracker.js` | 🟢 LIFT |
| ⭐ Success-rate + trend | 7d-vs-30d success rate, trend (worsening/improving/stable via delta), `declining` flag — a real regression detector. `scripts/lib/skill-evolution/health.js` | 🟢 LIFT |
| Sparkline dashboard | bucket-by-day + `▁▂▃▄▅▆▇█` + trend arrows. `scripts/lib/skill-evolution/dashboard.js` | 🟡 ADAPT |
| Failure clustering | `normalizeFailureReason` (strip timestamps/UUIDs/paths so failures group) + recurring-pattern detection. `scripts/lib/inspection.js` | 🟢 LIFT |
| Amendment proposer | Failure-cluster → structured fix proposal (guardrails/example/verification-checklist); `insufficient-evidence` when none. `scripts/lib/skill-improvement/amendify.js` | 🟡 ADAPT |
| Recursive decision ledger | Append-only rollout decisions (prior winner, fresh info, accept/watch/reject). skill `recursive-decision-ledger` | 🟡 ADAPT |

## E. Dashboard / observability contracts & views

| Feature | What | Verdict |
|---|---|---|
| ⭐ `ecc.hud-status.v1` contract | Versioned dashboard payload: `context/toolCalls(pending,stale)/activeAgents/cost(trend)/risk/queueState/sessionControls(supported,blocked)`. Rule: missing section = "unavailable", **never green**. `docs/architecture/hud-status-session-control.md` + `examples/hud-status-contract.json` | 🟡 ADAPT |
| `ecc.session.v1` snapshot | Versioned worker snapshot (state/health/runtime/intent/outputs/artifacts/aggregates) + graceful-degrade-on-unknown. `docs/SESSION-ADAPTER-CONTRACT.md` | 🟡 ADAPT |
| Attention queue | "What needs the operator now" list (approvals/conflicts/budget/stale) → drives ntfy. `dashboard.rs:6821` | 🟡 ADAPT |
| Test-run summary | Parse agent output for test pass/fail counts (surface "did tests pass"). `dashboard.rs:77` | 🟡 ADAPT |
| In-progress todo reader | Read `~/.claude/todos/<session>-agent-*.json` to show "what each session is doing right now" on tiles. `scripts/hooks/ecc-statusline.js` | 🟢 LIFT |
| Context-bar w/ auto-compact buffer | Account for the 16.5% auto-compact buffer in the per-session context gauge. `scripts/hooks/ecc-statusline.js` | 🟡 ADAPT |
| Fleet usage aggregate | Roll up tokens+cost across all sessions. `dashboard.rs:6066` | 🟢 LIFT |
| Operator command vocabulary | ~70 actions (spawn/assign/stop/resume/diff/PR/merge/rebalance/search…) — a feature checklist for the web UI. `tui/app.rs:66` | 🟡 ADAPT |
| Cross-session search | Search across all agent terminals (scope current/all, next/prev). `dashboard.rs:5689` | 🟡 ADAPT |

## F. Session orchestration / spawning (your many-sessions model)

| Feature | What | Verdict |
|---|---|---|
| ⭐ Claude CLI command builder | Exact recipe: `claude --print --name ecc-<id> --model --allowed-tools --disallowed-tools --permission-mode --add-dir --max-budget-usd --append-system-prompt`. `manager.rs:3071` | 🟢 LIFT |
| ⭐ Assignment policy | Route a task: reuse best idle delegate (context-affinity) → spawn if under cap → least-loaded idle → best active; returns Spawned/ReusedIdle/ReusedActive/DeferredSaturated. `manager.rs:3798` | 🟢 LIFT |
| Session state machine | 7 states + `can_transition_to()` guard **enforced at the DB layer**. `session/mod.rs:321`, `store.rs update_state` | 🟢 LIFT |
| ⭐ File-overlap detection | Find other active sessions touching the same paths (collision detection — a known clanker want). `store.rs:4006` | 🟢 LIFT |
| Merge-conflict dry-run | `git merge-tree --write-tree` predicts conflicts **without merging**. `worktree/mod.rs:722` | 🟢 LIFT |
| Dep-cache symlinking | Detect node_modules/target/.venv by lockfile SHA256 fingerprint; symlink cache into worktree only if fingerprints match. `worktree/mod.rs:1202` | 🟢 LIFT |
| Stable project identity | `computeProjectId`/`normalizeRemoteUrl` (from git remote, credentials stripped). `scripts/lib/observer-sessions.js` | 🟢 LIFT |
| tmux-worktree orchestrator | worktree + tmux-window per worker, seed-path overlay, templated launchers, per-worker task/handoff files. `scripts/lib/tmux-worktree-orchestrator.js` | 🟡 ADAPT |
| Worktree lifecycle | Stale/conflicting-worktree detection + cleanup planning (avoid sprawl). `scripts/lib/worktree-lifecycle/*` | 🟡 ADAPT |
| Daemon control loop + backoff | Periodic supervise pass + cooloff/saturation-streak/stabilized hysteresis so auto-dispatch doesn't thrash. `daemon.rs`, `manager.rs coordination` | 🟡 ADAPT |

## G. Knowledge base / memory (you have a wiki)

| Feature | What | Verdict |
|---|---|---|
| Context-graph recall scoring | Term-match + relations + observations + recency-band + pinned → ranked recall, **no embeddings**. `store.rs:2965,4986` | 🟢 LIFT |
| Observation compaction | Dedup + retain N newest + preserve pinned (bounded append-only KB). `store.rs:3245` | 🟢 LIFT |
| Memory connectors + dotenv redaction | Ingest jsonl/markdown/dotenv → entities; **redact secret keys on import**. `main.rs:2787` | 🟡 ADAPT |
| `longhand` MCP | Lossless Claude session history → SQLite+Chroma before rotation. `.mcp.json` | 🟡 ADAPT (evaluate) |

## H. Hook architecture / operability

| Feature | What | Verdict |
|---|---|---|
| Hook profiles + per-id toggle | `ECC_HOOK_PROFILE=minimal/standard/strict` + `ECC_DISABLED_HOOKS=csv` + stable hook ids. `scripts/lib/hook-flags.js` | 🟢 LIFT |
| Consolidated bash dispatcher | One pre/post-bash process fans out to ordered sub-checks, short-circuit on block. `scripts/hooks/bash-hook-dispatcher.js` | 🟡 ADAPT |
| Batch-at-Stop | Accumulate edited paths per-edit; run expensive format/typecheck **once at Stop**. `post-edit-accumulator.js` + `stop-format-typecheck.js` | 🟡 ADAPT |
| In-process hook dispatch | `require()+run()` over spawning node each time (~50-100ms). `scripts/hooks/run-with-flags.js` | 🟡 ADAPT |
| `hookify-rules` | Markdown-frontmatter rules that compile to hooks (lighter than hand-writing). skill `hookify-rules` | 🟡 ADAPT |

## I. Notifications (you have ntfy)

| Feature | What | Verdict |
|---|---|---|
| Quiet-hours (cross-midnight) | Per-event toggles + quiet-hours with wrap-around-midnight handling. `ecc2/src/notifications.rs:18` | 🟢 LIFT |
| Slack/Discord webhook fan-out | Multi-target, disabled `allowed_mentions`, URL sanitization. `notifications.rs:294` | 🟢 LIFT |
| `extractSummary` | Last assistant message → first non-empty line, truncated → the ntfy body. `scripts/hooks/desktop-notify.js` | 🟢 LIFT |
| OSC-9 / tmux gotcha | tmux/screen swallow OSC-9 desktop notifications — detect + fall back. (note) | NOTE |

## J. Self-improvement loop / self-audit (your thesis)

| Feature | What | Verdict |
|---|---|---|
| ⭐ Observability-readiness gate | "Be observable before more autonomous" — ordered checklist of signals (status emitter, tool log, risk ledger, sync) before raising autonomy. `docs/architecture/observability-readiness.md` | 🟡 ADAPT |
| Harness-audit scorecard | 7 re-runnable axes: tool coverage, context efficiency, quality gates, memory, eval, security, cost. `scripts/harness-audit.js` | 🟡 ADAPT |
| continuous-learning-v2 | observe → analyze → "instinct" score → evolve into skills/commands. `docs/continuous-learning-v2-spec.md` | 🟡 ADAPT |
| skill-comply | Auto-generate scenarios, run agents, **measure whether skills/rules are actually followed**. skill `skill-comply` | 🟡 ADAPT |
| conversation-analyzer → hookify | Mine transcripts for behaviors worth preventing → propose a guard hook. agent `conversation-analyzer` | 🟡 ADAPT |
| Stale-replay guard | "HISTORICAL REFERENCE ONLY — do not re-execute" wrap on resumed-session summaries. `scripts/hooks/session-start.js` | 🟡 ADAPT |
| Evaluate-session length gate | Only retrospect sessions above N user turns. `scripts/hooks/evaluate-session.js` | 🟡 ADAPT |

## K. Config / project-awareness

| Feature | What | Verdict |
|---|---|---|
| project-stack-mappings | Detect stack (tsconfig/etc.) → auto-enable rules/skills/hooks + per-stack **permission allow/deny** (allow `npx tsc`, deny `npm publish`). `config/project-stack-mappings.json` | 🟡 ADAPT |
| Layered global+project config | XDG-global + nearest-dir project, deep TOML merge. `config/mod.rs:493` | 🟢 LIFT |
| Agent-profile inheritance | Named presets (model/tools/permission/budget/system-prompt) with `inherits` chains + cycle detection. `config/mod.rs` | 🟢 LIFT |
| Orchestration templates | Multi-step pipelines with `{{var}}` interpolation + missing-var validation. `config/mod.rs` | 🟢 LIFT |
| project-detect | Language/framework detection (dependency-free). `scripts/lib/project-detect.js` | 🟡 ADAPT |

## L. Reusable primitives

| Feature | What | Verdict |
|---|---|---|
| Atomic JSON write + sanitizeSessionId | unique-tmp + rename (corruption-proof concurrent writes) + traversal-safe session id. `scripts/lib/session-bridge.js` | 🟢 LIFT |
| Single-writer DB actor | Dedicated thread owns SQLite; writes over a channel — avoids lock contention from concurrent web+CLI. `ecc2/src/session/runtime.rs` | 🟡 ADAPT |
| Output ring buffer + broadcast | Bounded deque (1000) + pub/sub fan-out for live tails. `ecc2/src/session/output.rs` | 🟡 ADAPT |
| Misc | char-safe truncate · FNV-1a deterministic IDs · github-compare-URL normalizer · git porcelain parser · patch-hunk splitter · `stripAnsi`/`findFiles(maxAge)`/`readStdinJson` | 🟢 LIFT |

---

## SKIP (entire buckets, with reason)

- **Install/catalog/multi-harness distribution engine** (`scripts/install-*`, `lib/install-*`, per-harness adapters) — clanker is a single-harness runtime, not a content distributor.
- **Hermes/OpenClaw migration suite** (`main.rs` ~981–7430) — no legacy to migrate from.
- **ratatui TUI rendering** (`tui/app.rs` draw loop, most of `dashboard.rs` draw code, `widgets.rs` render) — clanker is web/xterm; mine the *information architecture*, discard the rendering.
- **JS/TS-specific format/typecheck hooks** (`post-edit-{format,typecheck}`, `quality-gate`, `resolve-formatter`) — language-locked; keep the *batch-at-Stop pattern* only.
- **261 domain skills + 64 domain agents** (react/laravel/healthcare/cotton verticals) — Markdown content; clanker has its own ecosystem. Only the ~dozen *meta/harness* skills/agents are concept-relevant (harness-optimizer, loop-operator, conversation-analyzer, silent-failure-hunter, skill-comply, parallel-execution-optimizer).
- **CI/release/marketing** (`release*`, `*-audit` for GitHub queues, discord announce, codemaps, video suite) — repo-maintenance.
- **Tkinter `ecc_dashboard.py`** — strictly inferior to clanker's xterm/aiohttp dashboard.
- **Computer-use/browser dispatch, hand-rolled TCP HTTP server, Windows detached-process flags** — out of domain / use aiohttp / Linux-only.

## Caveat
ECC2's README states it is **alpha scaffolding, not battle-tested** — several systems
(orchestration templates, remote dispatch, migration) are breadth-first and lightly exercised.
Treat the larger orchestration pieces as design references; the small pure algorithms
(risk scorer, parked-session detector, cost-sum, recall scoring, trend math) are the safe ports.

---

## Recommended port order for clanker

**Phase 1 — small, pure, high-leverage (a few hundred lines total):**
1. ⭐ Risk scorer (A) → per-session "risk" column + alert on dangerous ops.
2. ⭐ Parked-session detector (C) → upgrades the ntfy "needs input" + stuck-session health.
3. ⭐ Run-record schema + 7d/30d trend + failure clustering (D) → upgrades the autonomy scoreboard into a regression detector.
4. Cost cache-tier rates + budget hard-stop (B).

**Phase 2 — orchestration & contracts:**
5. Claude CLI command builder + assignment policy + file-overlap detection (F) — the substrate for clanker spawning/routing its own sessions (also the basis for auto-nudge).
6. `ecc.hud-status.v1` dashboard contract + attention queue + in-progress-todo reader (E).
7. Hook profiles + governance-capture + destructive-command classifier + block-no-verify (A/H).

**Phase 3 — depth:** context-graph recall (G), quiet-hours/webhooks (I), observability-readiness gate + harness scorecard + continuous-learning-v2 (J), project-stack permission mapping (K).
