"""Mesh smoke test: five stations with stated links. Do they boot, hear each
other, relay for each other and deliver a message?

Stages, stopping at the first that fails:

  1  boot   every station starts and reports itself up
  2  hear   leaf-a and leaf-b each announce themselves; afterwards each one's
            path table has an entry for the other at a distance of one hop,
            which means each received the other's announce directly over
            the radio
  3  relay  leaf-c announces itself once; within a few minutes leaf-b's
            path table has an entry for leaf-c at two hops (through relay-2)
            and leaf-a's has one at three hops (through relay-1 and
            relay-2), which means both relays forwarded the announce
  4  deliver  one message is sent along each route in turn: leaf-a to
            leaf-b (direct), leaf-b to leaf-c (one relay), leaf-a to leaf-c
            (two relays); each sender is told its message was delivered, which
            means the recipient's proof came back to it

Made for the nodeset `three-leaves-two-relays` (beside this file) on the geodata
`plain-27`. When a stage fails the script stops the simulation and raises a
ScriptError naming the stage, which makes `sim run` exit with status 1.
Every stage prints one line, PASS or FAIL.

What each stage found is also written to `smoke.json` in the run directory as
the run goes, and `report(run_dir)` makes the run's report from it. A run
that fails writes no report of its own (the script has raised by then):
`sim run mesh-smoke-test.py --report RUN_DIR` writes it afterwards, which is
what `tests/run` does.
"""
import json
import os

from sim_mesh import *
from sim_mesh import library

firmware = script_input("firmware", type=Firmware, category="reticulum",
                        label="Firmware for every node")
settings = script_input("settings", label="Settings for every node: key=value …, or none",
                        default="none")

ROUNDS = 3        # announces tried before stage 2 fails: one lost frame is no fault
ROUND_S = 30      # seconds of T each of those rounds is given
POLL_S = 10       # seconds of T between two looks, in stages 3 and 4
RELAY_S = 300     # seconds of T an announce is given to cross both relays: a relay
                  # spends 2% of its airtime forwarding announces, nearest first
DELIVER_S = 300   # seconds of T a message is given to be proven to its sender
SENDS = 2         # messages tried per route: one lost frame is not a failure

RESULT_FILE = "smoke.json"
STAGES = ("boot", "hear", "relay", "deliver")
ROUTES = (("leaf-a", "leaf-b"), ("leaf-b", "leaf-c"), ("leaf-a", "leaf-c"))
RELAY_HOPS = {("leaf-b", "leaf-c"): 2, ("leaf-a", "leaf-c"): 3}

result = {"firmware": str(firmware), "passed": False, "stages": []}


def run_dir():
    return library.runtime.held().run_dir


def dump():
    with open(os.path.join(run_dir(), RESULT_FILE), "w") as handle:
        json.dump(result, handle, indent=1)


def stage(number, name, ok, detail, **figures):
    """One stage's verdict: printed, kept in the result, and the end of the
    run when it failed."""
    print("stage %d %-7s %s  %s" % (number, name, "PASS" if ok else "FAIL", detail))
    result["stages"].append(dict({"stage": number, "name": name, "ok": bool(ok),
                                  "detail": detail}, **figures))
    result["passed"] = bool(ok) and number == len(STAGES)
    dump()
    if not ok:
        sim_stop()
        raise ScriptError("stage %d (%s) failed: %s" % (number, name, detail))


def hops(frm, to):
    """How many hops `frm`'s path table gives to `to`, or None when it has no entry."""
    paths = node(frm).reticulum.path(to=to)[frm]
    return paths[0].get("hops") if paths else None


def hops_text(pairs):
    return "; ".join("%s -> %s: %s" % (f, t, "no path" if h is None else "%s hop(s)" % h)
                     for (f, t), h in pairs.items())


def messages():
    """What the run's events say of each message sent, by its id: its last
    status, and how many seconds of T lay between its first status and that
    one."""
    first, last = {}, {}
    with open(os.path.join(run_dir(), "events.jsonl")) as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("event") == "lxmf.message.status":
                first.setdefault(event["mid"], event)
                last[event["mid"]] = event
    return {mid: (last[mid]["status"], (last[mid]["t"] - first[mid]["t"]) / 1e6)
            for mid in last}


sim_speed("max")
nodes().firmware(firmware)
script_include("scripts/startup.py")
nodes(tag="leaf").on_first_boot(Node.reticulum.role("client"))
nodes().on_first_boot(Node.reticulum.lxmf.create())

# 1 boot
wanted = sorted(nodes().facts())
up = sorted(nodes().up(timeout=120))
pairs = [p.split("=", 1) for p in settings.split() if settings != "none"]
took = len(up)
if pairs:
    nodes().exec(["set %s %s" % (k, v) for k, v in pairs])
    shown = nodes().exec(["show %s" % k for k, _ in pairs])
    took = sum(all("%s=%s" % (k, v) in reply.replace(" ", "") for k, v in pairs)
               for reply in shown.values())
    if any(k.endswith("SUPE.enable") and v == "1" for k, v in pairs):
        for _ in range(12):
            supe = nodes().exec("lora 0 supe")
            running = sum("SUPE: on" in reply for reply in supe.values())
            if running == len(up):
                break
            sim_wait(5)
        took = min(took, running)
stage(1, "boot", up == wanted and took == len(up),
      "%d of %d up%s" % (len(up), len(wanted),
                         "; settings on %d of %d" % (took, len(up)) if pairs else ""),
      up=len(up), wanted=len(wanted), settings_on=took)

# 2 hear
a, b = ROUTES[0]
seen = {}
for round in range(1, ROUNDS + 1):
    node(a).reticulum.lxmf.announce()
    node(b).reticulum.lxmf.announce()
    sim_wait(ROUND_S)
    seen = {(a, b): hops(a, b), (b, a): hops(b, a)}
    if all(h == 1 for h in seen.values()):
        break
stage(2, "hear", all(h == 1 for h in seen.values()),
      hops_text(seen) + " after %d announce(s)" % round, announces=round)

# 3 relay
seen, took = {}, {}
# Announced once: another announce would only queue ahead of this one at the relays.
node("leaf-c").reticulum.lxmf.announce()
for waited in range(POLL_S, RELAY_S + 1, POLL_S):
    sim_wait(POLL_S)
    seen = {pair: hops(*pair) for pair in RELAY_HOPS}
    for pair, want in RELAY_HOPS.items():
        if seen[pair] == want:
            took.setdefault("%s -> %s" % pair, waited)
    if seen == RELAY_HOPS:
        break
stage(3, "relay", seen == RELAY_HOPS,
      hops_text(seen) + " (want %s) after %d s"
      % (" and ".join(str(n) for n in RELAY_HOPS.values()), waited),
      seconds=took, limit=RELAY_S)

# 4 deliver: one route at a time, so the routes don't collide at the relays
def deliver(route):
    f, t = route
    mid, tries, status, took = None, 0, "nothing reported", None
    for _ in range(0, DELIVER_S, POLL_S):
        if mid is None or (status == "failed" and tries < SENDS):
            mid = node(f).reticulum.lxmf.send(t, "smoke test %s to %s" % (f, t))[f]
            tries += 1
        sim_wait(POLL_S)
        status, took = messages().get(mid, ("nothing reported", None))
        if status == "delivered":
            break
    return status, took, tries

got = {route: deliver(route) for route in ROUTES}
stage(4, "deliver", all(status == "delivered" for status, _, _ in got.values()),
      "; ".join("%s -> %s: %s%s%s" % (f, t, got[f, t][0],
                                      "" if got[f, t][1] is None else " in %.1f s" % got[f, t][1],
                                      "" if got[f, t][2] == 1 else " (message %d)" % got[f, t][2])
                for f, t in ROUTES),
      seconds={"%s -> %s" % route: got[route][1] for route in ROUTES},
      messages={"%s -> %s" % route: got[route][2] for route in ROUTES}, limit=DELIVER_S)

sim_stop()


def report(run_dir):
    path = os.path.join(run_dir, RESULT_FILE)
    if not os.path.isfile(path):
        return "# Mesh smoke test\n\nNo result: the run ended before its first stage.\n"
    with open(path) as handle:
        found = json.load(handle)
    done = {row["name"]: row for row in found["stages"]}
    lines = ["# Mesh smoke test", "",
             "**%s** on `%s`." % ("PASS" if found.get("passed") else "FAIL", found["firmware"]),
             "", "| stage | verdict | what it found |", "|---|---|---|"]
    for number, name in enumerate(STAGES, 1):
        row = done.get(name)
        lines.append("| %d %s | %s | %s |" % (
            number, name, "not run" if row is None else "PASS" if row["ok"] else "FAIL",
            "" if row is None else row["detail"]))
    return "\n".join(lines) + "\n"
