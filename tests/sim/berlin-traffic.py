"""Runs sim-mesh's msg-traffic scenario on the berlin-50 nodeset and outputs
the traffic data for test evaluation."""
import json
import os
import re

import yaml

from sim_mesh import *
from sim_mesh import library

script_include("scripts/msg-traffic.py")

import airtime as airtime_tool
import delivery as delivery_tool

run_dir = library.runtime.held().run_dir
# msg-traffic's inputs
inputs = library.runtime.inputs
delivery_json = os.path.join(run_dir, "delivery.json")
airtime_json = os.path.join(run_dir, "airtime.json")
delivery_tool.main([os.path.join(run_dir, "traffic.json"), run_dir, "--json", delivery_json])
airtime_tool.main([run_dir, "--json", airtime_json])
with open(delivery_json) as handle:
    d = json.load(handle)
with open(airtime_json) as handle:
    a = json.load(handle)
with open(os.path.join(run_dir, "traffic.json")) as handle:
    traffic_seed = json.load(handle)["args"]["seed"]
with open(os.path.join(run_dir, "run.yaml")) as handle:
    sim_seed = (yaml.safe_load(handle) or {}).get("seed")


def quantiles(prefix, q):
    if not q:
        return {}
    return {prefix + ".p50": q["median"], prefix + ".p90": q["p90"],
            prefix + ".max": q["max"], prefix + ".mean": q["mean"]}


def slug(kind):
    return re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_")


def by_kind(prefix, kinds):
    out = {}
    for kind, v in sorted(kinds.items(), key=lambda kv: -kv[1]["s"]):
        out["%s.%s.airtime_s" % (prefix, slug(kind))] = v["s"]
        out["%s.%s.frames" % (prefix, slug(kind))] = v["frames"]
    return out


calling = a.get("by_kind_calling", {})
traffic = {k: {"frames": v["frames"] - calling.get(k, {}).get("frames", 0),
               "s": v["s"] - calling.get(k, {}).get("s", 0.0)}
           for k, v in a.get("by_kind", {}).items()}
traffic = {k: v for k, v in traffic.items() if v["frames"]}

sent = d["sent"]
results = {
    "run.dir": run_dir,
    "run.sim_seed": sim_seed,
    "run.traffic_seed": traffic_seed,
    "run.firmware": str(inputs["firmware"]),
    "run.repeater_firmware": str(inputs["repeater_firmware"]),
    "run.settings": inputs["reticulous_settings"],
    "delivery.sent": sent,
    "delivery.delivered": d["delivered"],
    "delivery.proven": d["proven"],
    "delivery.delivered_ratio": d["delivered"] / sent if sent else 0.0,
    "delivery.proven_ratio": d["proven"] / sent if sent else 0.0,
    **quantiles("latency.delivered_s", d.get("latency_s")),
    # TODO ldub: proof latency looks wrong (p50 ~100 s in every variant while
    # delivery p50 is 4-6 s); left out until we check how delivery.py times proofs.
    # **quantiles("latency.proven_s", d.get("proof_latency_s")),
    "airtime.frames": a.get("frames"),
    "airtime.window_s": a.get("window_s"),
    "airtime.announce_s": a.get("announce_s"),
    "airtime.other_s": a.get("other_s"),
    "airtime.calling_s": a["airtime"]["calling_s"],
    "airtime.traffic_channels_s": a["airtime"]["traffic_channels_s"],
    **by_kind("airtime.calling", calling),
    **by_kind("airtime.traffic", traffic),
}
with open(os.path.join(run_dir, "results.json"), "w") as handle:
    json.dump(results, handle, indent=1)
print("results: delivered %(delivery.delivered)d/%(delivery.sent)d, proven %(delivery.proven)d" % results)
