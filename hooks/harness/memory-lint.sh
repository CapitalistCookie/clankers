#!/usr/bin/env bash
# distribution source: synced to ~/.claude/hooks by clanker sync (do not edit the installed copy)
# memory-lint v3 (2026-07-05, harness overhaul — "bulletproof for dumber models").
# Fail-closed guard on EVERY write under a */memory/ dir. PostToolUse(Edit|Write);
# exit 2 feeds the violation back to the model with the exact fix.
#
# Rules:
#   MEMORY.md   — pointer INDEX law: ≤250 chars/line, ≤16KB file, and every
#                 markdown link target (x.md) must EXIST (no dead pointers).
#   any *.md    — (a) frontmatter with name: + non-empty description: REQUIRED
#                 (unroutable memories are how the 205-orphan pile formed),
#                 (b) frontmatter name: MUST equal the filename (recall routes by
#                 name — a mismatch makes the memory unfindable), (c) metadata
#                 type: REQUIRED (user|feedback|project|reference), (d) NO
#                 plaintext secrets (age store is law), (e) >64KB = advisory
#                 smell (session logs belong in repo docs/), non-blocking.
#   after any pass — regenerate INDEX_ALL.md (full inventory + ORPHANS section)
#                 so discovery never goes stale.
# Extra modes:
#   --bash-guard   PreToolUse(Bash) mode: BLOCK shell redirection/in-place writes
#                  into memory dirs (bypasses this lint) — weak models must use
#                  Write/Edit so the lint actually runs.
#   --regen <dir>  regenerate INDEX_ALL.md; prints "orphans=N" (session-start
#                  self-heal uses this).
#   --doctor <dir> lint EVERY file in a memory dir; non-zero exit on violations.
# Selftest: bash memory-lint.sh --selftest  (run after ANY edit here).
set -uo pipefail

# SECRET_RE is fragment-assembled so this file never matches its own patterns
# (adopt's contract secret-scan and gitleaks both walk repo .sh files — the
# same self-match rule publint uses).
SECRET_RE='glpat'
SECRET_RE+='-[A-Za-z0-9._-]{15,}|cfut'
SECRET_RE+='_[A-Za-z0-9]{30,}|db'
SECRET_RE+='-[A-Za-z0-9]{20,}|AK'
SECRET_RE+='IA[0-9A-Z]{16}|PGPASS'
SECRET_RE+='WORD=[^$][^)]|-----BEGIN [A-Z ]*PRIVAT'
SECRET_RE+='E KEY'

lint_file() {  # $1 = file path; prints violations to stderr, returns 2 on block
  local FILE="$1" DIR BASE
  BASE=$(basename "$FILE")
  DIR=$(dirname "$FILE")

  # Index law applies to the router AND its domain shards (*-POINTERS.md,
  # sharded-router design 2026-07-19): each is a pointer index with its OWN
  # 16KB/250-char budget, so the fleet's pointer corpus scales by adding shards
  # instead of ramming one flat file into the ceiling again.
  case "$BASE" in MEMORY.md|*-POINTERS.md)
    local SIZE MAXLEN FAT DEAD
    SIZE=$(wc -c < "$FILE")
    MAXLEN=$(awk '{ if (length > m) m = length } END { print m+0 }' "$FILE")
    if [ "$SIZE" -gt 16384 ] || [ "$MAXLEN" -gt 250 ]; then
      FAT=$(awk 'length > 250 { print NR": "substr($0,1,80) }' "$FILE" | head -5)
      {
        echo "memory-lint VIOLATION on $FILE — index files (MEMORY.md / *-POINTERS.md) are pointer INDEXES (law: ≤250 chars/line, ≤16KB/file)."
        echo "  size=${SIZE}B (max 16384), longest line=${MAXLEN} chars (max 250)."
        [ -n "$FAT" ] && echo "  offending lines:" && echo "$FAT" | sed 's/^/    /'
        echo "  FIX NOW: move the content VERBATIM into the entry's topic file (memory/<slug>.md) and leave a one-line pointer here — or split this shard by subdomain (NEW-DOMAIN-POINTERS.md + a routing line in MEMORY.md)."
      } >&2
      return 2
    fi
    DEAD=$(grep -oE '\]\([A-Za-z0-9_./-]+\.md\)' "$FILE" | tr -d '])(' | sort -u \
      | while read -r f; do [ -f "$DIR/$f" ] || echo "$f"; done)
    if [ -n "$DEAD" ]; then
      {
        echo "memory-lint VIOLATION on $FILE — DEAD POINTERS (index links to files that do not exist):"
        echo "$DEAD" | sed 's/^/    /'
        echo "  FIX NOW: create the topic file first (with frontmatter), or correct the link."
      } >&2
      return 2
    fi
    # Warn band (non-blocking): headroom alarms beat cliff edges — the router
    # sat at 99.8% for days with nothing saying so before the 2026-07-19 audit.
    if [ "$SIZE" -gt 13107 ]; then
      echo "memory-lint note: $FILE at $((SIZE * 100 / 16384))% of its 16KB index budget — plan the next shard split BEFORE the wall (law header in MEMORY.md)." >&2
    fi
    return 0
    ;;
  esac

  # generated files — no frontmatter/topic rules (regenerated, never hand-edited)
  case "$BASE" in INDEX_ALL.md|CODEBASE_INDEX.md|ROUTER-AUTO.md) return 0 ;; esac
  if ! head -12 "$FILE" | grep -qE '^name:[[:space:]]*[^[:space:]]' \
     || ! head -12 "$FILE" | grep -qE '^description:[[:space:]]*[^[:space:]]' \
     || ! head -12 "$FILE" | grep -qE '^[[:space:]]*type:[[:space:]]*(user|feedback|project|reference)'; then
    {
      echo "memory-lint VIOLATION on $FILE — memory files REQUIRE complete frontmatter (this is how recall routes; incomplete files become unfindable orphans):"
      echo '    ---'
      echo "    name: $(basename "$FILE" .md)"
      echo '    description: <one line — used to decide relevance during recall>'
      echo '    metadata:'
      echo '      type: user | feedback | project | reference   # pick ONE'
      echo '    ---'
      echo "  FIX NOW: add/complete the frontmatter block above (non-empty description, a valid type)."
    } >&2
    return 2
  fi
  local FM_NAME
  FM_NAME=$(head -12 "$FILE" | sed -n 's/^name:[[:space:]]*//p' | head -1 | tr -d '"' | tr -d "'")
  if [ -n "$FM_NAME" ] && [ "$FM_NAME" != "$(basename "$FILE" .md)" ]; then
    {
      echo "memory-lint VIOLATION on $FILE — frontmatter name '$FM_NAME' != filename '$(basename "$FILE" .md)'."
      echo "  Recall routes by name; a mismatch orphans the memory. FIX NOW: set name: $(basename "$FILE" .md) (or rename the file to $FM_NAME.md and fix its index pointer)."
    } >&2
    return 2
  fi
  if grep -qE "$SECRET_RE" "$FILE"; then
    {
      echo "memory-lint VIOLATION on $FILE — PLAINTEXT SECRET pattern detected."
      echo "  Law: secrets live in the age store. FIX NOW: printf '%s' '<value>' | secret set <name>, then replace with \$(secret get <name>)."
    } >&2
    return 2
  fi
  if [ "$(wc -c < "$FILE")" -gt 65536 ]; then
    echo "memory-lint note: $FILE is >64KB — session-log-sized. Memories are facts+pointers; full logs belong in the project repo's docs/." >&2
  fi
  return 0
}

regen_index() {  # $1 = memory dir; $2 = "print" to emit "orphans=N"
  python3 - "$1" "${2:-}" <<'PY' 2>/dev/null || true
import os, re, sys
MEM = sys.argv[1]
# The link corpus is the router PLUS its domain shards (sharded-router design
# 2026-07-19): a file pointed to from RESEARCH-POINTERS.md is reachable, not
# an orphan.
INDEX_FILES = ["MEMORY.md"] + sorted(
    f for f in os.listdir(MEM) if f.endswith("-POINTERS.md"))
mem_txt = ""
for idx in INDEX_FILES:
    try:
        mem_txt += open(os.path.join(MEM, idx), errors="ignore").read()
    except Exception:
        pass
rows, orphans, nofm = [], [], []
for fn in sorted(os.listdir(MEM)):
    if (not fn.endswith(".md")
            or fn in ("MEMORY.md", "INDEX_ALL.md", "ROUTER-AUTO.md")
            or fn.endswith("-POINTERS.md")):
        continue
    head = open(os.path.join(MEM, fn), errors="ignore").read(2000)
    m = re.search(r"^description:\s*(.+)$", head, re.M)
    desc = m.group(1).strip().strip('"') if m else ""
    if not m:
        nofm.append(fn)
        m2 = re.search(r"^#\s*(.+)$", head, re.M)
        desc = m2.group(1).strip() if m2 else (head.strip().splitlines() or [""])[0]
    rows.append((fn, desc[:180]))
    if fn not in mem_txt and fn[:-3] not in mem_txt:
        orphans.append(fn)
with open(os.path.join(MEM, "INDEX_ALL.md"), "w") as f:
    f.write("# INDEX_ALL — every memory file (auto-regenerated by memory-lint on each memory write)\n\n")
    f.write("To find anything: grep this file. Orphans below are unreachable from MEMORY.md and every *-POINTERS.md shard — add a pointer line or fold them into a topic file.\n\n")
    for fn, desc in rows:
        f.write(f"- [{fn}]({fn}) — {desc}\n")
    if orphans:
        f.write(f"\n## ORPHANS (not referenced from any router index — {len(orphans)})\n\n")
        for fn in orphans:
            f.write(f"- {fn}\n")
    if nofm:
        f.write(f"\n## MISSING FRONTMATTER (pre-law files — add name:+description: on next touch — {len(nofm)})\n\n")
        for fn in nofm:
            f.write(f"- {fn}\n")
if len(sys.argv) > 2 and sys.argv[2] == "print":
    print(f"orphans={len(orphans)}")
PY
}

# ── bash-guard: block shell writes into memory dirs (they bypass this lint) ──
# 2026-07-19 rewrite: operators anchored DIRECTLY to the memory path. The old
# pattern let [^|;]* bridge any earlier '>' to the path, so read-only commands
# like `awk 'length>250' <memfile>` or `grep 'a>b' <memfile>` were blocked,
# while interpreter writes (python open(...,'a')) sailed through.
# Same namespace scoping as the hook entry (2026-07-11): only ~/.claude/projects/<ns>/memory/,
# so shell writes to repo files under a memory/ dir (e.g. .specify/memory/) are not blocked.
bash_guard() {
  local INPUT
  INPUT=$(cat)
  MEMGUARD_INPUT="$INPUT" python3 - <<'PY'
import json, os, re, sys
try:
    cmd = json.loads(os.environ.get("MEMGUARD_INPUT", "{}")).get("tool_input", {}).get("command", "") or ""
except Exception:
    sys.exit(0)
if not cmd:
    sys.exit(0)
# a single shell token that is a memory file, optional surrounding quote
P = r'[\'"]?[^|;&\s\'"]*/\.claude/projects/[^/]+/memory/[^|;&\s\'"]*\.md[\'"]?'
WRITES = [
    r'>>?\s*' + P,                                   # redirect straight into a memory file
    r'\btee\s+(-a\s+)?' + P,                         # tee / tee -a <memfile>
    r'\bsed\s+([^|;&]*\s)?-i[^\s]*\s[^|;&]*' + P,    # sed -i ... <memfile>
    r'\b(mv|cp)\s+[^|;&]*\s' + P + r'\s*([;|&]|$)',  # mv/cp with <memfile> as DESTINATION
    # inline interpreter write: open('<memfile>','w'|'a'|...)
    r'\bopen\(\s*[\'"][^\'"]*/\.claude/projects/[^/]+/memory/[^\'"]*\.md[\'"]\s*,\s*[\'"][wa]',
]
if any(re.search(rx, cmd) for rx in WRITES):
    sys.stderr.write(
        "memory-guard BLOCK: shell writes into a memory dir bypass memory-lint (frontmatter/index/secret checks never run).\n"
        "  Use the Write or Edit tool on the memory file instead — the lint hook runs there and will guide you.\n"
        "  (Reads — cat/grep/awk/cp-FROM — are fine. Blocked: >, >>, tee, sed -i, mv/cp INTO */memory/*.md,\n"
        "   and inline open(...,'w'/'a'). Other interpreter writes can still slip past the guard — use Write/Edit.)\n"
    )
    sys.exit(2)
sys.exit(0)
PY
  exit $?
}

# ── mode dispatch ─────────────────────────────────────────────────────────────
if [ "${1:-}" = "--bash-guard" ]; then
  bash_guard
fi
if [ "${1:-}" = "--regen" ]; then
  [ -d "${2:-}" ] || { echo "usage: memory-lint.sh --regen <memory-dir>" >&2; exit 1; }
  regen_index "$2" print
  exit 0
fi
if [ "${1:-}" = "--doctor" ]; then
  DIR="${2:-$HOME/.claude/projects/$(printf '%s' "$HOME" | tr '/' '-')/memory}"
  [ -d "$DIR" ] || { echo "no such memory dir: $DIR" >&2; exit 1; }
  fails=0
  for f in "$DIR"/*.md; do
    [ -f "$f" ] || continue
    lint_file "$f" || fails=$((fails+1))
  done
  regen_index "$DIR" print
  if [ "$fails" -gt 0 ]; then
    echo "memory-doctor: $fails file(s) FAILING the lint (violations above)" >&2
    exit 2
  fi
  echo "memory-doctor: all files pass"
  exit 0
fi

# ── selftest ──────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--selftest" ]; then
  T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
  mkdir -p "$T/memory"
  fails=0
  FM='---\nname: %s\ndescription: ok\nmetadata:\n  type: project\n---\nbody\n'
  # 1 fat MEMORY.md -> 2
  python3 -c "open('$T/memory/MEMORY.md','w').write('x'*400+'\n')"
  lint_file "$T/memory/MEMORY.md" >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL fat-index"; fails=1; }
  # 2 dead pointer -> 2
  printf -- '- [ghost](ghost.md) — nope\n' > "$T/memory/MEMORY.md"
  lint_file "$T/memory/MEMORY.md" >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL dead-pointer"; fails=1; }
  # 3 good index -> 0
  printf -- '- [real](real.md) — ok\n' > "$T/memory/MEMORY.md"
  printf -- "$FM" real > "$T/memory/real.md"
  lint_file "$T/memory/MEMORY.md" >/dev/null 2>&1 && true; [ $? -eq 0 ] || { echo "FAIL good-index"; fails=1; }
  # 4 topic missing frontmatter -> 2
  printf 'just text\n' > "$T/memory/bare.md"
  lint_file "$T/memory/bare.md" >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL no-frontmatter"; fails=1; }
  # 5 topic with secret -> 2 (fixture token assembled so this file never
  # contains a secret-shaped literal itself)
  FAKE_TOKEN="glpat"$(printf -- '-%.0s' 1)$(printf 'A%.0s' {1..20})
  printf -- '---\nname: s\ndescription: d\nmetadata:\n  type: reference\n---\nkey %s\n' "$FAKE_TOKEN" > "$T/memory/s.md"
  lint_file "$T/memory/s.md" >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL secret"; fails=1; }
  # 6 good topic -> 0 + regen produces INDEX_ALL with ORPHANS section + count
  printf -- "$FM" orphan > "$T/memory/orphan.md"
  lint_file "$T/memory/real.md" >/dev/null 2>&1 || { echo "FAIL good-topic"; fails=1; }
  regen_index "$T/memory" print | grep -qE 'orphans=[1-9]' || { echo "FAIL orphan-count"; fails=1; }
  grep -q 'ORPHANS' "$T/memory/INDEX_ALL.md" && grep -q 'orphan.md' "$T/memory/INDEX_ALL.md" \
    || { echo "FAIL index-regen/orphans"; fails=1; }
  # 7 missing metadata.type -> 2
  printf -- '---\nname: notype\ndescription: d\n---\nbody\n' > "$T/memory/notype.md"
  lint_file "$T/memory/notype.md" >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL missing-type"; fails=1; }
  # 8 frontmatter name != filename -> 2
  printf -- '---\nname: other-slug\ndescription: d\nmetadata:\n  type: project\n---\nbody\n' > "$T/memory/mismatch.md"
  lint_file "$T/memory/mismatch.md" >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL name-mismatch"; fails=1; }
  # 9 bash-guard trips on shell write into memory -> 2. Fixture paths use a
  # placeholder $HOME + generic namespace (kept literal inside the single-quoted
  # JSON): the guard only regex-matches */.claude/projects/<ns>/memory/*.md, so the
  # exact home and namespace slug are irrelevant to what these fixtures exercise.
  printf '{"tool_input":{"command":"echo hi >> $HOME/.claude/projects/-ns/memory/MEMORY.md"}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL bash-guard-trip"; fails=1; }
  printf '{"tool_input":{"command":"sed -i s/a/b/ $HOME/.claude/projects/-x/memory/foo.md"}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL bash-guard-sed"; fails=1; }
  # 9b quoted redirect, cp INTO memory, inline open(...,'a') -> all 2
  printf '{"tool_input":{"command":"echo x > \\"$HOME/.claude/projects/-x/memory/foo.md\\""}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL bash-guard-quoted-redirect"; fails=1; }
  printf '{"tool_input":{"command":"cp /tmp/foo.md $HOME/.claude/projects/-x/memory/foo.md"}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL bash-guard-cp-into"; fails=1; }
  printf '{"tool_input":{"command":"python3 -c \\"open('"'"'$HOME/.claude/projects/-x/memory/foo.md'"'"','"'"'a'"'"').write('"'"'x'"'"')\\""}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL bash-guard-py-open"; fails=1; }
  # 10 bash-guard passes reads + unrelated writes -> 0
  printf '{"tool_input":{"command":"grep -r vwap $HOME/.claude/projects/-ns/memory/ | head"}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 0 ] || { echo "FAIL bash-guard-read"; fails=1; }
  printf '{"tool_input":{"command":"echo log >> /tmp/build.log"}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 0 ] || { echo "FAIL bash-guard-unrelated"; fails=1; }
  # 10b reads with > in the pattern + cp FROM memory -> 0 (the 2026-07-19 false positives)
  printf '{"tool_input":{"command":"awk '"'"'length>250'"'"' $HOME/.claude/projects/-ns/memory/MEMORY.md"}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 0 ] || { echo "FAIL bash-guard-awk-read"; fails=1; }
  printf '{"tool_input":{"command":"grep -c '"'"'a>b'"'"' $HOME/.claude/projects/-x/memory/foo.md"}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 0 ] || { echo "FAIL bash-guard-grep-gt"; fails=1; }
  printf '{"tool_input":{"command":"cp $HOME/.claude/projects/-x/memory/foo.md /tmp/foo.md"}}' \
    | bash "$0" --bash-guard >/dev/null 2>&1; [ $? -eq 0 ] || { echo "FAIL bash-guard-cp-from"; fails=1; }
  # 10c sharded-router law (2026-07-19): shard over line-limit blocks; a file
  # linked ONLY from a shard is NOT an orphan; >80% index budget notes to stderr
  python3 -c "open('$T/memory/X-POINTERS.md','w').write('y'*400+'\n')"
  lint_file "$T/memory/X-POINTERS.md" >/dev/null 2>&1; [ $? -eq 2 ] || { echo "FAIL shard-fat"; fails=1; }
  printf -- '- [orphan](orphan.md) — routed via shard\n' > "$T/memory/X-POINTERS.md"
  regen_index "$T/memory" print >/dev/null
  if awk '/## ORPHANS/,0' "$T/memory/INDEX_ALL.md" | grep -q 'orphan\.md'; then
    echo "FAIL shard-link-orphan"; fails=1
  fi
  python3 -c "open('$T/memory/Y-POINTERS.md','w').write(('x'*200+'\n')*70)"
  lint_file "$T/memory/Y-POINTERS.md" 2>"$T/warn.txt"; rc=$?
  { [ $rc -eq 0 ] && grep -q 'index budget' "$T/warn.txt"; } || { echo "FAIL warn-band"; fails=1; }
  rm -f "$T/memory/Y-POINTERS.md"
  # 11 doctor mode: clean tmp dir passes after removing planted violations
  rm -f "$T/memory/bare.md" "$T/memory/s.md" "$T/memory/notype.md" "$T/memory/mismatch.md"
  printf -- '- [real](real.md) — ok\n- [orphan](orphan.md) — now linked\n' > "$T/memory/MEMORY.md"
  bash "$0" --doctor "$T/memory" >/dev/null 2>&1; [ $? -eq 0 ] || { echo "FAIL doctor-clean"; fails=1; }
  [ $fails -eq 0 ] && echo "memory-lint selftest: 20/20 PASS" && exit 0
  echo "memory-lint selftest: FAILURES"; exit 1
fi

# ── hook entry ────────────────────────────────────────────────────────────────
INPUT=$(cat)
# Pure-bash prefilter (S6): PostToolUse(Edit|Write) fires on EVERY file write, but a
# memory write's payload must contain the substring "/memory/" (the precise namespace
# case below is a subset of this). If it can't, skip the python3 file_path parse
# entirely — the common non-memory write then pays zero extra process spawns.
case "$INPUT" in
  *"/memory/"*) ;;
  *) exit 0 ;;
esac
FILE=$(printf '%s' "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")
# Scope: CLAUDE memory namespaces ONLY (~/.claude/projects/<ns>/memory/). A bare */memory/* match
# false-positived on repo files that happen to live under a memory/ dir — e.g. spec-kit's
# .specify/memory/constitution.md (caught live 2026-07-11, constructionmanagement harness audit).
case "$FILE" in
  */.claude/projects/*/memory/*.md) ;;
  *) exit 0 ;;
esac
[ -f "$FILE" ] || exit 0

lint_file "$FILE"
rc=$?
[ $rc -eq 0 ] && regen_index "$(dirname "$FILE")"
exit $rc
