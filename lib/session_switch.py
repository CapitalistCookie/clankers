"""Move a running Claude session to another account slot — `clanker open <name> <slot>`
and the web terminal's switch control (docs/SESSION-ACCOUNTS.md 5.5).

A conversation lives in the config dir of the slot that runs it: the transcript
projects/<dir>/<id>.jsonl, its sidecar projects/<dir>/<id>/ (subagents, tool
results), file-history/<id>/ (rewind checkpoints) and tasks/<id>/. To continue it
on another slot, Claude exits, those files are copied into the target slot, and
Claude starts again in the SAME pane with `--resume <id>` under the target's
config dir. Measured 2026-10-29 (a1 -> a3 -> a1, Claude Code 2.1.312): the
session ID stays, the context stays, the target slot appends to its copy.

Copy rule (accounts D-002), per file: the target copy is missing, equal, a byte
prefix of the source, or unchanged since the session last left that slot (the
departure manifest). Anything else is DIVERGED and the switch stops before
anything changes. No file is ever deleted or modified in the source slot.

Every check that can refuse runs before Claude is stopped. After the stop, any
failure relaunches the session on its source slot (rollback) and verifies it.
"""
import contextlib
import fcntl
import glob
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time

import accounts
import claude_registry
import newsession

EXIT_WAIT = 20.0          # s: Claude exits ~1 s after /exit (measured)
CONV_TREES = ("file-history", "tasks")


def data_dir():
    return os.environ.get("CLANKER_DATA", "/data/clanker")


# ─── conversation files ────────────────────────────────────────────────────

def encode_cwd(cwd):
    """Claude Code's transcript dir name for a cwd: every non-alphanumeric -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def conversation_files(cfg, sid, cwd):
    """Relative paths of the conversation `sid` under config dir `cfg`, transcript
    first; None when the transcript is not found (or found twice)."""
    proj = os.path.join(cfg, "projects")
    t = os.path.join(proj, encode_cwd(cwd), sid + ".jsonl")
    if not os.path.isfile(t):
        hits = glob.glob(os.path.join(proj, "*", sid + ".jsonl"))
        if len(hits) != 1:
            return None
        t = hits[0]
    pdir = os.path.basename(os.path.dirname(t))
    rels = [os.path.join("projects", pdir, sid + ".jsonl")]
    for base in [os.path.join("projects", pdir, sid)] + [os.path.join(x, sid) for x in CONV_TREES]:
        root = os.path.join(cfg, base)
        for dp, _dn, fns in os.walk(root):
            for fn in sorted(fns):
                rels.append(os.path.relpath(os.path.join(dp, fn), cfg))
    return rels


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def plan_copy(src, dst, rels, departed):
    """(copies, diverged): which files to write into `dst`, and which cannot be.
    departed: {rel: sha256} of `dst`'s files when the session last left `dst`."""
    copies, diverged = [], []
    for rel in rels:
        a = _read(os.path.join(src, rel))
        d = os.path.join(dst, rel)
        if not os.path.lexists(d):
            copies.append((rel, "missing"))
            continue
        b = _read(d)
        if a == b:
            continue
        if len(b) < len(a) and a[:len(b)] == b:
            copies.append((rel, "prefix"))
        elif departed.get(rel) == _sha(b):
            copies.append((rel, "unchanged since it left"))
        else:
            diverged.append(rel)
    return copies, diverged


def copy_files(src, dst, copies):
    """Write each planned file atomically (temp file, fsync, rename), mode 0600."""
    for rel, _why in copies:
        target = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        data = _read(os.path.join(src, rel))
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".clanker-switch-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)              # our own temp file, never a slot file
            raise


def _manifest_path(sid):
    return os.path.join(data_dir(), "switch", f"{sid}.json")


def load_manifest(sid):
    try:
        with open(_manifest_path(sid)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_departure(sid, cfg, rels):
    """Record the hashes of the conversation's files in `cfg` as the session leaves it."""
    m = load_manifest(sid)
    m[os.path.realpath(cfg)] = {rel: _sha(_read(os.path.join(cfg, rel))) for rel in rels
                                if os.path.isfile(os.path.join(cfg, rel))}
    p = _manifest_path(sid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=1)
    os.replace(tmp, p)


# ─── the pane ──────────────────────────────────────────────────────────────

_SGR_TOKEN = re.compile(r"\x1b\[([0-9;]*)m|\x1b\[[0-9;?]*[A-Za-z]|([^\x1b]+)")


def undimmed(line):
    """The text of a captured line (`capture-pane -e`) without what is drawn dim
    (SGR 2). Claude draws its prompt-box suggestions dim (`❯ ^[[2mdelete the
    clones…^[[0m`, 2026-10-29); typed text is not dim."""
    out, dim = [], False
    for m in _SGR_TOKEN.finditer(line):
        if m.group(1) is not None:
            ps = [int(x) if x.isdigit() else 0 for x in (m.group(1).split(";") if m.group(1) else ["0"])]
            i = 0
            while i < len(ps):
                if ps[i] in (38, 48):            # extended colour: skip its arguments (5;n or 2;r;g;b)
                    i += 3 if i + 1 < len(ps) and ps[i + 1] == 5 else 5
                    continue
                if ps[i] == 2:
                    dim = True
                elif ps[i] in (0, 22):
                    dim = False
                i += 1
        elif m.group(2) and not dim:
            out.append(m.group(2))
    return "".join(out)


def prompt_box_text(name, pane=None):
    """The text a person typed into Claude's prompt box (between the last two ───
    rules), best effort; the dim suggestion Claude shows there is not counted.
    pane: a pane id (%12) instead of the session's active pane."""
    p = subprocess.run(["tmux", "capture-pane", "-p", "-e", "-J", "-t", pane or f"={name}:"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return ""
    lines = p.stdout.split("\n")
    plain = [re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", ln) for ln in lines]
    rules = [i for i, ln in enumerate(plain) if ln.strip() and set(ln.strip()) <= {"─"} and len(ln.strip()) > 10]
    if len(rules) < 2:
        return ""
    box = lines[rules[-2] + 1:rules[-1]]
    text = [undimmed(ln).strip().lstrip("❯").strip() for ln in box]
    return "\n".join(t for t in text if t).strip()


def _gone(rec):
    return not claude_registry.alive(rec)


def _log(row):
    try:
        os.makedirs(data_dir(), exist_ok=True)
        with open(os.path.join(data_dir(), "account-switch.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _alert(msg, details):
    try:
        import alerts
        alerts._create_alert("account-switch-rollback-failed", "critical", "session_switch", msg,
                             details=details)
    except Exception:
        pass


# ─── switch ────────────────────────────────────────────────────────────────

HELD = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@contextlib.contextmanager
def held_interrupts(note=None):
    """Ctrl-C (SIGINT), SIGTERM and SIGHUP wait until the block ends, so the steps
    between Claude stopping and Claude running again never stop half-way (before
    this, a Ctrl-C there skipped the rollback and left a bare shell). note(text) is
    told at once. A held SIGTERM or SIGHUP is delivered after the block; a held
    Ctrl-C is not (the work it would have stopped is done). Main thread only: the
    dashboard switches from a worker thread, where no terminal sends Ctrl-C."""
    if threading.current_thread() is not threading.main_thread():
        yield []
        return
    got = []

    def hold(signum, _frame):
        got.append(signum)
        if note:
            note("Ctrl-C is held until this finishes (stopping half-way would leave the session broken)"
                 if signum == signal.SIGINT else f"signal {signum} is held until this finishes")

    old = {}
    for sig in HELD:
        old[sig] = signal.getsignal(sig)
        signal.signal(sig, hold)
    try:
        yield got
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
        for sig in dict.fromkeys(got):
            if sig != signal.SIGINT:
                os.kill(os.getpid(), sig)


def switch(name, account, exit_wait=EXIT_WAIT, launch_timeout=None, progress=None, force=False):
    """Move session `name` to slot `account`. Returns (ok, message). progress(text)
    hears each step (the CLI prints them on a terminal). Unsent text in the prompt
    box refuses the switch unless force (the switch clears it)."""
    name = newsession._slug(name)
    lock_dir = os.path.join(data_dir(), "locks")
    os.makedirs(lock_dir, exist_ok=True)
    with open(os.path.join(lock_dir, f"switch-{name}.lock"), "a") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False, f"another account switch of '{name}' is running"
        return _switch_locked(name, account, exit_wait, launch_timeout, progress or (lambda _t: None), force)


def _switch_locked(name, account, exit_wait, launch_timeout, say, force=False):
    if not accounts.enabled():
        return False, "account switch needs the accounts command (single-slot mode has one account)"
    try:
        sl = accounts.slots(online=True)
    except accounts.AccountsError as e:
        return False, f"account slots unavailable: {e}"
    target = accounts.get(account, sl)
    if not target:
        return False, f"unknown account '{account}'\n" + accounts.usable_summary(sl)
    if not target["usable"]:
        return False, f"account {target['id']} is not usable: {target['why']}"

    pane = newsession.pane_info(name)
    if not pane:
        return False, f"no session named '{name}'"
    if pane["command"] != "claude":
        return False, (f"session '{name}' runs {pane['command'] or 'nothing'}, not claude — "
                       f"start Claude in it with: clanker open {name} {target['id']}")
    rec = claude_registry.record_for_pane(pane, claude_registry.read_all(sl))
    if not rec:
        return False, f"session '{name}': no live Claude registry record for its pane"
    cur = accounts.slot_of_pid(rec["pid"], sl)
    if cur is None:
        return False, f"session '{name}': cannot read the environment of pid {rec['pid']}"
    if cur["id"] == target["id"]:
        return True, f"session '{name}' already runs on account {target['id']}"
    if cur["id"] == "?" or not cur.get("real"):
        return False, f"session '{name}' runs on an unknown config dir ({cur.get('why')})"
    if rec.get("status") != "idle":
        return False, (f"session '{name}' is {rec.get('status') or 'starting'}: only an idle session "
                       "switches (wait until it is at rest)")
    if rec["pid"] == pane["pane_pid"]:
        return False, (f"session '{name}': Claude is the pane's own process (an `exec` launch), so "
                       "an exit would close the pane — not switchable")
    sid, cwd = rec.get("sessionId"), rec.get("cwd") or pane["path"]
    if not sid:
        return False, f"session '{name}': its registry record has no session id"
    src, dst = cur["real"], target["real"]
    rels = conversation_files(src, sid, cwd)
    # No transcript yet: Claude never got a message, so there is no conversation to
    # carry — the session starts fresh on the target (no --resume).
    fresh = not rels
    rels = rels or []
    departed = load_manifest(sid).get(os.path.realpath(dst), {})
    _copies, diverged = plan_copy(src, dst, rels, departed)
    if diverged:
        return False, (f"conversation {sid} differs in {target['id']} and cannot be merged "
                       f"({len(diverged)} file(s), e.g. {diverged[0]}): nothing changed")

    draft = prompt_box_text(name)
    if draft and not force:
        return False, (f"session '{name}': its prompt box holds unsent text — send or clear it, or "
                       f"switch anyway with --force (the text is cleared):\n  {draft[:300]}")
    lease_id, warnings, err = newsession._lease(target, name)
    if err:
        return False, err

    with held_interrupts(say):
        # ── point of no return: Claude stops ──
        say(f"stopping Claude in '{name}' (on {cur['id']})…")
        subprocess.run(["tmux", "send-keys", "-t", f"={name}:", "C-c"], capture_output=True)
        time.sleep(0.5)
        newsession._send_line(name, "/exit")
        deadline = time.time() + exit_wait
        while time.time() < deadline and not _gone(rec):
            time.sleep(0.25)
        if not _gone(rec):
            accounts.release(lease_id)
            return False, (f"Claude in '{name}' did not exit within {int(exit_wait)} s — the session "
                           f"stays on {cur['id']}; look at it: clanker open {name}")

        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session": name,
               "session_id": sid, "from": cur["id"], "to": target["id"], "old_pid": rec["pid"],
               "lease": lease_id}
        try:
            rels = conversation_files(src, sid, cwd) or rels      # the exit may have appended
            fresh = not rels
            copies, diverged = plan_copy(src, dst, rels, departed)
            if diverged:
                raise RuntimeError(f"conversation diverged during the exit ({diverged[0]})")
            if copies:
                say(f"copying {len(copies)} conversation file(s) to {target['id']}…")
            copy_files(src, dst, copies)
            if rels:
                save_departure(sid, src, rels)
            say(f"starting Claude on {target['id']}" + ("" if fresh else f" with conversation {sid}") + "…")
            new, err = newsession.start_in_pane(name, cwd, target, resume=None if fresh else sid,
                                                exclude_pids={rec["pid"]}, timeout=launch_timeout)
            if err:
                late = _running_claude(name, sl, {rec["pid"]})
                if late and late[1] and late[1]["id"] == target["id"] and (fresh or late[0].get("sessionId") == sid):
                    new = late[0]                    # it did start, only later than the check
                else:
                    raise RuntimeError(err)
        except Exception as e:
            _log(dict(row, result="rollback", error=str(e)))
            say(f"failed ({e}); starting Claude again on {cur['id']}…")
            return _rollback(name, cwd, sid, cur, rec["pid"], lease_id, str(e), launch_timeout, sl)

        if lease_id and not accounts.renew(lease_id, pid=new["pid"]):
            warnings.append(f"lease {lease_id} could not move to pid {new['pid']} (the reconciler retries)")
        try:
            for l in accounts.leases(max_age=0):
                if l.get("holder") == accounts.holder(name) and l.get("slot") == cur["id"]:
                    accounts.release(l.get("id"))
        except accounts.AccountsError:
            pass
        _log(dict(row, result="ok", new_pid=new["pid"], copied=len(copies)))
        msg = (f"switched '{name}' from {cur['id']} to {accounts.label(target)} "
               + ("(no conversation yet: started fresh)" if fresh
                  else f"(session {sid}, {len(copies)} file(s) copied)"))
        if lease_id:
            msg += f"\nlease {lease_id}"
        for w in warnings:
            msg += f"\nwarning: {w}"
        if draft:
            msg += f"\nnote: the prompt box held text that the switch cleared:\n  {draft}"
        msg += "\nnote: SendMessage does not reach sessions on other account slots"
        return True, msg


def _running_claude(name, sl, exclude):
    """(record, slot) of a Claude now running in the pane of `name`, else None."""
    r = newsession.pane_record(newsession.pane_info(name), exclude)
    return (r, accounts.slot_of_pid(r["pid"], sl)) if r else None


def _rollback(name, cwd, sid, src_slot, old_pid, lease_id, why, launch_timeout, sl):
    """Relaunch the session on its source slot and verify it. Never types into a
    pane where a Claude already runs: that Claude is reported instead."""
    accounts.release(lease_id)
    running = _running_claude(name, sl, {old_pid})
    if running:
        r, slot = running
        return False, (f"switch failed: {why}\na Claude runs in '{name}' on account "
                       f"{slot['id'] if slot else '?'} (pid {r['pid']}, session {r.get('sessionId')}): "
                       f"nothing typed — look at it: clanker open {name}")
    resume = sid if conversation_files(src_slot["real"], sid, cwd) else None
    rec, err = newsession.start_in_pane(name, cwd, src_slot, resume=resume,
                                        exclude_pids={old_pid}, timeout=launch_timeout)
    if err:
        manual = newsession.launch_cmd(cwd, resume=resume, slot=src_slot, session=name)
        _alert(f"account switch of '{name}' failed and so did the rollback: {err}",
               {"session": name, "session_id": sid, "switch_error": why, "manual": manual})
        return False, (f"switch failed: {why}\nROLLBACK FAILED: {err}\n"
                       f"start it by hand in the pane:\n  {manual}")
    return False, (f"switch failed: {why}\nrolled back: session '{name}' runs again on "
                   f"{src_slot['id']} (pid {rec['pid']})")
