"""Hermetic publish-pipeline tests: temp public git repo, real main repo read-only.

The main repo is only READ (git ls-files / log / file contents); the public
side is a throwaway git repo in tmp, selected via CLANKER_PUBLIC_REPO.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import publishcmd  # noqa: E402


def _mk_public(tmp):
    pub = os.path.join(tmp, "public")
    os.makedirs(pub)
    subprocess.run(["git", "init", "-q", "-b", "main", pub], check=True)
    subprocess.run(["git", "-C", pub, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", pub, "config", "user.name", "t"], check=True)
    # public-only files that must survive
    with open(os.path.join(pub, "LICENSE"), "w") as f:
        f.write("MIT\n")
    with open(os.path.join(pub, "README.md"), "w") as f:
        f.write("public readme — never clobbered\n")
    # a stale main-origin file that must get swept (was tracked in main history)
    os.makedirs(os.path.join(pub, "adapters", "claude-code"), exist_ok=True)
    with open(os.path.join(pub, "adapters", "claude-code", "hooks.json"), "w") as f:
        f.write("{}\n")
    subprocess.run(["git", "-C", pub, "add", "-A"], check=True)
    subprocess.run(["git", "-C", pub, "commit", "-qm", "seed"], check=True)
    return pub


def test_check_mode_reports_without_touching(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        pub = _mk_public(tmp)
        monkeypatch.setenv("CLANKER_PUBLIC_REPO", pub)
        rc = publishcmd.publish(check_only=True)
        assert rc == 0
        # nothing changed in the public tree
        out = subprocess.run(["git", "-C", pub, "status", "--porcelain"],
                             capture_output=True, text=True).stdout
        assert out.strip() == ""


def test_publish_copies_excludes_and_sweeps_stale(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        pub = _mk_public(tmp)
        monkeypatch.setenv("CLANKER_PUBLIC_REPO", pub)
        rc = publishcmd.publish()
        assert rc == 0
        # managed content arrived (tracked files only — publish never ships
        # uncommitted work)
        assert os.path.exists(os.path.join(pub, "bin", "clanker"))
        assert os.path.exists(os.path.join(pub, "lib", "registry.py"))
        # excludes NOT copied; public-only preserved
        assert not os.path.exists(os.path.join(pub, "STATUS.md"))
        assert open(os.path.join(pub, "README.md")).read().startswith("public readme")
        assert os.path.exists(os.path.join(pub, "LICENSE"))
        # stale main-origin file swept
        assert not os.path.exists(
            os.path.join(pub, "adapters", "claude-code", "hooks.json"))
        # manifest written with source commit
        m = json.load(open(os.path.join(pub, publishcmd.MANIFEST)))
        assert "bin/clanker" in m["managed"] and m["source_commit"]
        # committed, tree clean
        out = subprocess.run(["git", "-C", pub, "status", "--porcelain"],
                             capture_output=True, text=True).stdout
        assert out.strip() == ""
        # second run: no-op
        rc2 = publishcmd.publish()
        assert rc2 == 0


def test_dirty_public_refuses(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        pub = _mk_public(tmp)
        with open(os.path.join(pub, "LICENSE"), "a") as f:
            f.write("dirty\n")
        monkeypatch.setenv("CLANKER_PUBLIC_REPO", pub)
        assert publishcmd.publish() == 1
