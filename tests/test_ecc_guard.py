"""Hermetic + adversarial tests for the ECC command-safety guard (ecc.guard).

Pure stdlib, no filesystem or network — operates entirely on string inputs.
Run: python3 -m pytest tests/test_ecc_guard.py -v   (or: python3 tests/test_ecc_guard.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from ecc import guard  # noqa: E402


# ─── split_segments ──────────────────────────────────────────────────────────
def test_split_segments_top_level_operators():
    assert guard.split_segments("a; b | c && d || e") == ["a", "b", "c", "d", "e"]


def test_split_segments_quote_aware():
    # Separators inside quotes must NOT split.
    assert guard.split_segments('echo "a; b | c"') == ['echo "a; b | c"']


def test_split_segments_keeps_redirection():
    # &> / 2>&1 are redirections, not segment separators.
    seg = guard.split_segments("cmd 2>&1")
    assert seg == ["cmd 2>&1"]


# ─── extract_subshells / all_command_bodies ──────────────────────────────────
def test_extract_subshells_dollar_paren():
    assert "rm -rf x" in guard.extract_subshells('echo "$(rm -rf x)"')


def test_extract_subshells_backtick():
    assert "rm -rf /tmp" in guard.extract_subshells("echo `rm -rf /tmp`")


def test_extract_subshells_nested():
    # A (...) inside a $(...) must be discovered recursively.
    bodies = guard.extract_subshells("echo $( ( rm -rf y ) )")
    assert any("rm -rf y" in b for b in bodies)


def test_all_command_bodies_includes_raw_and_nested():
    bodies = guard.all_command_bodies('ls; echo "$(rm -rf x)"')
    assert "ls" in bodies
    assert "rm -rf x" in bodies


# ─── classify_command: destructive detection ─────────────────────────────────
def test_classify_rm_rf_destructive():
    r = guard.classify_command("rm -rf /tmp/x")
    assert r["destructive"] is True
    assert "rm_rf" in r["categories"]


def test_classify_rm_f_destructive():
    r = guard.classify_command("rm -f /tmp/file")
    assert r["destructive"] is True
    assert "rm_f" in r["categories"]


def test_classify_subshell_evasion_caught():
    # The destructive command hides inside a $(...) — must still be flagged.
    r = guard.classify_command('echo "$(rm -rf x)"')
    assert r["destructive"] is True
    assert "rm_rf" in r["categories"]


def test_classify_backtick_evasion_caught():
    r = guard.classify_command("echo y | `rm -rf /tmp`")
    assert r["destructive"] is True


def test_classify_subshell_group_evasion_caught():
    r = guard.classify_command("( rm -rf /tmp/y )")
    assert r["destructive"] is True


def test_classify_brace_group_evasion_caught():
    r = guard.classify_command("{ rm -rf /tmp/z; }")
    assert r["destructive"] is True


def test_classify_git_reset_hard():
    r = guard.classify_command("git reset --hard HEAD~1")
    assert r["destructive"] is True
    assert "git_destructive" in r["categories"]


def test_classify_git_checkout_discard():
    assert guard.classify_command("git checkout -- file.py")["destructive"] is True


def test_classify_git_clean_force():
    assert guard.classify_command("git clean -fdx")["destructive"] is True


def test_classify_git_push_force():
    r = guard.classify_command("git push --force origin main")
    assert r["destructive"] is True
    assert "git_destructive" in r["categories"]


def test_classify_git_push_force_with_lease_is_safe():
    # --force-with-lease is the safety-checked form; not destructive.
    assert guard.classify_command("git push --force-with-lease origin main")["destructive"] is False


def test_classify_git_push_plus_refspec():
    assert guard.classify_command("git push origin +main")["destructive"] is True


def test_classify_git_commit_amend():
    assert guard.classify_command("git commit --amend")["destructive"] is True


def test_classify_git_global_flags_skipped():
    # `-c key=value` / `-C path` precede the subcommand; must still resolve reset.
    assert guard.classify_command("git -c gc.auto=0 reset --hard")["destructive"] is True


def test_classify_sql_drop():
    r = guard.classify_command("psql mydb -c DROP TABLE users")
    assert r["destructive"] is True
    assert "sql_drop" in r["categories"]


def test_classify_sql_delete_and_truncate():
    assert "sql_delete" in guard.classify_command("DELETE FROM logs")["categories"]
    assert "sql_truncate" in guard.classify_command("TRUNCATE bigtable")["categories"]


def test_classify_dd():
    r = guard.classify_command("dd if=/dev/zero of=/dev/sda bs=1M")
    assert r["destructive"] is True
    assert "dd" in r["categories"]


def test_classify_shred():
    assert "shred" in guard.classify_command("shred -u secret.txt")["categories"]


def test_classify_kubectl_delete():
    r = guard.classify_command("kubectl delete pod nginx")
    assert r["destructive"] is True
    assert "kubectl_delete" in r["categories"]


# ─── classify_command: benign / false-positive resistance ────────────────────
def test_classify_benign_ls_clean():
    r = guard.classify_command("ls -la")
    assert r["destructive"] is False
    assert r["categories"] == []
    assert r["reasons"] == []


def test_classify_quoted_sql_in_message_is_clean():
    # A commit message mentioning "drop table" must not trip the SQL detector
    # (quoted strings are stripped before matching), mirroring ECC.
    r = guard.classify_command('git commit -m "drop table mention in message"')
    assert r["destructive"] is False


def test_classify_plain_git_status_clean():
    assert guard.classify_command("git status --porcelain")["destructive"] is False


# ─── blocks_no_verify ────────────────────────────────────────────────────────
def test_no_verify_message_body_not_flagged():
    # ADVERSARIAL: --no-verify only appears inside the -m message body.
    assert guard.blocks_no_verify('git commit -m "do not use --no-verify"') is False


def test_no_verify_F_file_body_not_flagged():
    assert guard.blocks_no_verify("git commit -F notes-about-no-verify.txt") is False


def test_no_verify_commit_flag_flagged():
    assert guard.blocks_no_verify("git commit --no-verify") is True


def test_no_verify_commit_after_message_flagged():
    assert guard.blocks_no_verify('git commit -m "msg" --no-verify') is True


def test_no_verify_commit_short_n_flagged():
    assert guard.blocks_no_verify("git commit -n") is True


def test_no_verify_commit_combined_short_flagged():
    # -nm = -n (no-verify) + -m; the combined form must still trip.
    assert guard.blocks_no_verify('git commit -nm "msg"') is True


def test_no_verify_push_flagged():
    assert guard.blocks_no_verify("git push --no-verify") is True


def test_no_verify_push_n_is_not_no_verify():
    # For push, -n is dry-run, NOT --no-verify; must not be flagged.
    assert guard.blocks_no_verify("git push -n origin main") is False


def test_no_verify_merge_and_rebase_flagged():
    assert guard.blocks_no_verify("git merge --no-verify topic") is True
    assert guard.blocks_no_verify("git rebase --no-verify") is True


def test_no_verify_hooks_path_override_flagged():
    assert guard.blocks_no_verify("git -c core.hooksPath=/dev/null commit -m x") is True


def test_no_verify_hooks_path_case_insensitive():
    assert guard.blocks_no_verify("git -c core.HOOKSPATH=/dev/null commit -m x") is True


def test_no_verify_non_git_clean():
    assert guard.blocks_no_verify("ls -la") is False


def test_no_verify_plain_commit_clean():
    assert guard.blocks_no_verify('git commit -m "normal message"') is False


# ─── detect_governance ───────────────────────────────────────────────────────
def test_governance_aws_key():
    findings = guard.detect_governance("export AWS=AKIAIOSFODNN7EXAMPLE")
    types = {f["type"] for f in findings}
    assert "secret" in types
    assert any(f["detail"] == "aws_key" for f in findings)


def test_governance_github_token():
    tok = "ghp_" + "A" * 36
    findings = guard.detect_governance(f"token={tok}")
    assert any(f["type"] == "secret" and f["detail"] == "github_token" for f in findings)


def test_governance_jwt():
    jwt = "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12
    assert any(f["detail"] == "jwt" for f in guard.detect_governance(jwt))


def test_governance_pem_private_key():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert any(f["detail"] == "private_key" for f in guard.detect_governance(pem))


def test_governance_generic_api_key():
    findings = guard.detect_governance('api_key="s3cr3tval0123"')
    assert any(f["detail"] == "generic_secret" for f in findings)


def test_governance_env_path():
    findings = guard.detect_governance("cat /app/config/.env")
    assert any(f["type"] == "sensitive_path" for f in findings)


def test_governance_pem_and_key_paths():
    assert any(f["type"] == "sensitive_path"
               for f in guard.detect_governance("/keys/server.pem"))
    assert any(f["type"] == "sensitive_path"
               for f in guard.detect_governance("/keys/server.key"))
    assert any(f["type"] == "sensitive_path"
               for f in guard.detect_governance("read ~/.aws/credentials"))


def test_governance_approval_force_push_and_destructive():
    findings = guard.detect_governance("git push --force origin main")
    assert any(f["type"] == "approval" for f in findings)
    findings = guard.detect_governance("rm -rf /tmp/x")
    assert any(f["type"] == "approval" for f in findings)
    findings = guard.detect_governance("DROP TABLE users")
    assert any(f["type"] == "approval" for f in findings)


def test_governance_elevated_privilege():
    assert any(f["type"] == "security" for f in guard.detect_governance("sudo rm /etc/x"))


def test_governance_benign_clean():
    assert guard.detect_governance("just some normal log output here") == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
