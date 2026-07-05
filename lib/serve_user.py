"""CLI enrollment for dashboard logins — `clanker serve-user ...`.

Creates / manages the username + password + TOTP credentials that the dashboard
login uses. Prints a scannable QR (if `qrencode` is installed) plus the manual
setup key + otpauth URI so you can add the account to Google Authenticator.
"""

import getpass
import shutil
import subprocess
import sys

import webauth


def _prompt_password():
    pw = getpass.getpass("New password (min 8 chars): ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw != pw2:
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(1)
    if len(pw) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)
    return pw


def _show_enrollment(username, secret, uri):
    print()
    if shutil.which("qrencode"):
        try:
            subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "1", uri], check=True)
        except Exception:
            pass
    else:
        print("  (install `qrencode` to render a scannable QR here)")
    print(f"\n  Add to Google Authenticator — scan the QR, or enter manually:")
    print(f"    Account : Clanker:{username}")
    print(f"    Key     : {secret}")
    print(f"    Type    : Time based")
    print(f"\n  otpauth URI:\n    {uri}")
    print(f"\n  Then sign in at the dashboard with username + password + the 6-digit code.")


def add(username, force=False):
    username = (username or "").strip()
    if not username:
        print("Usage: clanker serve-user add <username>", file=sys.stderr)
        sys.exit(1)
    if username in webauth.list_users() and not force:
        print(f"User '{username}' already exists. Use --force to reset password + TOTP.",
              file=sys.stderr)
        sys.exit(1)
    pw = _prompt_password()
    try:
        secret, uri = webauth.add_user(username, pw, force=force)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    verb = "reset" if force else "enrolled"
    print(f"\n✓ User '{username}' {verb}.")
    _show_enrollment(username, secret, uri)


def passwd(username):
    username = (username or "").strip()
    if username not in webauth.list_users():
        print(f"No such user: {username}", file=sys.stderr)
        sys.exit(1)
    pw = _prompt_password()
    try:
        webauth.set_password(username, pw)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Password updated for '{username}'. (TOTP secret unchanged.)")


def recovery(username):
    username = (username or "").strip()
    if username not in webauth.list_users():
        print(f"No such user: {username}", file=sys.stderr)
        sys.exit(1)
    codes = webauth.regenerate_recovery_codes(username)
    print(f"✓ New backup codes for '{username}' (any previous codes are now invalid).")
    print("  Save these — each works once, in place of your authenticator code:\n")
    for c in codes:
        print(f"    {c}")
    print()


def remove(username):
    username = (username or "").strip()
    if webauth.remove_user(username):
        print(f"✓ Removed user '{username}'.")
    else:
        print(f"No such user: {username}", file=sys.stderr)
        sys.exit(1)


def list_all():
    users = webauth.list_users()
    if not users:
        print("No dashboard users enrolled. Create one: clanker serve-user add <name>")
        return
    print(f"Enrolled dashboard users ({len(users)}):")
    for u in users:
        print(f"  - {u}")
