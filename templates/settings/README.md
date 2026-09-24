# Archetype settings templates

Each file holds the keys that a repo of one archetype must carry in its tracked
`.claude/settings.json`. `clanker settings check` compares each repo with its
template. `clanker settings apply <repo> --key <key>` writes one key from the
template into the repo.

| Template | Used by |
|---|---|
| `research.json`, `production.json`, `tool.json`, `infra.json`, `frontend.json` | The repos of that archetype in the registry |
| `build.json` | The repos that use claude.ai connectors. Select it in the registry with `settings_templates: {overrides: {<repo>: build}}`. |

A template holds only keys that Claude Code reads from project settings.
`disableClaudeAiConnectors` is such a key: in Claude Code 2.1.280, a `true`
value in any settings source stops the claude.ai connectors, and a project
cannot set `false` over a `true` from the user settings.
