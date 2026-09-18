#!/usr/bin/env python3
"""The virtual ether: one UDP endpoint that carries frames between stations.

Stations announce themselves with `hello`, describe their radio with `state`
and hand over a transmission with `tx`; the ether answers `welcome`,
`rx_begin` and `rx_end`. Physics is deliberately absent at this depth: there
are no positions, no path loss and no referee. A frame reaches every other
station whose latest state is `RX` on the same carrier, bandwidth, spreading
factor and sync word, at a constant level.

Clocks. A station's `t` fields are its own clock and are meaningful only
relative to each other inside one message; the ether rebases every frame onto
its own monotonic clock and schedules from that, so the `t` fields it sends
back are ether microseconds. The offsets inside a frame (`t_pre - t0`,
`t_hdr - t0`, `t_end - t0`) are the transmitter's and are carried through
unchanged, which is what a receiver actually needs.

Every datagram in and out is one tab-separated line of the record:
wall-clock timestamp, direction, station id, JSON.
"""

import argparse
import asyncio
import json
import random
import sys
from datetime import datetime, timezone

# What a receiver's state must share with a frame for the frame to be heard.
MATCH_KEYS = ("freq", "bw", "sf", "sync")

LEVEL_DBM = -80         # level reported in rx_begin
RSSI_DBM = -80          # rssi reported in rx_end
SNR_DB = 10             # snr reported in rx_end

MAX_FRAME_US = 60 * 1000 * 1000   # a stated timeline longer than this is junk


def wall_stamp():
    """The wall-clock timestamp that heads a record line."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def log(msg):
    """One line of human-readable running commentary."""
    sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], msg))
    sys.stderr.flush()


class Station:
    """A station the ether has heard from, and the radio state it last stated."""

    def __init__(self, sid, addr, slots):
        self.sid = sid
        self.addr = addr
        self.slots = slots
        self.states = {}        # slot -> the last `state` message for it

    def state(self, slot):
        return self.states.get(slot)

    def listening(self, slot):
        """True when this station's slot last said it was receiving."""
        st = self.states.get(slot)
        return bool(st) and st.get("mode") == "RX"


class Frame:
    """A transmission in flight, on the ether's own clock.

    `verdict` is shared by every receiver of the frame: a collision spoils it
    for all of them at once, and each scheduled rx_end reads it when it fires.
    """

    def __init__(self, fid, sid, msg, start_us, end_us, pre_us, hdr_us):
        self.fid = fid
        self.sid = sid
        self.freq = msg.get("freq")
        self.bw = msg.get("bw")
        self.sf = msg.get("sf")
        self.sync = msg.get("sync")
        self.payload = msg.get("payload", "")
        self.start_us = start_us
        self.end_us = end_us
        self.pre_us = pre_us
        self.hdr_us = hdr_us
        self.verdict = "clean"
        self.receivers = []     # (sid, slot) pairs that locked onto it

    def overlaps(self, other):
        """True when the two frames share the carrier and any instant of air."""
        return (self.freq == other.freq
                and self.start_us < other.end_us
                and other.start_us < self.end_us)


class Ether(asyncio.DatagramProtocol):
    """The UDP endpoint: parses station messages and delivers frames."""

    def __init__(self, record_path, seed=None):
        self.transport = None
        self.loop = asyncio.get_event_loop()
        self.stations = {}          # sid -> Station
        self.frames = []            # frames still in flight or just ended
        self.seed = seed if seed is not None else random.randrange(1 << 31)
        self.record = open(record_path, "a", encoding="utf-8", buffering=1)
        self.record.write("# %s\tether record: stamp\tdir\tsid\tjson\n" % wall_stamp())

    # ---- clock ---------------------------------------------------------

    def now(self):
        """The ether's own monotonic clock, in microseconds."""
        return int(self.loop.time() * 1_000_000)

    # ---- plumbing ------------------------------------------------------

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.write_record("in", None, {"raw": repr(data[:200])})
            return
        if not isinstance(msg, dict):
            return
        sid = msg.get("sid")
        self.write_record("in", sid, msg)
        kind = msg.get("type")
        if not isinstance(sid, int):
            return
        if kind == "hello":
            self.on_hello(sid, addr, msg)
        elif kind == "state":
            self.on_state(sid, addr, msg)
        elif kind == "tx":
            self.on_tx(sid, addr, msg)
        # Anything else is not ours to understand.

    def send(self, sid, msg):
        """Send one message to a station, at the address it last wrote from."""
        station = self.stations.get(sid)
        if station is None or self.transport is None:
            return
        self.write_record("out", sid, msg)
        self.transport.sendto(json.dumps(msg).encode("utf-8"), station.addr)

    def write_record(self, direction, sid, msg):
        self.record.write("%s\t%s\t%s\t%s\n" % (
            wall_stamp(), direction, "-" if sid is None else sid,
            json.dumps(msg, separators=(",", ":"), sort_keys=True)))

    # ---- station messages ----------------------------------------------

    def station_for(self, sid, addr, slots=None):
        """The station record for `sid`, created on first sight."""
        station = self.stations.get(sid)
        if station is None:
            station = Station(sid, addr, slots or [0])
            self.stations[sid] = station
            log("station %d joined from %s:%d" % (sid, addr[0], addr[1]))
        else:
            station.addr = addr
            if slots:
                station.slots = slots
        return station

    def on_hello(self, sid, addr, msg):
        slots = msg.get("slots") or [0]
        self.station_for(sid, addr, slots)
        self.send(sid, {"type": "welcome", "t0": self.now(),
                        "seed": self.seed, "mode": "real"})

    def on_state(self, sid, addr, msg):
        station = self.station_for(sid, addr)
        slot = msg.get("slot", 0)
        station.states[slot] = msg
        log("station %d slot %s %s freq=%s bw=%s sf=%s sync=%s" % (
            sid, slot, msg.get("mode"), msg.get("freq"), msg.get("bw"),
            msg.get("sf"), msg.get("sync")))

    def on_tx(self, sid, addr, msg):
        station = self.station_for(sid, addr)
        start = self.now()
        t0 = msg.get("t0", 0)
        span = max(0, min(int(msg.get("t_end", t0)) - int(t0), MAX_FRAME_US))
        pre = max(0, min(int(msg.get("t_pre", t0)) - int(t0), span))
        hdr = max(pre, min(int(msg.get("t_hdr", t0)) - int(t0), span))
        frame = Frame(msg.get("id"), sid, msg, start, start + span,
                      start + pre, start + hdr)

        self.prune(start)
        collided = [f for f in self.frames if f.overlaps(frame)]
        self.frames.append(frame)
        if collided:
            frame.verdict = "crc"
            for other in collided:
                other.verdict = "crc"
            log("frame %s from %d collides with %s on %s Hz" % (
                frame.fid, sid, [f.fid for f in collided], frame.freq))

        for rsid, rstation in self.stations.items():
            if rsid == sid:
                continue
            for slot in rstation.slots:
                if not rstation.listening(slot):
                    continue
                if not self.matches(rstation.state(slot), msg):
                    continue
                frame.receivers.append((rsid, slot))
                self.send(rsid, {"type": "rx_begin", "slot": slot,
                                 "id": frame.fid, "t0": frame.start_us,
                                 "t_pre": frame.pre_us, "t_hdr": frame.hdr_us,
                                 "t_end": frame.end_us, "level": LEVEL_DBM})
                self.loop.call_later(span / 1_000_000.0,
                                     self.deliver_end, frame, rsid, slot)

        log("frame %s from %d: %d us on %s Hz sf%s -> %s" % (
            frame.fid, sid, span, frame.freq, frame.sf,
            [r[0] for r in frame.receivers] or "nobody"))

    @staticmethod
    def matches(state, tx):
        """True when a receiver's stated radio can hear this transmission."""
        return all(state.get(k) == tx.get(k) for k in MATCH_KEYS)

    # ---- delivery -------------------------------------------------------

    def deliver_end(self, frame, rsid, slot):
        """Close out one receiver's reception of a frame, at its stated end."""
        self.send(rsid, {"type": "rx_end", "slot": slot, "id": frame.fid,
                         "t": self.now(), "verdict": frame.verdict,
                         "payload": frame.payload, "rssi": RSSI_DBM,
                         "snr": SNR_DB})
        log("frame %s from %d -> station %d slot %s: %s" % (
            frame.fid, frame.sid, rsid, slot, frame.verdict))

    def prune(self, now):
        """Drop frames whose air is long gone; collisions can no longer touch them."""
        self.frames = [f for f in self.frames if f.end_us > now]

    def close(self):
        if self.transport is not None:
            self.transport.close()
        self.record.close()


def parse_bind(text):
    """`host:port` into a tuple, with a bare port allowed."""
    if ":" in text:
        host, _, port = text.rpartition(":")
        return (host or "127.0.0.1", int(port))
    return ("127.0.0.1", int(text))


async def serve(bind, record_path):
    loop = asyncio.get_running_loop()
    transport, ether = await loop.create_datagram_endpoint(
        lambda: Ether(record_path), local_addr=bind)
    host, port = transport.get_extra_info("sockname")[:2]
    log("ether listening on %s:%d" % (host, port))
    log("recording to %s" % record_path)
    stop = loop.create_future()
    try:
        await stop
    finally:
        ether.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="the virtual ether")
    ap.add_argument("--bind", default="127.0.0.1:7000",
                    help="host:port to listen on (default 127.0.0.1:7000)")
    ap.add_argument("--record", default="record.tsv",
                    help="record file (default record.tsv in the current directory)")
    args = ap.parse_args(argv)
    try:
        asyncio.run(serve(parse_bind(args.bind), args.record))
    except KeyboardInterrupt:
        log("ether stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
