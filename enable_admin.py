#!/usr/bin/env python3
"""Give the current GRG-4355u account the existing admin RPC permission groups."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import time

from enable_bridge import Router, Failure


def account(router, username):
    values = router.rpc("uci", "get", {"config": "rpcd"})["values"]
    matches = [(name, section) for name, section in values.items()
               if section.get(".type") == "login" and section.get("username") == username]
    if len(matches) != 1:
        raise Failure("Cannot identify exactly one RPC login for the supplied username.")
    name, section = matches[0]
    # Never return or persist a password or password hash.
    return name, {key: section[key] for key in ("read", "write") if key in section}


def set_permissions(router, section, permissions):
    for key in ("read", "write"):
        if key in permissions:
            router.rpc("uci", "set", {"config": "rpcd", "section": section,
                                      "values": {key: permissions[key]}})
        else:
            router.rpc("uci", "delete", {"config": "rpcd", "section": section,
                                         "options": [key]})


def private_json(prefix, data):
    path = Path(__file__).resolve().parent / (
        prefix + time.strftime("-%Y%m%d-%H%M%S") + f"-{os.getpid()}.json")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
    return path


def change(router, username, password, permissions):
    section, before = account(router, username)
    if before == permissions:
        return router, False
    # Commit only rpcd. Do not apply/reload networking or restart the daemon.
    # Existing sessions retain their permissions. A new login loads the new groups.
    attempted_commit = False
    try:
        set_permissions(router, section, permissions)
        attempted_commit = True
        router.rpc("uci", "commit", {"config": "rpcd"})
        fresh = Router("192.168.1.1")
        fresh.login(username, password)
        fresh_section, actual = account(fresh, username)
        if fresh_section != section or actual != permissions:
            raise Failure("The fresh login did not show the requested RPC configuration.")
        if permissions == {"read": ["*"], "write": ["*"]}:
            for function in ("read", "write"):
                access = fresh.rpc("session", "access", {
                    "scope": "access-group", "object": "cxt-base-wan-config",
                    "function": function})
                if not access.get("access"):
                    raise Failure("The fresh login still lacks the WAN configuration permission.")
        return fresh, True
    except BaseException:
        try:
            router.rpc("uci", "revert", {"config": "rpcd"})
            if attempted_commit:
                set_permissions(router, section, before)
                router.rpc("uci", "commit", {"config": "rpcd"})
            print("Previous RPC permissions restored.")
        except Exception:
            print("Automatic recovery could not be verified. Use the saved recovery file.", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspect without changing permissions")
    parser.add_argument("--restore", type=Path, help="Restore permissions from this script's recovery file")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise Failure("Run this script in an interactive terminal.")
    print("Connect to the Comtrend LAN or Wi-Fi at 192.168.1.1.")
    print("This grants the current account all defined RPC permission groups.")
    print("Its password, SSH configuration, network, and firmware remain unchanged.")
    print("Local HTTPS accepts the router's self-signed certificate.")
    username = input("Current Comtrend username: ").strip()
    password = getpass.getpass("Current Comtrend password: ")
    router = Router("192.168.1.1")
    router.login(username, password)
    if router.rpc("uci", "changes", {"config": "rpcd"}).get("changes"):
        raise Failure("Pending rpcd changes exist. Resolve them before running this script.")
    if not router.rpc("session", "access", {
            "scope": "uci", "object": "rpcd", "function": "write"}).get("access"):
        raise Failure("This account cannot write the RPC configuration.")
    section, before = account(router, username)
    desired = {"read": ["*"], "write": ["*"]}
    if args.restore:
        saved = json.loads(args.restore.read_text())
        if saved.get("format") != "comtrend-rpc-permissions-v1" or saved.get("username") != username:
            raise Failure("The recovery file does not match this account.")
        desired = saved["permissions"]
        if not isinstance(desired, dict) or set(desired) - {"read", "write"}:
            raise Failure("Invalid recovery permission fields.")
        if any(not isinstance(v, list) or not all(isinstance(x, str) for x in v)
               for v in desired.values()):
            raise Failure("Invalid recovery permission values.")
    print(f"Matched account section: {section}")
    if args.dry_run:
        print("Dry run passed. No configuration changed.")
        return
    recovery = private_json("admin-recovery", {
        "format": "comtrend-rpc-permissions-v1", "username": username, "permissions": before})
    print(f"Recovery file: {recovery}")
    _, changed = change(router, username, password, desired)
    del password
    print("RPC permissions verified." if changed else "RPC permissions already match.")
    print("Log out of the router web interface, then log in again to refresh its menus.")
    if not args.restore:
        print("FIRST: open System > Backup / Flash Firmware and download a configuration backup.")
        print("Do this before changing the bridge, firewall, or installed packages.")


if __name__ == "__main__":
    try:
        main()
    except (Failure, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.", file=sys.stderr)
        sys.exit(130)
