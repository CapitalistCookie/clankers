"""Command-safety + governance classifiers — ported from ECC's PreToolUse hooks.

Pure stdlib (only `re`). A faithful Python port of the quote-aware shell parsing
and pattern lists that ECC ships as Node hooks, exposing them as plain functions
clanker can call in-process.

Provenance (affaan-m/ECC, read verbatim during the port):
  - scripts/hooks/gateguard-fact-force.js   destructive git/rm + subshell evasion
  - scripts/hooks/governance-capture.js     secret / approval / sensitive-path lists
  - scripts/hooks/block-no-verify.js         flag-position-aware --no-verify tokenizer
  - scripts/lib/shell-split.js               operator-aware segment splitting
  - scripts/lib/shell-substitution.js        nested $()/``/()/{ ;} body extraction

Two intentional deviations from the JS, both documented inline at the call site:
  1. `classify_command` is a *superset* of ECC's `isDestructiveBash` gate. ECC folds
     dd/shred/kubectl/`rm -f`/SQL into its risk scorer (risk.py here); the task asks
     this classifier to surface them as explicit categories, so they are detected
     token-wise in addition to ECC's narrow git/rm/SQL-regex set.
  2. ECC's SQL/dd regex (word-boundary + dd-if=) has a latent no-match: in the
     dd alternative the trailing word-boundary never fires because `=` and the
     following `/` are both non-word characters. We keep that regex verbatim for
     `destructive` parity but add a dedicated token check so `dd if=...` is
     actually categorized.

Public API:
    split_segments(cmd) -> list[str]
    extract_subshells(cmd) -> list[str]
    all_command_bodies(cmd) -> list[str]
    classify_command(cmd) -> {"destructive", "categories", "reasons"}
    blocks_no_verify(cmd) -> bool
    detect_governance(text) -> list[dict]
"""

import re

# ─── Pattern lists (ported from governance-capture.js) ───────────────────────
# SECRET_PATTERNS: order preserved from the JS array. `name` keys match the JS.
_SECRET_PATTERNS = [
    ("aws_key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}", re.IGNORECASE)),
    ("generic_secret",
     re.compile(r"""(?:secret|password|token|api[_-]?key)\s*[:=]\s*["'][^"']{8,}""",
                re.IGNORECASE)),
    ("private_key",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----")),
    ("jwt",
     re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}")),
]

# APPROVAL_COMMANDS: governance-relevant operations that warrant explicit sign-off.
_APPROVAL_PATTERNS = [
    re.compile(r"git\s+push\s+.*--force"),
    re.compile(r"git\s+reset\s+--hard"),
    re.compile(r"rm\s+-rf?\s"),
    re.compile(r"DROP\s+(?:TABLE|DATABASE)", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+\w+\s*(?:;|$)", re.IGNORECASE),
]

# SENSITIVE_PATHS: file patterns that indicate a policy-sensitive target.
_SENSITIVE_PATH_PATTERNS = [
    re.compile(r"\.env(?:\.|$)"),
    re.compile(r"credentials", re.IGNORECASE),
    re.compile(r"secrets?\.", re.IGNORECASE),
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"id_rsa"),
]

# SQL/dd phrases (gateguard-fact-force.js DESTRUCTIVE_SQL_DD). Matched against
# quote-stripped text, same as ECC. The trailing \b on dd is ECC's (see module
# docstring deviation #2); a dedicated dd token check below covers the real case.
_DESTRUCTIVE_SQL_DD = re.compile(
    r"\b(drop\s+table|delete\s+from|truncate|dd\s+if=)\b", re.IGNORECASE)


# ─── Quote-aware segment splitting (port of shell-split.js) ───────────────────
def split_segments(cmd):
    """Split a command line into top-level segments at unquoted `;`, `|`, `&`,
    `&&`, `||`.

    Quoting (single/double) and backslash escapes are respected. Redirection
    operators (`&>`, `>&`, `2>&1`) are NOT treated as separators — matching
    bash and ECC's `splitShellSegments`. Segments are trimmed; empties dropped.
    """
    command = cmd if isinstance(cmd, str) else str(cmd or "")
    segments = []
    current = ""
    quote = None
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]

        # Inside quotes: handle escapes and the closing quote.
        if quote:
            if ch == "\\" and i + 1 < n:
                current += ch + command[i + 1]
                i += 2
                continue
            if ch == quote:
                quote = None
            current += ch
            i += 1
            continue

        # Backslash escape outside quotes.
        if ch == "\\" and i + 1 < n:
            current += ch + command[i + 1]
            i += 2
            continue

        # Opening quote.
        if ch == '"' or ch == "'":
            quote = ch
            current += ch
            i += 1
            continue

        nxt = command[i + 1] if i + 1 < n else ""
        prev = command[i - 1] if i > 0 else ""

        # && operator.
        if ch == "&" and nxt == "&":
            if current.strip():
                segments.append(current.strip())
            current = ""
            i += 2
            continue

        # || operator.
        if ch == "|" and nxt == "|":
            if current.strip():
                segments.append(current.strip())
            current = ""
            i += 2
            continue

        # Single | pipe.
        if ch == "|":
            if current.strip():
                segments.append(current.strip())
            current = ""
            i += 1
            continue

        # ; separator.
        if ch == ";":
            if current.strip():
                segments.append(current.strip())
            current = ""
            i += 1
            continue

        # Single & — but skip redirection patterns (&>, >&, digit>&).
        if ch == "&" and nxt != "&":
            if nxt == ">" or prev == ">":
                current += ch
                i += 1
                continue
            if current.strip():
                segments.append(current.strip())
            current = ""
            i += 1
            continue

        current += ch
        i += 1

    if current.strip():
        segments.append(current.strip())
    return segments


# ─── Nested-subshell extraction (port of shell-substitution.js) ──────────────
def _extract_command_substitutions(text):
    """Bodies of `$(...)` and backtick command substitutions, recursively.

    Single quotes are literal (substitutions inside them are ignored); double
    quotes still permit substitutions, so those bodies are scanned before quoted
    text would be stripped. Port of `extractCommandSubstitutions`.
    """
    source = text if isinstance(text, str) else str(text or "")
    out = []
    in_single = False
    in_double = False
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]
        prev = source[i - 1] if i > 0 else ""

        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double and prev != "\\":
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single and prev != "\\":
            in_double = not in_double
            i += 1
            continue
        if in_single:
            i += 1
            continue

        # Backtick substitution.
        if ch == "`":
            body = ""
            i += 1
            while i < n:
                inner = source[i]
                if inner == "\\":
                    body += inner
                    if i + 1 < n:
                        body += source[i + 1]
                        i += 2
                        continue
                if inner == "`":
                    break
                body += inner
                i += 1
            if body.strip():
                out.append(body)
                out.extend(_extract_command_substitutions(body))
            i += 1
            continue

        # $(...) substitution.
        if ch == "$" and i + 1 < n and source[i + 1] == "(":
            depth = 1
            body = ""
            b_single = False
            b_double = False
            i += 2
            while i < n and depth > 0:
                inner = source[i]
                inner_prev = source[i - 1] if i > 0 else ""
                if inner == "\\" and not b_single:
                    body += inner
                    if i + 1 < n:
                        body += source[i + 1]
                        i += 2
                        continue
                if inner == "'" and not b_double and inner_prev != "\\":
                    b_single = not b_single
                elif inner == '"' and not b_single and inner_prev != "\\":
                    b_double = not b_double
                elif not b_single and not b_double:
                    if inner == "(":
                        depth += 1
                    elif inner == ")":
                        depth -= 1
                        if depth == 0:
                            break
                body += inner
                i += 1
            if body.strip():
                out.append(body)
                out.extend(_extract_command_substitutions(body))
            i += 1
            continue

        i += 1

    return out


def _skip_balanced(source, i, opener, closer):
    """Advance past a balanced `opener`/`closer` span (quote-aware), starting at
    the opener index `i`; return the index just after the matching closer."""
    n = len(source)
    depth = 1
    in_single = False
    in_double = False
    i += 1
    while i < n and depth > 0:
        inner = source[i]
        inner_prev = source[i - 1] if i > 0 else ""
        if inner == "\\" and not in_single:
            i += 2
            continue
        if inner == "'" and not in_double and inner_prev != "\\":
            in_single = not in_single
        elif inner == '"' and not in_single and inner_prev != "\\":
            in_double = not in_double
        elif not in_single and not in_double:
            if inner == opener:
                depth += 1
            elif inner == closer:
                depth -= 1
        i += 1
    return i


def _extract_subshell_groups(text):
    """Bodies of plain `(...)` subshell groups, recursively.

    Skips `$(...)`, backticks (covered by the substitution extractor) and quoted
    spans. Port of `extractSubshellGroups`. Bash treats `(cmd)` as a forked
    subshell that executes its contents.
    """
    source = text if isinstance(text, str) else str(text or "")
    groups = []
    in_single = False
    in_double = False
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]
        prev = source[i - 1] if i > 0 else ""

        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double and prev != "\\":
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single and prev != "\\":
            in_double = not in_double
            i += 1
            continue
        if in_single or in_double:
            i += 1
            continue

        # Skip $(...) — handled by the substitution extractor.
        if ch == "$" and i + 1 < n and source[i + 1] == "(":
            i = _skip_balanced(source, i + 1, "(", ")")
            continue

        # Skip backtick spans.
        if ch == "`":
            i += 1
            while i < n and source[i] != "`":
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += 1
            continue

        # Plain (...) subshell.
        if ch == "(":
            depth = 1
            body = ""
            b_single = False
            b_double = False
            i += 1
            while i < n and depth > 0:
                inner = source[i]
                inner_prev = source[i - 1] if i > 0 else ""
                if inner == "\\" and not b_single:
                    body += inner
                    if i + 1 < n:
                        body += source[i + 1]
                        i += 2
                        continue
                if inner == "'" and not b_double and inner_prev != "\\":
                    b_single = not b_single
                elif inner == '"' and not b_single and inner_prev != "\\":
                    b_double = not b_double
                elif not b_single and not b_double:
                    if inner == "(":
                        depth += 1
                    elif inner == ")":
                        depth -= 1
                        if depth == 0:
                            break
                body += inner
                i += 1
            if body.strip():
                groups.append(body)
                groups.extend(_extract_subshell_groups(body))
            i += 1
            continue

        i += 1

    return groups


def _extract_brace_groups(text):
    """Bodies of `{ ...; }` brace groups, recursively.

    A `{` opens a group only when followed by whitespace and preceded by a
    boundary (start / whitespace / `;` `|` `&` `(`); a `}` closes only when
    preceded by `;` or whitespace — bash's reserved-word rules. `$(...)`,
    backticks and `(...)` spans inside the body are skipped so their contents
    are not double-extracted here. Port of `extractBraceGroups`.
    """
    source = text if isinstance(text, str) else str(text or "")
    groups = []
    in_single = False
    in_double = False
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]
        prev = source[i - 1] if i > 0 else ""

        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double and prev != "\\":
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single and prev != "\\":
            in_double = not in_double
            i += 1
            continue
        if in_single or in_double:
            i += 1
            continue

        # Skip $(...) and backtick and (...) spans at the scan level.
        if ch == "$" and i + 1 < n and source[i + 1] == "(":
            i = _skip_balanced(source, i + 1, "(", ")")
            continue
        if ch == "`":
            i += 1
            while i < n and source[i] != "`":
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if ch == "(":
            i = _skip_balanced(source, i, "(", ")")
            continue

        # Brace group: `{` + whitespace, preceded by a boundary.
        if ch == "{" and i + 1 < n and source[i + 1].isspace():
            prev_is_boundary = (i == 0) or (prev in " \t\n;|&(")
            if not prev_is_boundary:
                i += 1
                continue

            depth = 1
            body = ""
            b_single = False
            b_double = False
            i += 1
            while i < n and depth > 0:
                inner = source[i]
                inner_prev = source[i - 1] if i > 0 else ""
                if inner == "\\" and not b_single:
                    body += inner
                    if i + 1 < n:
                        body += source[i + 1]
                        i += 2
                        continue
                if inner == "'" and not b_double and inner_prev != "\\":
                    b_single = not b_single
                    body += inner
                    i += 1
                    continue
                if inner == '"' and not b_single and inner_prev != "\\":
                    b_double = not b_double
                    body += inner
                    i += 1
                    continue
                if b_single or b_double:
                    body += inner
                    i += 1
                    continue
                # Skip nested $(...) spans (a `}` inside must not close us).
                if inner == "$" and i + 1 < n and source[i + 1] == "(":
                    start = i
                    i = _skip_balanced(source, i + 1, "(", ")")
                    body += source[start:i]
                    continue
                # Skip backtick spans.
                if inner == "`":
                    body += inner
                    i += 1
                    while i < n and source[i] != "`":
                        if source[i] == "\\" and i + 1 < n:
                            body += source[i] + source[i + 1]
                            i += 2
                            continue
                        body += source[i]
                        i += 1
                    if i < n:
                        body += source[i]
                        i += 1
                    continue
                # Skip plain (...) spans.
                if inner == "(":
                    start = i
                    i = _skip_balanced(source, i, "(", ")")
                    body += source[start:i]
                    continue
                if inner == "{" and i + 1 < n and source[i + 1].isspace():
                    if inner_prev in " \t\n;|&(":
                        depth += 1
                elif inner == "}" and (inner_prev == ";" or inner_prev.isspace()):
                    depth -= 1
                    if depth == 0:
                        break
                body += inner
                i += 1
            if body.strip():
                groups.append(body)
                groups.extend(_extract_brace_groups(body))
            i += 1
            continue

        i += 1

    return groups


def extract_subshells(cmd):
    """All executable bodies nested in `$(...)`, backticks, `(...)`, and
    `{ ...; }`, discovered recursively across every syntax.

    Mirrors ECC's `collectExecutableBodies` BFS minus the top-level `raw` entry:
    each harvested body is fed back through all three extractors so a `(...)`
    inside a `$(...)`, or a `{ ...; }` inside a `(...)`, is found too. Returns
    de-duplicated bodies (order: first-seen).
    """
    command = cmd if isinstance(cmd, str) else str(cmd or "")
    seen = set()
    ordered = []
    queue = [command]
    processed = set()

    while queue:
        current = queue.pop(0)
        if current in processed:
            continue
        processed.add(current)
        for extractor in (_extract_command_substitutions,
                          _extract_subshell_groups,
                          _extract_brace_groups):
            for body in extractor(current):
                if body not in seen:
                    seen.add(body)
                    ordered.append(body)
                if body not in processed:
                    queue.append(body)
    return ordered


def all_command_bodies(cmd):
    """The full surface to analyze: top-level segments of the raw command PLUS
    the segments of every nested subshell body.

    This is the flattened list `classify_command` scans so a destructive command
    hidden inside `$(...)`/backticks/`(...)`/`{ ;}` is caught. Port of ECC's
    `collectExecutableBodies(raw).flatMap(splitCommandSegments)`, with the raw
    command included as the first surface.
    """
    command = cmd if isinstance(cmd, str) else str(cmd or "")
    bodies = [command]
    bodies.extend(extract_subshells(command))
    segments = []
    for body in bodies:
        segments.extend(split_segments(body))
    return segments


# ─── Destructive-command classification (port of gateguard-fact-force.js) ─────
def _strip_quoted_strings(text):
    """Replace single/double-quoted string contents with empty quotes so phrases
    inside a `-m` message or echoed arg don't trip the SQL/dd regex. Port of
    `stripQuotedStrings`."""
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    return text


def _tokenize(segment):
    """Whitespace-tokenize a segment after quote contents are collapsed."""
    return [t for t in re.split(r"\s+", _strip_quoted_strings(segment)) if t]


def _command_basename(token):
    """Strip a leading path and trailing `.exe`, lowercase. Port of
    `commandBasename` (`/usr/bin/git`, `git.exe`, `GIT` -> `git`)."""
    if not token:
        return ""
    token = re.sub(r"^.*[\\/]", "", token)
    token = re.sub(r"\.exe$", "", token, flags=re.IGNORECASE)
    return token.lower()


def _is_destructive_rm(tokens):
    """`rm` with both recursive AND force flags (combined `-rf`/`-fr` or split
    `-r -f`, plus `--recursive`/`--force`). Port of `isDestructiveRm`."""
    if not tokens or _command_basename(tokens[0]) != "rm":
        return False
    has_r = False
    has_f = False
    for t in tokens[1:]:
        if t == "--recursive":
            has_r = True
            continue
        if t == "--force":
            has_f = True
            continue
        if not t.startswith("-") or t.startswith("--"):
            continue
        body = t[1:]
        if re.search(r"[rR]", body):
            has_r = True
        if "f" in body:
            has_f = True
    return has_r and has_f


def _is_rm_force(tokens):
    """`rm -f` (force, with or without recursive). Superset detail the task asks
    for as its own category — ECC scores this via risk.py rather than the gate."""
    if not tokens or _command_basename(tokens[0]) != "rm":
        return False
    for t in tokens[1:]:
        if t == "--force":
            return True
        if t.startswith("-") and not t.startswith("--") and "f" in t[1:]:
            return True
    return False


def _find_git_subcommand(tokens):
    """Locate the git subcommand, skipping global options (`-c k=v`, `-C path`,
    `--git-dir=`, …). Returns (command, rest) or None. Port of
    `findGitSubcommand`."""
    if not tokens or _command_basename(tokens[0]) != "git":
        return None
    value_short = {"-c", "-C"}
    value_long = {"--git-dir", "--work-tree", "--namespace", "--super-prefix"}
    i = 1
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t in value_short or t in value_long:
            i += 2
            continue
        if (t.startswith("--git-dir=") or t.startswith("--work-tree=")
                or t.startswith("--namespace=") or t.startswith("--super-prefix=")):
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        return (t.lower(), tokens[i + 1:])
    return None


def _is_destructive_git(tokens):
    """Destructive `git`: `reset --hard`, `checkout --`/`.`/`-f`, `clean -f*`,
    `push --force`/`+refspec`, `commit --amend`, `rm -r*`, `switch` discard/force.
    Port of `isDestructiveGit`. Returns (matched: bool, label: str|None)."""
    sub = _find_git_subcommand(tokens)
    if not sub:
        return (False, None)
    command, rest = sub

    if command == "reset":
        if "--hard" in rest:
            return (True, "git reset --hard")
        return (False, None)

    if command == "checkout":
        for t in rest:
            if t in ("--", ".", "--force"):
                return (True, "git checkout (discards working tree)")
            if t.startswith("-") and not t.startswith("--") and "f" in t[1:]:
                return (True, "git checkout -f")
        return (False, None)

    if command == "clean":
        for t in rest:
            if t == "--force":
                return (True, "git clean --force")
            if t.startswith("-") and not t.startswith("--") and "f" in t[1:]:
                return (True, "git clean -f")
        return (False, None)

    if command == "push":
        with_lease = False
        bare_force = False
        plus_refspec_force = False
        for t in rest:
            if t == "--force-with-lease" or t.startswith("--force-with-lease="):
                with_lease = True
                continue
            if t == "--force" or t.startswith("--force="):
                bare_force = True
                continue
            if t.startswith("-") and not t.startswith("--") and "f" in t[1:]:
                bare_force = True
                continue
            # Refspec prefix `+<src>[:<dst>]` forces a non-fast-forward update.
            if len(t) > 1 and t.startswith("+") and re.match(r"^\+(?:[a-zA-Z_/.:]|HEAD)", t):
                plus_refspec_force = True
        if bare_force or (plus_refspec_force and not with_lease):
            return (True, "git push --force")
        return (False, None)

    if command == "commit":
        if "--amend" in rest:
            return (True, "git commit --amend")
        return (False, None)

    if command == "rm":
        for t in rest:
            if t.startswith("-") and not t.startswith("--") and re.search(r"[rR]", t[1:]):
                return (True, "git rm -r")
        return (False, None)

    if command == "switch":
        for t in rest:
            if t in ("--discard-changes", "--force"):
                return (True, "git switch (discards working tree)")
            if t.startswith("-") and not t.startswith("--") and re.search(r"[fC]", t[1:]):
                return (True, "git switch -f/-C")
        return (False, None)

    return (False, None)


def classify_command(cmd):
    """Classify a shell command line for destructive actions across ALL command
    bodies (top-level segments + nested subshell bodies, so subshell evasion is
    caught).

    Detects: `rm -rf` / `rm -f`; git `reset --hard` / `checkout --` / `clean -f` /
    `push --force` / `commit --amend` (+ rm/switch/checkout force forms); SQL
    `drop table` / `delete from` / `truncate`; `dd`; `shred`; `kubectl delete`.

    Returns:
        {
          "destructive": bool,                       # any category matched
          "categories": [..],                        # de-duped, sorted
          "reasons": [..],                           # human-readable, first-seen order
        }
    """
    command = cmd if isinstance(cmd, str) else str(cmd or "")
    categories = []
    reasons = []
    seen_cat = set()

    def add(category, reason):
        if category not in seen_cat:
            seen_cat.add(category)
            categories.append(category)
        if reason not in reasons:
            reasons.append(reason)

    segments = all_command_bodies(command)

    for segment in segments:
        stripped = _strip_quoted_strings(segment)

        # SQL / dd-regex phrases (ECC parity: quote-stripped). Each SQL verb is
        # its own category for clarity; `truncate` here also covers the coreutil.
        if re.search(r"\bdrop\s+table\b", stripped, re.IGNORECASE):
            add("sql_drop", "SQL DROP TABLE")
        if re.search(r"\bdelete\s+from\b", stripped, re.IGNORECASE):
            add("sql_delete", "SQL DELETE FROM")
        if re.search(r"\btruncate\b", stripped, re.IGNORECASE):
            add("sql_truncate", "SQL/coreutil TRUNCATE")

        tokens = _tokenize(segment)
        if not tokens:
            continue
        base = _command_basename(tokens[0])

        if _is_destructive_rm(tokens):
            add("rm_rf", "rm -rf (recursive force delete)")
        elif _is_rm_force(tokens):
            add("rm_f", "rm -f (force delete)")

        git_hit, git_label = _is_destructive_git(tokens)
        if git_hit:
            add("git_destructive", git_label)

        # dd: token-based so `dd if=...` is actually caught (see docstring #2).
        if base == "dd":
            add("dd", "dd (raw disk/device write)")

        if base == "shred":
            add("shred", "shred (irrecoverable file wipe)")

        if base == "kubectl" and "delete" in tokens[1:]:
            add("kubectl_delete", "kubectl delete (removes cluster resources)")

    return {
        "destructive": bool(categories),
        "categories": sorted(categories),
        "reasons": reasons,
    }


# ─── --no-verify detection (port of block-no-verify.js) ───────────────────────
_GIT_COMMANDS_WITH_NO_VERIFY = ["commit", "push", "merge", "cherry-pick", "rebase", "am"]
_VALID_BEFORE_GIT = " \t\n\r;&|$`(<{!\"'/.~\\"
_GIT_CONFIG_KEY_PREFIX = "core.hookspath="

_COMMIT_OPTIONS_WITH_VALUE = {
    "-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c",
    "--reedit-message", "--author", "--date", "--template", "--fixup",
    "--squash", "--pathspec-from-file",
}
_COMMIT_OPTIONS_WITH_INLINE_VALUE = [
    "--message=", "--file=", "--reuse-message=", "--reedit-message=",
    "--author=", "--date=", "--template=", "--fixup=", "--squash=",
    "--pathspec-from-file=",
]
_COMMIT_SHORT_OPTIONS_WITH_VALUE = set("mFCct")


def _tokenize_shell_words(text, start, end):
    """Tokenize `text[start:end]` into {value,start,end}, honoring quotes/escapes.
    Port of block-no-verify's `tokenizeShellWords` (positions retained for the
    hooksPath scan)."""
    tokens = []
    value = ""
    token_start = None
    quote = None
    escaped = False

    def begin(idx):
        nonlocal token_start
        if token_start is None:
            token_start = idx

    i = start
    while i < end:
        char = text[i]

        if escaped:
            begin(i - 1)
            value += char
            escaped = False
            i += 1
            continue

        if quote:
            if char == quote:
                quote = None
                i += 1
                continue
            if quote == '"' and char == "\\":
                begin(i)
                escaped = True
                i += 1
                continue
            begin(i)
            value += char
            i += 1
            continue

        if char == '"' or char == "'":
            begin(i)
            quote = char
            i += 1
            continue

        if char == "\\":
            begin(i)
            escaped = True
            i += 1
            continue

        if char.isspace():
            if token_start is not None:
                tokens.append({"value": value, "start": token_start, "end": i})
                value = ""
                token_start = None
            i += 1
            continue

        begin(i)
        value += char
        i += 1

    if escaped:
        value += "\\"
    if token_start is not None:
        tokens.append({"value": value, "start": token_start, "end": end})
    return tokens


def _find_command_segment_end(text, start):
    """Index of the next unquoted `;` `|` `&` `\\n` from `start`, else len. Port
    of `findCommandSegmentEnd`."""
    quote = None
    escaped = False
    i = start
    n = len(text)
    while i < n:
        char = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if quote:
            if quote == '"' and char == "\\":
                escaped = True
                i += 1
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char == '"' or char == "'":
            quote = char
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        if char in ";|&\n":
            return i
        i += 1
    return n


def _is_commit_no_verify_short_flag(value):
    """`-n` or a combined `-n<letter…>` short flag (commit's `--no-verify`)."""
    return value == "-n" or bool(re.match(r"^-n[a-zA-Z]", value))


def _get_commit_short_value_option(value):
    """For a combined short token, if a value-taking short option (m/F/C/c/t)
    appears, report whether it consumes the next token or holds an inline value.
    Port of `getCommitShortValueOption`."""
    if not value.startswith("-") or value.startswith("--") or value == "-":
        return None
    options = value[1:]
    for idx, ch in enumerate(options):
        if ch in _COMMIT_SHORT_OPTIONS_WITH_VALUE:
            return {
                "consumes_next": idx == len(options) - 1,
                "inline_value": idx < len(options) - 1,
            }
    return None


def _commit_option_consumes_next(value):
    if _is_commit_no_verify_short_flag(value):
        return False
    if value in _COMMIT_OPTIONS_WITH_VALUE:
        return True
    opt = _get_commit_short_value_option(value)
    return bool(opt and opt["consumes_next"])


def _commit_option_contains_inline(value):
    if _is_commit_no_verify_short_flag(value):
        return False
    if any(value.startswith(p) for p in _COMMIT_OPTIONS_WITH_INLINE_VALUE):
        return True
    opt = _get_commit_short_value_option(value)
    return bool(opt and opt["inline_value"])


def _is_in_comment(text, idx):
    """True if `idx` falls inside an unescaped `#` shell comment on its line.
    Port of `isInComment`."""
    line_start = text.rfind("\n", 0, idx) + 1
    before = text[line_start:idx]
    for i, ch in enumerate(before):
        if ch == "#":
            prev = before[i - 1] if i > 0 else ""
            if prev != "$" and prev != "\\":
                return True
    return False


def _find_git(text, start):
    """Find the next `git`/`git.exe` token preceded by a valid boundary and
    followed by whitespace/quote. Returns {idx,len} or None. Port of `findGit`."""
    pos = start
    n = len(text)
    while pos < n:
        idx = text.find("git", pos)
        if idx == -1:
            return None
        is_exe = text[idx + 3:idx + 7].lower() == ".exe"
        length = 7 if is_exe else 3
        after = text[idx + length] if idx + length < n else " "
        if not re.match(r"[\s\"']", after):
            pos = idx + 1
            continue
        before = text[idx - 1] if idx > 0 else " "
        if before in _VALID_BEFORE_GIT:
            return {"idx": idx, "len": length}
        pos = idx + 1
    return None


def _detect_git_command(text, start=0):
    """Detect the git subcommand nearest to `git`, skipping global flags/args and
    not crossing `;`/`|`. Returns a dict of positions or None. Port of
    `detectGitCommand`."""
    n = len(text)
    while start < n:
        git = _find_git(text, start)
        if not git:
            return None
        if _is_in_comment(text, git["idx"]):
            start = git["idx"] + git["len"]
            continue

        best_cmd = None
        best_idx = None
        git_after = git["idx"] + git["len"]

        for cmd in _GIT_COMMANDS_WITH_NO_VERIFY:
            search_pos = git_after
            while search_pos < n:
                cmd_idx = text.find(cmd, search_pos)
                if cmd_idx == -1:
                    break
                before = text[cmd_idx - 1] if cmd_idx > 0 else " "
                after = text[cmd_idx + len(cmd)] if cmd_idx + len(cmd) < n else " "
                if not re.match(r"\s", before):
                    search_pos = cmd_idx + 1
                    continue
                if after != "" and not re.match(r"[\s;&#|>)\]}\"']", after):
                    search_pos = cmd_idx + 1
                    continue
                if re.search(r"[;|]", text[git_after:cmd_idx]):
                    break
                if _is_in_comment(text, cmd_idx):
                    search_pos = cmd_idx + 1
                    continue

                gap = text[git_after:cmd_idx]
                gap_tokens = [t for t in gap.strip().split() if t]
                only_flags = True
                expect_arg = False
                for t in gap_tokens:
                    if expect_arg:
                        expect_arg = False
                        continue
                    if t.startswith("-"):
                        if t in ("-c", "-C", "--work-tree", "--git-dir",
                                 "--namespace", "--super-prefix"):
                            expect_arg = True
                        continue
                    only_flags = False
                    break
                if not only_flags:
                    search_pos = cmd_idx + 1
                    continue

                if best_idx is None or cmd_idx < best_idx:
                    best_idx = cmd_idx
                    best_cmd = cmd
                break

        if best_cmd is not None:
            return {
                "command": best_cmd,
                "offset": best_idx + len(best_cmd),
                "git_start": git["idx"],
                "git_end": git["idx"] + git["len"],
                "command_start": best_idx,
            }

        start = git["idx"] + git["len"]
    return None


def _has_no_verify_flag(text, command, offset):
    """True if the segment from `offset` carries `--no-verify` (or commit's `-n`
    short forms), with `-m`/`-F`/… message/value bodies skipped. Port of
    `hasNoVerifyFlag`."""
    segment_end = _find_command_segment_end(text, offset)
    tokens = _tokenize_shell_words(text, offset, segment_end)
    skip_next = False

    for token in tokens:
        value = token["value"]

        if skip_next:
            skip_next = False
            continue

        if value == "--":
            break

        if command == "commit":
            if _commit_option_consumes_next(value):
                skip_next = True
                continue
            if _commit_option_contains_inline(value):
                continue

        if value == "--no-verify":
            return True

        if command == "commit" and _is_commit_no_verify_short_flag(value):
            return True

    return False


def _has_hooks_path_override(text, detected):
    """True if a `-c core.hooksPath=` override sits between `git` and the
    subcommand. Port of `hasHooksPathOverride` (config key case-insensitive)."""
    tokens = _tokenize_shell_words(text, detected["git_end"], detected["command_start"])
    i = 0
    while i < len(tokens):
        value = tokens[i]["value"]
        lowered = value.lower()
        if value == "-c":
            nxt = tokens[i + 1]["value"] if i + 1 < len(tokens) else None
            if isinstance(nxt, str) and nxt.lower().startswith(_GIT_CONFIG_KEY_PREFIX):
                return True
            i += 2
            continue
        if lowered.startswith("-c" + _GIT_CONFIG_KEY_PREFIX):
            return True
        i += 1
    return False


def blocks_no_verify(cmd):
    """True if the command bypasses git hooks: a `commit`/`push`/`merge`/
    `cherry-pick`/`rebase`/`am` using `--no-verify` (or `-n`/`-n<x>` for commit),
    or any of those with a `-c core.hooksPath=` override.

    Crucially flag-position-aware: `--no-verify` appearing inside a `-m`/`-F`
    message body does NOT trigger (the message value is skipped). Port of
    block-no-verify's `checkCommand`.
    """
    text = cmd if isinstance(cmd, str) else str(cmd or "")
    start = 0
    n = len(text)
    while start < n:
        detected = _detect_git_command(text, start)
        if not detected:
            return False
        if _has_hooks_path_override(text, detected):
            return True
        if _has_no_verify_flag(text, detected["command"], detected["offset"]):
            return True
        start = _find_command_segment_end(text, detected["offset"]) + 1
    return False


# ─── Governance event detection (port of governance-capture.js) ───────────────
def detect_governance(text):
    """Scan free text for governance-relevant signals and return a flat list of
    findings.

    Each finding is `{"type", "match", "detail"}` where `type` is one of:
      - "secret"        a hardcoded-secret pattern hit (AWS key, gh token, JWT,
                        PEM private key, generic `api_key=` style); detail=name
      - "approval"      an operation needing sign-off (force-push, reset --hard,
                        rm -rf, DROP TABLE/DATABASE, DELETE FROM); detail=regex
      - "sensitive_path" a sensitive file reference (.env, .pem, .key,
                        credentials, secrets., id_rsa); detail=pattern
      - "security"      an elevated-privilege command (sudo/chmod/chown);
                        detail=reason

    `match` is the matched substring. The secret/approval/sensitive-path lists
    are ported verbatim from governance-capture.js; the security/elevated check
    mirrors its post-phase `security_finding` heuristic but is applied to the
    same text for a single-call surface.
    """
    body = text if isinstance(text, str) else str(text or "")
    findings = []
    if not body:
        return findings

    for name, pattern in _SECRET_PATTERNS:
        m = pattern.search(body)
        if m:
            findings.append({"type": "secret", "match": m.group(0), "detail": name})

    for pattern in _APPROVAL_PATTERNS:
        m = pattern.search(body)
        if m:
            findings.append({"type": "approval", "match": m.group(0),
                             "detail": pattern.pattern})

    for pattern in _SENSITIVE_PATH_PATTERNS:
        m = pattern.search(body)
        if m:
            findings.append({"type": "sensitive_path", "match": m.group(0),
                             "detail": pattern.pattern})

    elevated = re.search(r"(?:sudo|chmod|chown)\s", body)
    if elevated:
        findings.append({"type": "security", "match": elevated.group(0).strip(),
                         "detail": "elevated_privilege_command"})

    return findings
