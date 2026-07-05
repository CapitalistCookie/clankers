---
name: clanker-session
description: Track development sessions for analysis and improvement. Call clanker CLI at session start and end to log metrics. Activate at the beginning of any work session.
---

# Session Tracking

Track this session so clanker can analyze patterns, detect issues, and suggest improvements.

## At the START of this session

Run this command once to register the session:

```bash
clanker ingest --event session_start --project $(basename $PWD) --agent <your-agent-name>
```

Replace `<your-agent-name>` with the name of the agent you're using (e.g., `claude-code`, `cursor`, `cline`, `gemini-cli`).

## At the END of this session

When the user says they're done, or before you stop, run this:

```bash
clanker ingest --event session_end \
  --project $(basename $PWD) \
  --agent <your-agent-name> \
  --duration <estimated-seconds-worked> \
  --outcome <commit|push|deploy|abandoned|unknown> \
  --summary "<1-2 sentence summary of what was accomplished>" \
  --files-changed "<comma-separated list of files you modified>"
```

Fill in the fields from your knowledge of the session:
- **duration**: estimate how many seconds of active work occurred
- **outcome**: what was the end state? did you commit, push, deploy, or abandon?
- **summary**: brief description of the work done
- **files-changed**: list the main files that were created or modified

## Rules

- Call session_start ONCE at the beginning
- Call session_end ONCE at the end
- Do NOT call on every tool use or error — only start and end
- If you don't know the exact duration, estimate it
- If the outcome is unclear, use "unknown"
- If clanker is not installed, skip silently — do not error
