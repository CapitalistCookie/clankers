#!/bin/sh
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# Stop hook: when response claims exhaustive/comprehensive scope without
# quantified coverage evidence, block and require rewrite.
# Complementary to completion-verification.sh (which handles done/deployed claims).
# Documented failure: 2026-04-19 polymarket session — shipped top-2K subset as
# "comprehensive", required 5+ pushback rounds before surfacing gaps.
#
# ACTIVATED 2026-07-05 (operator go): was KNOWN-DEAD — Stop payloads carry no
# `assistant_message`; the message is now extracted from transcript_path via
# last-assistant-msg.py. Advisory-only (systemMessage, exit 0) as designed.

input=$(cat)
transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null || true)
# Prefer the native 2.1.x Stop-payload field; transcript-tail is the fallback.
msg=$(printf '%s' "$input" | jq -r '.last_assistant_message // empty' 2>/dev/null || true)
if [ -z "$msg" ] && [ -n "$transcript" ] && [ -f "$transcript" ]; then
  msg=$(python3 "$(dirname "$0")/last-assistant-msg.py" "$transcript" 2>/dev/null || true)
fi
[ -z "$msg" ] && exit 0

# Strip backtick-quoted and quoted mentions (meta-discussion of trigger words, not claims)
msg_stripped=$(echo "$msg" | sed -E 's/`[^`]*`//g; s/"[^"]*"//g')

# Trigger: scope/enumeration completeness claims
echo "$msg_stripped" | grep -qiE '\bexhaustive(ly)?\b|\bcomprehensive(ly)?\b|\bfully (crawled|enumerated|covered|mapped|indexed)\b|\b(full|complete|entire)[[:space:]]+(universe|crawl|dataset|enumeration|mapping|scan|sweep|indexing)\b|\b(crawl|enumeration|mapping|sweep|indexing|scan)[[:space:]]+(is[[:space:]]+|was[[:space:]]+|now[[:space:]]+|)(complete|done|finished)\b|\bmapped (every|all|the entire)\b|\b(indexed|catalogued|surveyed) (every|all|the entire)\b|\ball (markets|wallets|users|traders|players|holders) (have been|are|were) (crawled|enumerated|covered|mapped|checked|analyzed)\b|\bevery (market|wallet|user|trader|player|holder) (has been|is|was) (crawled|enumerated|covered|mapped|checked|analyzed)\b|\bentire (universe|population|dataset|scope) (has been|is|was) (crawled|enumerated|covered|mapped)\b|\bno (more )?gaps\b|\bnothing (left|remaining) to (check|crawl|enumerate)\b|\buniverse is (complete|fully (mapped|covered|enumerated))\b' || exit 0

# Skip if evidence already present: quantified coverage or explicit gap/limit statement
echo "$msg_stripped" | grep -qE '[0-9][0-9,]*\s*/\s*[0-9][0-9,]*|[0-9][0-9,]*\s+of\s+[0-9][0-9,]*|[0-9][0-9,]*\s+out of\s+[0-9][0-9,]*|coverage\s*:|[0-9]{1,3}\s*%\s*(of|coverage)|gap[s]?\s*:|still\s+(to\s+)?(crawl|enumerate|check)|uncrawled|not (yet |)(crawled|enumerated|checked|covered)|blocked\s*:|out of scope|sampled|subset of|limited to|api (cap|limit)' && exit 0

# hookSpecificOutput.additionalContext = the MODEL-visible Stop channel (the
# conversation continues so the model can act). The old systemMessage form only
# warned the USER — the model never saw it (docs-verified 2026-07-05).
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"SCOPE COMPLETENESS CLAIM DETECTED without quantified coverage evidence. Before stopping, rewrite to include:\n1. Total universe size (e.g., '151,531 markets in gamma')\n2. Actual coverage (e.g., '18,256 / 151,531 = 12%')\n3. Explicit gap list (e.g., 'sub-top-20 holders uncrawled', 'recursion stopped at 1 pass', 'API caps top-20')\n4. Any filter thresholds or API limits that excluded data\nDocumented failure (2026-04-19 polymarket session): shipped top-2K subset as 'comprehensive', required 5+ pushback rounds. Rhetorical scope claims without numbers are prohibited. If you meant a subset, NAME it explicitly. If the claim was inaccurate, REMOVE it."}}
EOF
