"""Offline sanity check for plan()/enabled_pppoe_on_pon() — no network calls."""
import sys
sys.path.insert(0, ".")
from enable_bridge import plan, enabled_pppoe_on_pon, Failure

MAPPING = {"port": {"portmapping": ["LAN5(10G):eth0.5"]}}

BASE = {
    "lan": {".type": "interface", "device": "br-lan", "ipaddr": "192.168.1.1"},
    "cfg_lan": {".type": "device", "name": "br-lan", "type": "bridge",
                "ports": ["eth0.1", "eth0.2", "eth0.3", "eth0.4"]},
    "pon20dev": {".type": "device", "name": "pon.20", "ifname": "pon", "vid": "20"},
}

# --- Scenario A: original documented case (existing local PPPoE on pon.20) ---
a = dict(BASE)
a["wan"] = {".type": "interface", "proto": "pppoe", "device": "pon.20"}
forward, inverse, ppps = plan(a, MAPPING)
assert ppps == ["wan"], ppps
assert any(op["args"].get("section") == "wan" for op in forward if op["method"] == "set")
print("Scenario A OK:", len(forward), "forward ops, ppps =", ppps)

# --- Scenario B: native "bridge mode" wizard already bridged pon.20 into br-lan ---
b = dict(BASE)
b["cfg_lan"] = dict(BASE["cfg_lan"])
b["cfg_lan"]["ports"] = ["eth0.1", "eth0.2", "eth0.3", "eth0.4", "pon.20"]
b["ppp1"] = {".type": "interface", "proto": "none", "device": "pon.20"}  # inert placeholder
forward, inverse, ppps = plan(b, MAPPING)
assert ppps == [], ppps
lan_set = [op for op in forward if op["method"] == "set" and op["args"]["section"] == "cfg_lan"]
assert lan_set and "pon.20" not in lan_set[0]["args"]["values"]["ports"], lan_set
bridge_add = [op for op in forward if op["method"] == "add" and op["args"]["values"].get("name") == "br-bridge"]
assert bridge_add and "pon.20" in bridge_add[0]["args"]["values"]["ports"]
print("Scenario B OK:", len(forward), "forward ops, ppps =", ppps)

# --- Scenario C: neither — must fail closed ---
c = dict(BASE)
try:
    plan(c, MAPPING)
    print("Scenario C FAILED: expected Failure, got none")
    sys.exit(1)
except Failure as exc:
    print("Scenario C OK (correctly refused):", exc)

# --- enabled_pppoe_on_pon(): ignores disabled and non-pon interfaces ---
net = {
    "a": {"proto": "pppoe", "device": "pon.20"},
    "b": {"proto": "pppoe", "device": "pon.20", "disabled": "1"},
    "c": {"proto": "pppoe", "device": "eth0.6"},
    "d": {"proto": "pppoe", "device": "@a"},  # alias resolves to pon.20 too
}
found = enabled_pppoe_on_pon(net)
assert set(found) == {"a", "d"}, found
print("enabled_pppoe_on_pon OK:", found)

print("\nAll offline checks passed.")
