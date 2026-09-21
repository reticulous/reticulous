#!/usr/bin/env python3
"""Draw the ether's record as a sequence diagram: one lifeline per station.

Every frame in `record.tsv` becomes one row — an arrow from the station that
transmitted it to each station the ether told about it, with the verdict at
each arrow head and what the frame was on the right.

  seq.py                          the whole record of the loaded run
  seq.py --tail 40                the last forty frames
  seq.py --scenario run           lifelines named from the scenario
  seq.py --only announce,path     rows whose label matches any of these

The record is the ether's own account: it knows a frame's carrier, its air
time and its bytes, and nothing about what the bytes mean. The reading on the
right is this tool's, done the way the firmware's own receive path does it —
the RNode header byte, then a Reticulum packet's flags, hops, destination and
context.
"""

import argparse
import base64
import hashlib
import json
import os
import sys

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RECORD = os.path.join(SIM_DIR, "run", "record.tsv")
DEFAULT_SCENARIO = os.path.join(SIM_DIR, "run")

COL_W = 9               # characters between two lifelines
GUTTER = 3              # room to the left of the first lifeline for its name
TRUNC_HASH = 16         # a Reticulum address is a truncated hash, 16 bytes

# The verdict at an arrow head.
HEAD = {"clean": ("▶", "◀"), "crc": ("✗", "✗"),
        "hdr": ("✗", "✗"), "lost": ("·", "·")}

RNODE_FLAG_SPLIT = 0x01     # the air frame is half of a packet
MAGIC_PWRREQ = 0x04         # the four-byte power request that prefixes a frame
PWRREQ_LEN = 4
SUPE_TYPE = {0xC2: "HAIL", 0xC3: "ANNOUNCE", 0xC4: "GOT", 0xC5: "READY",
             0xC6: "END", 0xC7: "BYE", 0xC8: "RESEND"}

BENCH_MAX = 48              # printable frames this short are somebody's `lora tx`

PACKET_TYPE = {0: "DATA", 1: "ANNOUNCE", 2: "LINKREQUEST", 3: "PROOF"}
DEST_TYPE = {0: "single", 1: "group", 2: "plain", 3: "link"}
CONTEXT = {
    0x00: "", 0x01: "resource", 0x02: "resource-adv", 0x03: "resource-req",
    0x04: "resource-hmu", 0x05: "resource-prf", 0x06: "resource-icl",
    0x07: "resource-rcl", 0x08: "cache-request", 0x09: "request",
    0x0A: "response", 0x0B: "path-response", 0x0C: "command",
    0x0D: "command-status", 0x0E: "channel", 0xFA: "keepalive",
    0xFB: "link-identify", 0xFC: "link-close", 0xFD: "link-proof",
    0xFE: "link-rtt", 0xFF: "link-proof",
}

# The destinations this mesh names out loud. An announce carries the name hash
# of its aspect; a plain destination is addressed by the hash of that hash.
ASPECTS = [
    "lxmf.delivery", "lxmf.propagation", "lxmproxy.server",
    "nomadnetwork.node", "netgraph.discovery",
    "rnstransport.probe", "rnstransport.remote.management",
    "rnstransport.path.request", "rnstransport.tunnel.synthesize",
]


def name_hash(name):
    return hashlib.sha256(name.encode()).digest()[:10]


NAME_HASHES = {name_hash(n): n for n in ASPECTS}
PLAIN_DESTS = {hashlib.sha256(name_hash(n)).digest()[:TRUNC_HASH]: n
               for n in ASPECTS}


def hexid(raw):
    """A destination as a person reads it: the first four bytes."""
    return raw[:4].hex()


def read_frame(frame, part=0):
    """What one frame on the air is, in a line.

    An air frame is the RNode header byte — a sequence number and the split
    flag — and then a Reticulum packet, except for the frames the interface
    speaks for itself: SUPE, and the four-byte power request.
    """
    if not frame:
        return "empty frame"
    if len(frame) == PWRREQ_LEN and frame[0] == MAGIC_PWRREQ:
        return "power request  suggest %d dBm" % ((frame[1] ^ 0x80) - 0x80)
    if frame[0] in SUPE_TYPE:
        return "SUPE %-6s %dB" % (SUPE_TYPE[frame[0]], len(frame))
    if len(frame) <= BENCH_MAX and all(0x20 <= b < 0x7F for b in frame):
        return "bench frame  \"%s\"  %dB" % (frame.decode(), len(frame))
    split = " split %s" % ("1/2" if part == 1 else "2/2") if part else ""
    if part == 2:
        return "…continuation  %dB%s" % (len(frame), split)
    return read_packet(frame[1:]) + split


def read_packet(payload):
    """A Reticulum packet's header in one line, or what it is instead.

    The layout is the firmware's `rnsParse`: a flags byte, a hop count, one or
    two addresses, a context byte, then the data.
    """
    if not payload:
        return "empty packet"
    flags = payload[0]
    if flags & 0x80:
        return "%d B, IFAC-authenticated" % len(payload)
    hdr2 = bool(flags & 0x40)
    need = 2 + (32 if hdr2 else 16) + 1
    if len(payload) < need:
        return "%d B, not a Reticulum packet" % len(payload)
    ptype = PACKET_TYPE.get(flags & 0x03, "?")
    dtype = DEST_TYPE.get((flags >> 2) & 0x03, "?")
    ctxflag = bool(flags & 0x20)
    hops = payload[1]
    via = payload[2:2 + TRUNC_HASH] if hdr2 else None
    dest = payload[2 + (TRUNC_HASH if hdr2 else 0):][:TRUNC_HASH]
    ctx = payload[need - 1]
    data = payload[need:]

    where = PLAIN_DESTS.get(dest) or "%s/%s" % (dtype, hexid(dest))
    parts = ["%-11s %s" % (ptype, where)]
    if ptype == "ANNOUNCE" and len(data) >= 74:
        aspect = NAME_HASHES.get(data[64:74])
        parts.append("of %s" % (aspect or "?" + data[64:74][:4].hex()))
        if ctxflag:
            parts.append("+ratchet")
    elif CONTEXT.get(ctx):
        parts.append(CONTEXT[ctx])
    if via is not None:
        parts.append("via %s" % hexid(via))
    parts.append("hops=%d" % hops)
    parts.append("%dB" % len(payload))
    return "  ".join(parts)


class Frame:
    """One transmission, and what each station made of it."""

    def __init__(self, at, sender, msg):
        self.at = at
        self.sender = sender
        self.fid = msg.get("id")
        self.freq = msg.get("freq")
        self.sf = msg.get("sf")
        self.payload = base64.b64decode(msg.get("payload") or "")
        self.part = 0           # 1 or 2 when the packet was split over two frames
        self.heard = {}         # receiver sid -> verdict, "lost" until its rx_end

    @property
    def label(self):
        return read_frame(self.payload, self.part)


def parse_time(stamp):
    """Seconds out of a record stamp, wrapping at the hour is nobody's problem."""
    clock = stamp.split("T")[-1]
    hour, minute, second = clock.split("+")[0].split("Z")[0].split(":")
    return int(hour) * 3600 + int(minute) * 60 + float(second)


def read_record(path):
    """The record as a list of frames, in the order they went on the air."""
    frames = []
    sent = None                 # the transmission being told to its receivers
    arriving = {}               # the ether's frame id -> that transmission
    halves = set()              # senders with the first half of a split packet
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 4:
                continue
            stamp, direction, sid, blob = fields
            try:
                msg = json.loads(blob)
            except ValueError:
                continue
            kind = msg.get("type")
            if direction == "in" and kind == "tx":
                frame = Frame(parse_time(stamp), int(sid), msg)
                if frame.payload and frame.payload[0] & RNODE_FLAG_SPLIT \
                        and frame.payload[0] not in SUPE_TYPE:
                    frame.part = 2 if frame.sender in halves else 1
                    halves.symmetric_difference_update({frame.sender})
                frames.append(frame)
                sent = frame
                continue
            if direction != "out" or kind not in ("rx_begin", "rx_end"):
                continue
            # The ether tells a frame's receivers about it as it reads the
            # transmission, so the frame a reception belongs to is the one
            # just sent; its id, the ether's own, is what the end comes back
            # under however many frames are in the air.
            if kind == "rx_begin":
                if sent is None:
                    continue    # a record that begins mid-frame
                arriving[msg.get("id")] = sent
                sent.heard[int(sid)] = "lost"
            else:
                frame = arriving.get(msg.get("id"))
                if frame is not None:
                    frame.heard[int(sid)] = msg.get("verdict", "?")
    return frames


def lifelines(columns):
    """A blank row: every station's line and nothing on it."""
    row = [" "] * ((len(columns) - 1) * COL_W + 1)
    for index in range(len(columns)):
        row[index * COL_W] = "│"
    return row


def draw(frame, columns):
    """The rows for one frame: one arrow per station that heard it.

    One transmission heard by two stations is two arrows, not one line drawn
    through the transmitter — a line across three lifelines reads as a frame
    passing from the first station to the last, which is the one thing the
    diagram must never say.
    """
    here = columns[frame.sender]
    rows = []
    for sid in sorted(frame.heard, key=lambda s: columns.get(s, -1)):
        if sid not in columns:
            continue
        there = columns[sid]
        row = lifelines(columns)
        lo, hi = min(here, there), max(here, there)
        for x in range(lo * COL_W, hi * COL_W + 1):
            if row[x] == " ":
                row[x] = "─"
        for index in range(lo + 1, hi):
            row[index * COL_W] = "┼"    # a lifeline the arrow passes
        row[here * COL_W] = "├" if there > here else "┤"
        head = HEAD.get(frame.heard[sid], HEAD["lost"])
        row[there * COL_W] = head[0 if there > here else 1]
        rows.append("".join(row))
    return rows or ["".join(lifelines(columns))]


def header(columns, names):
    """The station heading above the lifelines, each name over its own line."""
    row = [" "] * ((len(columns) - 1) * COL_W + 1 + 2 * GUTTER)
    for sid, index in columns.items():
        label = names.get(sid, str(sid))[:COL_W - 1]
        start = max(0, GUTTER + index * COL_W - len(label) // 2)
        row[start:start + len(label)] = list(label)
    return "".join(row).rstrip()


def read_scenario(path):
    """Station id to node name, out of a scenario file.

    A record is numbers — the ether knows stations by id and nothing else — so
    the diagram's lifelines are named by the scenario that ran, which is the
    only thing that knows what those numbers were called.
    """
    if not path:
        return {}
    if os.path.isdir(path):
        path = os.path.join(path, "scenario.yaml")
    try:
        import yaml
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, ImportError, ValueError):
        return {}
    return {int(node["id"]): name
            for name, node in (data.get("nodes") or {}).items()
            if isinstance(node, dict) and "id" in node}


def parse_names(text):
    names = {}
    for item in (text or "").split(","):
        if not item.strip():
            continue
        sid, _, name = item.partition("=")
        names[int(sid)] = name or sid
    return names


def main(argv=None):
    ap = argparse.ArgumentParser(description="the ether's record as a sequence diagram")
    ap.add_argument("--record", default=DEFAULT_RECORD, help="the record to read")
    ap.add_argument("--names", help="lifeline names, e.g. 1=alpha,2=bravo,3=charlie")
    ap.add_argument("--tail", type=int, metavar="N", help="only the last N frames")
    ap.add_argument("--only", metavar="WORDS",
                    help="only frames whose reading contains one of these,"
                         " comma separated (case-insensitive)")
    ap.add_argument("--scenario", metavar="PATH", default=DEFAULT_SCENARIO,
                    help="the scenario the record came from: names the "
                         "lifelines and prints where the stations stand "
                         "(default: the loaded run)")
    args = ap.parse_args(argv)

    frames = read_record(args.record)
    if args.only:
        wanted = [w.strip().lower() for w in args.only.split(",") if w.strip()]
        frames = [f for f in frames if any(w in f.label.lower() for w in wanted)]
    if args.tail:
        frames = frames[-args.tail:]
    if not frames:
        sys.stderr.write("no frames in %s\n" % args.record)
        return 1

    placed = read_scenario(args.scenario)
    names = {**placed, **parse_names(args.names)}
    seen = sorted({f.sender for f in frames} | {r for f in frames for r in f.heard})
    columns = {sid: index for index, sid in enumerate(seen)}
    origin = frames[0].at

    print("%8s  %s" % ("t (s)", header(columns, names)))
    for frame in frames:
        rows = draw(frame, columns)
        print("%8.3f  %s%s  %s" % (
            frame.at - origin, " " * GUTTER, rows[0],
            frame.label + ("" if frame.heard else "   → nobody")))
        for row in rows[1:]:
            # The same frame, at another station: no stamp, no second reading.
            print("%8s  %s%s" % ("", " " * GUTTER, row))
    print("\n▶ received  ✗ CRC failure  · reception never closed out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
