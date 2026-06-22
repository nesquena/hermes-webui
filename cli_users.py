#!/usr/bin/env python3
"""CLI utility for managing WebUI users.

Usage:
    python cli_users.py add <username> <password> [--profile PROFILE]
    python cli_users.py remove <username>
    python cli_users.py passwd <username> <new-password>
    python cli_users.py set-profile <username> <profile>
    python cli_users.py list

Runs against the WebUI's STATE_DIR (configurable via --state-dir or
$HERMES_WEBUI_STATE_DIR; defaults to ~/.hermes/webui).
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure we can import from the api package.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _resolve_state_dir(args) -> Path:
    env_dir = os.environ.get("HERMES_WEBUI_STATE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    if args.state_dir:
        return Path(args.state_dir).expanduser().resolve()
    return Path.home() / ".hermes" / "webui"


def cmd_add(args):
    from api.users import add_user
    profile = args.profile or args.username
    ok = add_user(args.username, args.password, profile)
    if ok:
        print(f"User '{args.username}' created (profile: {profile}).")
    else:
        print(f"Error: user '{args.username}' already exists.", file=sys.stderr)
        sys.exit(1)


def cmd_remove(args):
    from api.users import delete_user
    ok = delete_user(args.username)
    if ok:
        print(f"User '{args.username}' removed.")
    else:
        print(f"Error: user '{args.username}' not found.", file=sys.stderr)
        sys.exit(1)


def cmd_passwd(args):
    from api.users import change_password
    ok = change_password(args.username, args.password)
    if ok:
        print(f"Password changed for '{args.username}'.")
    else:
        print(f"Error: user '{args.username}' not found.", file=sys.stderr)
        sys.exit(1)


def cmd_set_profile(args):
    from api.users import set_user_profile
    ok = set_user_profile(args.username, args.profile)
    if ok:
        print(f"Profile for '{args.username}' set to '{args.profile}'.")
    else:
        print(f"Error: user '{args.username}' not found.", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    from api.users import list_users
    users = list_users()
    if not users:
        print("No users configured.")
        return
    print(f"{'Username':<20} {'Profile':<20} {'Created':<30}")
    print("-" * 70)
    for u in users:
        import time
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(u.get("created_at", 0))) if u.get("created_at") else "N/A"
        print(f"{u['username']:<20} {u.get('profile', ''):<20} {ts:<30}")


def main():
    parser = argparse.ArgumentParser(description="WebUI user management CLI")
    parser.add_argument("--state-dir", help="Override WebUI state directory")
    sub = parser.add_subparsers(title="commands", dest="command")

    p_add = sub.add_parser("add", help="Create a new user")
    p_add.add_argument("username")
    p_add.add_argument("password")
    p_add.add_argument("--profile", "-p", help="Hermes profile name (default: username)")

    p_rm = sub.add_parser("remove", help="Delete a user")
    p_rm.add_argument("username")

    p_pw = sub.add_parser("passwd", help="Change a user's password")
    p_pw.add_argument("username")
    p_pw.add_argument("password")

    p_sp = sub.add_parser("set-profile", help="Set a user's Hermes profile")
    p_sp.add_argument("username")
    p_sp.add_argument("profile")

    p_ls = sub.add_parser("list", help="List all users")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Resolve state dir and ensure it's on Python path for api.* imports.
    state_dir = _resolve_state_dir(args)
    os.environ.setdefault("HERMES_WEBUI_STATE_DIR", str(state_dir))

    # Ensure api package is importable
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    cmds = {
        "add": cmd_add,
        "remove": cmd_remove,
        "passwd": cmd_passwd,
        "set-profile": cmd_set_profile,
        "list": cmd_list,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
