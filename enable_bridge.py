#!/usr/bin/env python3
"""Configure the observed DIGI GRG-4355u via its authenticated LuCI API.

Only Python's standard library is required. No SSH or firmware changes.
"""
import argparse
import getpass
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class Failure(Exception):
    pass


class SameRouterRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (new.scheme, new.netloc) != (old.scheme, old.netloc):
            raise Failure("The router redirected to another origin. Login stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Router:
    def __init__(self, host, timeout=10):
        self.base = "https://" + host
        self.timeout = timeout
        self.sid = None
        # This local router uses a self-signed certificate. Never use a proxy.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), SameRouterRedirect(),
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, data=None, content_type=None):
        headers = {"Content-Type": content_type} if content_type else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Never print response bodies, request bodies, cookies or credentials.
            raise Failure(f"Router request to {path} failed ({type(exc).__name__}, timeout {self.timeout}s).") from None

    def login(self, username, password):
        self.request("/cgi-bin/luci", urllib.parse.urlencode({
            "luci_username": username, "luci_password": password
        }).encode(), "application/x-www-form-urlencoded")
        page = self.request("/cgi-bin/luci/")
        match = re.search(r'"sessionid"\s*:\s*"([0-9a-f]{32})"', page)
        if not match:
            raise Failure("Login failed, or this firmware uses a different login format.")
        self.sid = match.group(1)
        if "GRG-4355" not in page:
            raise Failure("The router page does not identify a GRG-4355. No changes made.")

    def rpc(self, obj, method, args=None):
        try:
            raw = self.request("/ubus/", json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "call",
                "params": [self.sid, obj, method, args or {}]
            }).encode(), "application/json")
        except Failure as exc:
            raise Failure(f"{obj}.{method}: {exc}") from None
        try:
            response = json.loads(raw)
            result = response.get("result")
            if not isinstance(result, list) or not result or result[0] != 0:
                code = result[0] if result else response.get("error", {}).get("code", "unknown")
                raise Failure(f"{obj}.{method} failed (code {code}).")
            return result[1] if len(result) > 1 else {}
        except (ValueError, AttributeError, TypeError):
            raise Failure(f"{obj}.{method} returned an unexpected response.") from None

    def network(self):
        return self.rpc("uci", "get", {"config": "network"})["values"]


def sequence(value):
    return value if isinstance(value, list) else str(value or "").split()


def operation(method, **args):
    return {"method": method, "args": {"config": "network", **args}}


def plan(network, mapping, tagged=True):
    """Return forward and inverse operations. Never copy credential fields."""
    if "LAN5(10G):eth0.5" not in sequence(mapping.get("port", {}).get("portmapping")):
        raise Failure("LAN5 does not map to eth0.5. This script cannot select the port safely.")
    lan = network.get("lan", {})
    if lan.get("device") != "br-lan" or lan.get("ipaddr") != "192.168.1.1":
        raise Failure("The management LAN differs from the known configuration. No changes made.")
    lan_sections = [k for k, v in network.items()
                    if v.get(".type") == "device" and v.get("name") == "br-lan"]
    if len(lan_sections) != 1:
        raise Failure("Cannot identify one br-lan device.")
    lan_section = lan_sections[0]
    if network[lan_section].get("type") != "bridge":
        raise Failure("br-lan is not a bridge device.")
    vlan = [v for v in network.values() if v.get("name") == "pon.20"
            and v.get(".type") == "device"]
    if len(vlan) != 1 or vlan[0].get("ifname") != "pon" or str(vlan[0].get("vid")) != "20":
        raise Failure("The fiber VLAN 20 device is missing or changed. No changes made.")

    def physical(device):
        seen = set()
        while isinstance(device, str) and device.startswith("@"):
            name = device[1:]
            if name in seen:
                return None
            seen.add(name)
            device = network.get(name, {}).get("device")
        return device

    ppps = [k for k, v in network.items() if v.get("proto") == "pppoe"
            and physical(v.get("device")) == "pon.20"]
    if not ppps:
        raise Failure("No existing PPPoE interface uses pon.20. Inspect the configuration first.")

    # Only these exact signatures are recognized as our previous bridge objects.
    signatures = {
        "gl_vlan20": ("device", "eth0.5.20"),
        "gl_bridge_device": ("device", "br-glwan"),
        "gl_bridge": ("interface", "br-glwan"),
        "bridge_vlan20": ("device", "eth0.5.20"),
        "bridge_device": ("device", "br-bridge"),
        "bridge": ("interface", "br-bridge"),
    }
    allowed = {".anonymous", ".type", ".name", ".index", "name", "type", "ifname",
               "vid", "ports", "ipv6", "proto", "device", "auto", "disabled"}
    owned = set()
    for name, (kind, dev) in signatures.items():
        if name not in network:
            continue
        value = network[name]
        field = "name" if kind == "device" else "device"
        if value.get(".type") != kind or value.get(field) != dev or set(value) - allowed:
            raise Failure(f"Section {name} has unexpected configuration. No changes made.")
        if kind == "interface" and value.get("proto") != "none":
            raise Failure(f"Section {name} is not an unmanaged bridge interface.")
        if kind == "device" and dev == "eth0.5.20":
            if (value.get("type"), value.get("ifname"), str(value.get("vid"))) != (
                    "8021q", "eth0.5", "20"):
                raise Failure(f"Section {name} is not the expected VLAN device.")
        if kind == "device" and dev.startswith("br-"):
            if value.get("type") != "bridge" or set(sequence(value.get("ports"))) - {
                    "pon.20", "eth0.5", "eth0.5.20"}:
                raise Failure(f"Section {name} contains unexpected bridge ports.")
        owned.add(name)

    reserved = {"eth0.5", "eth0.5.20", "pon.20", "br-glwan", "br-bridge"}
    for name, value in network.items():
        if name in owned or name == lan_section:
            continue
        if value.get("type") == "bridge" and reserved.intersection(sequence(value.get("ports"))):
            raise Failure(f"Another bridge ({name}) uses a required port. No changes made.")
        if value.get("device") in {"br-glwan", "br-bridge", "eth0.5", "eth0.5.20"}:
            raise Failure(f"Another interface ({name}) uses a required device. No changes made.")
        if value.get("name") in {"eth0.5.20", "br-glwan", "br-bridge"}:
            raise Failure(f"Another device ({name}) conflicts with the bridge. No changes made.")

    forward, inverse = [], []

    def change(section, values):
        old = network[section]
        forward.append(operation("set", section=section, values=values))
        present = {key: old[key] for key in values if key in old}
        absent = [key for key in values if key not in old]
        if present:
            inverse.insert(0, operation("set", section=section, values=present))
        if absent:
            inverse.insert(0, operation("delete", section=section, options=absent))

    ports = [p for p in sequence(network[lan_section].get("ports")) if p != "eth0.5"]
    if not ports:
        raise Failure("The change would remove every wired management port.")
    change(lan_section, {"ports": ports})
    for name in ppps:
        change(name, {"auto": "0", "disabled": "1"})
    for name, value in network.items():
        if value.get("proto") == "dhcpv6" and value.get("device") in {"@" + p for p in ppps}:
            change(name, {"auto": "0", "disabled": "1"})
    for name in sorted(owned):
        old = network[name]
        forward.append(operation("delete", section=name))
        inverse.insert(0, operation("add", type=old[".type"], name=name,
                                    values={k: v for k, v in old.items() if not k.startswith(".")}))
    new = []
    if tagged:
        new.append(("bridge_vlan20", "device", {
            "type": "8021q", "ifname": "eth0.5", "vid": "20", "name": "eth0.5.20"}))
    new.extend([
        ("bridge_device", "device", {"type": "bridge", "name": "br-bridge",
         "ports": ["pon.20", "eth0.5.20" if tagged else "eth0.5"], "ipv6": "0"}),
        ("bridge", "interface", {"proto": "none", "device": "br-bridge", "auto": "1"})])
    for name, kind, values in new:
        forward.append(operation("add", type=kind, name=name, values=values))
        inverse.insert(0, operation("delete", section=name))
    return forward, inverse, ppps


def stage(router, operations):
    for op in operations:
        router.rpc("uci", op["method"], op["args"])


def save_recovery(operations, host):
    directory = Path(__file__).resolve().parent / "recovery"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (time.strftime("before-%Y%m%d-%H%M%S") + f"-{os.getpid()}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"format": 1, "host": host, "operations": operations}, out, indent=2)
    return path


def bridge_is_up(router, tagged, ppps):
    devices = router.rpc("luci-rpc", "getNetworkDevices")
    bridge = devices.get("br-bridge", {})
    expected = {"pon.20", "eth0.5.20" if tagged else "eth0.5"}
    if not bridge.get("up") or set(bridge.get("ports", [])) != expected:
        return False
    if "eth0.5" in devices.get("br-lan", {}).get("ports", []):
        return False
    if not all(devices.get(p, {}).get("up") for p in expected):
        return False
    if any(devices.get("pppoe-" + p, {}).get("up") for p in ppps):
        return False
    return True


def wait_for_success(deadline):
    print("\nConnect a SECOND device to the GL router and open a website.")
    print("Keep this computer connected to the Comtrend for management.")
    print("After PPPoE connects AND browsing works, type KEEP and press Enter.")
    print("Any other response, or no response before the deadline, restores the previous configuration.")
    remaining = max(0, deadline - time.monotonic())
    print(f"Time available for your check: {int(remaining)} seconds.")
    ready, _, _ = select.select([sys.stdin], [], [], remaining)
    return bool(ready) and sys.stdin.readline().strip() == "KEEP" and time.monotonic() < deadline


def apply_and_verify(router, operations, timeout, tagged=True, ppps=(), restore=False):
    staged = False
    apply_attempted = False
    kept = False
    try:
        # Set this before the first request: a timed-out write can still have succeeded.
        staged = True
        stage(router, operations)
        apply_attempted = True
        deadline = time.monotonic() + timeout - 30
        router.rpc("uci", "apply", {"timeout": timeout, "rollback": True})
        print(f"Configuration applied. Automatic rollback is active for {timeout} seconds.")
        time.sleep(5)
        if not restore:
            valid = False
            until = min(deadline, time.monotonic() + 40)
            while time.monotonic() < until:
                try:
                    valid = bridge_is_up(router, tagged, ppps)
                except Failure:
                    valid = False
                if valid:
                    break
                time.sleep(3)
            if not valid:
                raise Failure("The live bridge did not pass verification.")
            print("Live bridge verified: pon.20 to LAN5; local PPPoE stopped.")
        else:
            print("Recovery configuration applied. Check the expected connection before keeping it.")
        if restore:
            print("Type KEEP after you verify the recovered connection.")
            ready, _, _ = select.select([sys.stdin], [], [], max(0, deadline - time.monotonic()))
            accepted = bool(ready) and sys.stdin.readline().strip() == "KEEP"
        else:
            accepted = wait_for_success(deadline)
        if not accepted or time.monotonic() >= deadline:
            raise Failure("The connection was not verified before the deadline.")
        if not restore and not bridge_is_up(router, tagged, ppps):
            raise Failure("The bridge changed during the internet check.")
        router.rpc("uci", "confirm")
        kept = True
        print("SUCCESS: Configuration kept. Automatic rollback canceled.")
    finally:
        if not kept and staged:
            if apply_attempted:
                try:
                    router.rpc("uci", "rollback")
                    print("Previous configuration restored by the router.")
                except Failure:
                    print("Immediate rollback could not be verified. Leave the router powered on.")
                    print(f"If apply succeeded, its automatic rollback is due within {timeout} seconds.")
            try:
                router.rpc("uci", "revert", {"config": "network"})
            except Failure:
                print("Could not clear staged changes. Do not apply them from another interface.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.1.1", help="Private IPv4 router address")
    parser.add_argument("--dry-run", action="store_true", help="Login and inspect without writing configuration")
    parser.add_argument("--untagged", action="store_true", help="Use only when WAN VLAN tagging on the GL is disabled")
    parser.add_argument("--timeout", type=int, default=300, help="Rollback delay in seconds (120–600)")
    parser.add_argument("--restore", type=Path, help="Restore this script's recovery file")
    args = parser.parse_args()
    try:
        address = ipaddress.IPv4Address(args.host)
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_link_local:
            raise ValueError()
    except ValueError:
        parser.error("--host must be a private LAN IPv4 address")
    if not 120 <= args.timeout <= 600:
        parser.error("--timeout must be between 120 and 600")
    if not sys.stdin.isatty():
        parser.error("Run this script in an interactive terminal")
    print("Connect this computer to Comtrend Wi-Fi or LAN1–LAN4.")
    print("LAN5 (10G) must connect only to the GL WAN port.")
    print("Local HTTPS uses the router's self-signed certificate; certificate verification is disabled.")
    username = input("Current Comtrend username: ").strip()
    password = getpass.getpass("Current Comtrend password: ")
    router = Router(args.host)
    router.login(username, password)
    del password
    if router.rpc("uci", "changes").get("changes"):
        raise Failure("This session has pending changes. Resolve them before running this script.")
    access = router.rpc("session", "access", {"scope": "uci", "object": "network", "function": "write"})
    if not access.get("access"):
        raise Failure("This account cannot change the network configuration.")
    if args.restore:
        data = json.loads(args.restore.read_text())
        if data.get("format") != 1 or data.get("host") != args.host:
            raise Failure("The recovery file does not match this router address.")
        ops = data["operations"]
        for op in ops:
            if op.get("method") not in {"add", "set", "delete"} or op.get("args", {}).get("config") != "network":
                raise Failure("The recovery file contains an unsupported operation.")
        ppps = []
    else:
        network = router.network()
        mapping = router.rpc("uci", "get", {"config": "namemap"})["values"]
        ops, inverse, ppps = plan(network, mapping, tagged=not args.untagged)
        del network
        # Prove status access before any writes, since verification depends on it.
        router.rpc("luci-rpc", "getNetworkDevices")
        print("Plan: interface bridge, device br-bridge, fiber VLAN 20 to LAN5.")
        print("GL WAN: PPPoE, " + ("VLAN tagging disabled." if args.untagged else "VLAN 20 enabled."))
        print("Comtrend Wi-Fi stays available for management; internet moves to the GL.")
        if not args.dry_run:
            path = save_recovery(inverse, args.host)
            print(f"Recovery file (no credentials): {path}")
    if args.dry_run:
        print("Dry run passed. No router configuration changed.")
        return
    apply_and_verify(router, ops, args.timeout, tagged=not args.untagged,
                     ppps=ppps, restore=bool(args.restore))


if __name__ == "__main__":
    try:
        main()
    except (Failure, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Any unconfirmed apply remains subject to automatic rollback.", file=sys.stderr)
        sys.exit(130)
