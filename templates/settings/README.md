# Archetype settings templates

Each file holds the keys that a repo of one archetype must carry in its tracked
`.claude/settings.json`. `clanker settings check` compares each repo with its
template. `clanker settings apply <repo> --key <key>` writes one key from the
template into the repo.

| Template | Used by |
|---|---|
| `research.json`, `production.json`, `tool.json`, `infra.json`, `frontend.json` | The repos of that archetype in the registry |
| `build.json` | The repos that use claude.ai connectors or the Artifact tool. Select it in the registry with `settings_templates: {overrides: {<repo>: build}}`. |

A template holds only keys that Claude Code reads from project settings.
`disableClaudeAiConnectors` is such a key: in Claude Code 2.1.280, a `true`
value in any settings source stops the claude.ai connectors, and a project
cannot set `false` over a `true` from the user settings.

## Tool set policy (2026-09-24)

The five archetype templates also remove two tools that these repos do not use.
The token counts are for one session with Fable 5.1 or Opus 5.5.

| Key | Effect | Tokens saved per session |
|---|---|---|
| `"enableArtifact": false` | Removes the Artifact tool and its ArtifactComments and ArtifactData add-on tools. Project settings can turn Artifact off. They cannot turn it on. | 9,967 always loaded, 6,066 deferred |
| `"permissions": {"deny": ["ScheduleWakeup"]}` | Removes the ScheduleWakeup tool from the tool list that goes to the model. | 1,695 |

`build.json` keeps both tools.

Exceptions:

- `fableNQkronos` keeps ScheduleWakeup, because its sessions use the tool to wait for long runs. `check` shows this repo with `missing: permissions`. This is intentional.
- `omnigentfork` uses Artifact, but the registry gives it no `build` override. Do not apply `enableArtifact` to this repo.

`check` compares the whole `permissions` object. A repo with more permission
rules shows `differs: permissions`. `apply --key permissions` writes the whole
`permissions` object of the template and replaces a `permissions` block that is
already in the file. If the repo has a `permissions` block, add the `deny` entry
by hand.
