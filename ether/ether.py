#!/usr/bin/env python3
"""The virtual ether: one UDP endpoint that carries frames between stations.

Stations announce themselves with `hello`, describe their radio with `state`
and hand over a transmission with `tx`; the ether answers `welcome`,
`rx_begin` and `rx_end`. A frame reaches every other station that is close
enough for it to rise out of the noise and whose latest state is `RX` on the
same carrier, bandwidth, spreading factor and sync word. Two frames sharing a
carrier and any instant of air interfere, and each receiver decides the
outcome for itself: the stronger frame survives when it leads the other by the
capture margin.

The level a frame arrives at is computed, not stated. Every station has a
position in metres and an antenna gain, every pair may have an obstruction in
dB, and

    L = P_tx + G_tx + G_rx − PL(d)
    PL(d) = FSPL(1 m, f) + 10·n·log10(d) + obstruction(tx, rx)

Positions arrive by direct call — `place()` and `obstruct()` — from whatever
is driving the run; the UDP wire to the stations never mentions them, and a
station never learns where it is.

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
import math
import os
import random
import sys
from datetime import datetime, timezone

# What a receiver's state must share with a frame for the frame to be heard.
MATCH_KEYS = ("freq", "bw", "sf", "sync")

DEFAULT_EXPONENT = 2.7      # suburban; 2 is free space
DEFAULT_NOISE_FIGURE_DB = 6
DEFAULT_CAPTURE_DB = 6      # how far a frame must lead an interferer to survive it
DEFAULT_POWER_DBM = 14      # a `tx` that did not say what it was sent at

# The SNR a spreading factor needs before its receiver detects a preamble at
# all, from the SX1262 datasheet: -2.5 dB at SF5 and 2.5 dB lower per step. A
# frame that arrives under its own modem's threshold is not delivered — no
# rx_begin goes out — and that is what "out of range" means here.
#
# It is per spreading factor because that is the whole point of one: SF12 buys
# 17.5 dB of reach over SF7 and pays for it in air time. A flat threshold would
# make the two identical to the medium, and a scenario that moved SF to reach
# further would see no change at all.
SENSITIVITY_DB = {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0,
                  9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
SLOWEST_SENSITIVITY_DB = -20.0      # a frame that named no spreading factor

MIN_DISTANCE_M = 1.0        # two stations at one point are still a metre apart
EARTH_RADIUS_M = 6371008.8  # the mean radius, for the equirectangular projection

MAX_FRAME_US = 60 * 1000 * 1000   # a stated timeline longer than this is junk

SPEED_OF_LIGHT = 299_792_458.0


def wall_stamp():
    """The wall-clock timestamp that heads a record line."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def log(msg):
    """One line of human-readable running commentary."""
    sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], msg))
    sys.stderr.flush()


def project(origin, lat, lon):
    """Degrees of latitude and longitude to metres east and north of `origin`.

    Equirectangular around the origin: the scale in longitude is the origin's
    own cosine rather than each point's, so the plane is flat and distances
    between two points on it are what the path loss is computed from. Over the
    tens of kilometres a LoRa scenario spans the error is far below anything
    the medium models.
    """
    lat0, lon0 = origin
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_RADIUS_M
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def unproject(origin, x, y):
    """Metres east and north of `origin` back to latitude and longitude."""
    lat0, lon0 = origin
    lat = lat0 + math.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return lat, lon


def fspl_1m_db(freq_hz):
    """Free-space path loss over the first metre at this carrier, in dB."""
    return 20.0 * math.log10(4.0 * math.pi * max(freq_hz, 1.0) / SPEED_OF_LIGHT)


class Physics:
    """The scenario's constants: how fast the air eats a signal, and the noise."""

    def __init__(self, exponent=DEFAULT_EXPONENT,
                 noise_figure_db=DEFAULT_NOISE_FIGURE_DB,
                 capture_db=DEFAULT_CAPTURE_DB):
        self.exponent = float(exponent)
        self.noise_figure_db = float(noise_figure_db)
        self.capture_db = float(capture_db)

    def describe(self):
        return "exponent %.2f, noise figure %.1f dB, capture margin %.1f dB" % (
            self.exponent, self.noise_figure_db, self.capture_db)

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        return cls(data.get("exponent", DEFAULT_EXPONENT),
                   data.get("noise_figure_db", DEFAULT_NOISE_FIGURE_DB),
                   data.get("capture_db", DEFAULT_CAPTURE_DB))

    def as_dict(self):
        return {"exponent": self.exponent,
                "noise_figure_db": self.noise_figure_db,
                "capture_db": self.capture_db}


class Placement:
    """Where a station stands, and what its antenna adds."""

    def __init__(self, x_m, y_m, gain_db=0.0):
        self.x_m = float(x_m)
        self.y_m = float(y_m)
        self.gain_db = float(gain_db)


class Station:
    """A station the ether has heard from, and the radio state it last stated."""

    def __init__(self, sid, addr, slots):
        self.sid = sid
        self.addr = addr
        self.slots = slots
        self.states = {}        # slot -> the last `state` message for it
        self.tx_until = 0       # while its own frame is on the air, it is deaf
        # The power its last frame went out at. A station states this per
        # transmission rather than in its `state`, so it is learned by
        # listening — and it is what `levels()` must answer with, or the map
        # would draw a reach the station does not have.
        self.power_dbm = DEFAULT_POWER_DBM

    def state(self, slot):
        return self.states.get(slot)

    def listening(self, slot):
        """True when this station's slot last said it was receiving."""
        st = self.states.get(slot)
        return bool(st) and st.get("mode") == "RX"


class Frame:
    """A transmission in flight, on the ether's own clock.

    A frame carries the other frames it shared air with. The verdict is not
    one of its properties: each receiver reads that list at its own rx_end,
    against the levels its own position gives, and may keep a frame the
    station beside it lost.
    """

    def __init__(self, eid, fid, sid, msg, start_us, end_us, pre_us, hdr_us):
        self.eid = eid          # the ether's own number, unique across stations
        self.fid = fid          # the transmitter's, unique only to it
        self.sid = sid
        self.freq = msg.get("freq")
        self.bw = msg.get("bw")
        self.sf = msg.get("sf")
        self.sync = msg.get("sync")
        self.power_dbm = msg.get("power_dbm", DEFAULT_POWER_DBM)
        self.payload = msg.get("payload", "")
        self.start_us = start_us
        self.end_us = end_us
        self.pre_us = pre_us
        self.hdr_us = hdr_us
        self.interferers = []   # frames that shared this one's carrier and air
        self.receivers = []     # (sid, slot, level) for each station that locked on

    def overlaps(self, other):
        """True when the two frames share the carrier and any instant of air."""
        return (self.freq == other.freq
                and self.start_us < other.end_us
                and other.start_us < self.end_us)


class Ether(asyncio.DatagramProtocol):
    """The UDP endpoint: parses station messages and delivers frames.

    Whatever drives a run places the stations and subscribes to `on_tx`,
    `on_rx` and `on_station` to watch the air. Each callback takes the
    arguments named beside it below and returns nothing; exceptions raised in
    one are logged and swallowed, because a page that has gone away must not
    be able to stop the medium.
    """

    def __init__(self, record_path, physics=None, seed=None):
        self.transport = None
        self.loop = asyncio.get_event_loop()
        self.physics = physics or Physics()
        self.stations = {}          # sid -> Station
        self.places = {}            # sid -> Placement, whether or not it has joined
        self.obstructions = {}      # frozenset({a, b}) -> dB
        self.frames = []            # frames still in flight or just ended
        self.next_eid = 0           # the ether's own frame numbering
        self.seed = seed if seed is not None else random.randrange(1 << 31)
        self.on_tx = None           # (sid, eid, freq, t_start, t_end)
        self.on_rx = None           # (sid, rsid, eid, verdict, level)
        self.on_station = None      # (sid, state)
        self.record = None
        if record_path is not None:
            self.record = open(record_path, "a", encoding="utf-8", buffering=1)
            self.record.write("# %s\tether record: stamp\tdir\tsid\tjson\n"
                              % wall_stamp())

    # ---- clock ---------------------------------------------------------

    def now(self):
        """The ether's own monotonic clock, in microseconds."""
        return int(self.loop.time() * 1_000_000)

    # ---- the arrangement of the network ---------------------------------

    def place(self, sid, x_m, y_m, gain_db=0.0):
        """Put a station at a point on the plane, in metres."""
        self.places[sid] = Placement(x_m, y_m, gain_db)

    def unplace(self, sid):
        """Take a station off the plane; it is deaf and inaudible from then on."""
        self.places.pop(sid, None)
        self.obstructions = {pair: db for pair, db in self.obstructions.items()
                             if sid not in pair}

    def obstruct(self, a, b, db):
        """Put `db` of extra loss between one pair, in both directions."""
        pair = frozenset((a, b))
        if db:
            self.obstructions[pair] = float(db)
        else:
            self.obstructions.pop(pair, None)

    def clear(self):
        """Forget every position and obstruction, for a scenario being replaced."""
        self.places.clear()
        self.obstructions.clear()

    def distance(self, a, b):
        """Metres between two placed stations, never less than one."""
        pa, pb = self.places.get(a), self.places.get(b)
        if pa is None or pb is None:
            return None
        return max(MIN_DISTANCE_M, math.hypot(pa.x_m - pb.x_m, pa.y_m - pb.y_m))

    def path_loss(self, a, b, freq_hz):
        """The dB between two stations at this carrier, or None if either is off-plane."""
        d = self.distance(a, b)
        if d is None:
            return None
        loss = fspl_1m_db(freq_hz) + 10.0 * self.physics.exponent * math.log10(d)
        return loss + self.obstructions.get(frozenset((a, b)), 0.0)

    def level(self, tx_sid, rx_sid, freq_hz, power_dbm=DEFAULT_POWER_DBM):
        """The level in dBm a frame from `tx_sid` arrives at `rx_sid`, or None."""
        loss = self.path_loss(tx_sid, rx_sid, freq_hz)
        if loss is None:
            return None
        return (float(power_dbm) + self.places[tx_sid].gain_db
                + self.places[rx_sid].gain_db - loss)

    def noise(self, bw_hz):
        """A receiver's noise floor in dBm for this bandwidth: kTB plus the figure."""
        return -174.0 + 10.0 * math.log10(max(float(bw_hz or 125_000), 1.0)) \
            + self.physics.noise_figure_db

    def sensitivity(self, sf):
        """The SNR this spreading factor needs to detect a frame, in dB."""
        return SENSITIVITY_DB.get(sf, SLOWEST_SENSITIVITY_DB)

    def audible(self, level, bw_hz, sf=None):
        """True when a frame at this level is one this modem can detect.

        The threshold is the receiver's noise floor plus what the spreading
        factor needs above it — so the same frame that a SF12 receiver hears
        comfortably is silence to a SF7 one, which is the trade a spreading
        factor is.
        """
        if level is None:
            return False
        return level >= self.noise(bw_hz) + self.sensitivity(sf)

    def levels(self, sid, freq_hz, bw_hz=125_000, power_dbm=None, sf=None):
        """What every other placed station would hear from `sid`, as sid -> dBm.

        Only the ones actually within earshot: the rest are what "no link"
        means here, and saying so is the point of asking.

        The power is the one that station last transmitted at, so the answer
        describes the station as it is rather than as a constant assumed it
        would be. A station that has never transmitted has no such figure and
        is asked about at the default.
        """
        station = self.stations.get(sid)
        if power_dbm is None:
            power_dbm = station.power_dbm if station else DEFAULT_POWER_DBM
        if sf is None and station is not None:
            state = station.state(0)
            sf = state.get("sf") if state else None
        heard = {}
        for other in self.places:
            if other == sid:
                continue
            level = self.level(sid, other, freq_hz, power_dbm)
            if self.audible(level, bw_hz, sf):
                heard[other] = level
        return heard

    # ---- events ---------------------------------------------------------

    def raise_event(self, callback, *args):
        """Hand one event to a subscriber; a broken subscriber is not the medium's
        problem, so it is logged and the frame carries on."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as err:                # noqa: BLE001 - see docstring
            log("subscriber raised %r" % (err,))

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
            self.recv_hello(sid, addr, msg)
        elif kind == "state":
            self.recv_state(sid, addr, msg)
        elif kind == "tx":
            self.recv_tx(sid, addr, msg)
        # Anything else is not ours to understand.

    def send(self, sid, msg):
        """Send one message to a station, at the address it last wrote from."""
        station = self.stations.get(sid)
        if station is None or self.transport is None:
            return
        self.write_record("out", sid, msg)
        self.transport.sendto(json.dumps(msg).encode("utf-8"), station.addr)

    def write_record(self, direction, sid, msg):
        if self.record is None:
            return
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

    def recv_hello(self, sid, addr, msg):
        slots = msg.get("slots") or [0]
        self.station_for(sid, addr, slots)
        self.send(sid, {"type": "welcome", "t0": self.now(),
                        "seed": self.seed, "mode": "real"})

    def recv_state(self, sid, addr, msg):
        station = self.station_for(sid, addr)
        slot = msg.get("slot", 0)
        station.states[slot] = msg
        log("station %d slot %s %s freq=%s bw=%s sf=%s sync=%s" % (
            sid, slot, msg.get("mode"), msg.get("freq"), msg.get("bw"),
            msg.get("sf"), msg.get("sync")))
        self.raise_event(self.on_station, sid, msg)

    def recv_tx(self, sid, addr, msg):
        station = self.station_for(sid, addr)
        start = self.now()
        t0 = msg.get("t0", 0)
        span = max(0, min(int(msg.get("t_end", t0)) - int(t0), MAX_FRAME_US))
        pre = max(0, min(int(msg.get("t_pre", t0)) - int(t0), span))
        hdr = max(pre, min(int(msg.get("t_hdr", t0)) - int(t0), span))
        self.next_eid += 1
        frame = Frame(self.next_eid, msg.get("id"), sid, msg, start, start + span,
                      start + pre, start + hdr)

        self.prune(start)
        for other in [f for f in self.frames if f.overlaps(frame)]:
            frame.interferers.append(other)
            other.interferers.append(frame)
            log("frame %d from %d shares air with frame %d from %d on %s Hz" % (
                frame.eid, sid, other.eid, other.sid, frame.freq))
        self.frames.append(frame)
        station.tx_until = frame.end_us
        station.power_dbm = frame.power_dbm
        self.raise_event(self.on_tx, sid, frame.eid, frame.freq,
                         frame.start_us, frame.end_us)

        for rsid, rstation in self.stations.items():
            if rsid == sid:
                continue
            if rstation.tx_until > start:
                continue        # half duplex: its own frame is still going out
            level = self.level(sid, rsid, frame.freq, frame.power_dbm)
            if not self.audible(level, frame.bw, frame.sf):
                continue        # too far below this receiver's noise to exist
            for slot in rstation.slots:
                if not rstation.listening(slot):
                    continue
                if not self.matches(rstation.state(slot), msg):
                    continue
                frame.receivers.append((rsid, slot, level))
                self.send(rsid, {"type": "rx_begin", "slot": slot,
                                 "id": frame.eid, "t0": frame.start_us,
                                 "t_pre": frame.pre_us, "t_hdr": frame.hdr_us,
                                 "t_end": frame.end_us, "level": round(level)})
                self.loop.call_later(span / 1_000_000.0,
                                     self.deliver_end, frame, rsid, slot, level)

        log("frame %d (station's %s) from %d: %d us on %s Hz sf%s -> %s" % (
            frame.eid, frame.fid, sid, span, frame.freq, frame.sf,
            ["%d@%.1fdBm" % (r[0], r[2]) for r in frame.receivers] or "nobody"))

    @staticmethod
    def matches(state, tx):
        """True when a receiver's stated radio can hear this transmission."""
        return all(state.get(k) == tx.get(k) for k in MATCH_KEYS)

    # ---- delivery -------------------------------------------------------

    def verdict_for(self, frame, rsid, level):
        """How this frame ends at one receiver, given what else was in the air.

        Everything this station could hear on the carrier at the same instant
        is interference. The frame survives only by leading all of it by the
        capture margin — so a receiver near one transmitter keeps its frame
        while a receiver that hears both equally keeps neither.
        """
        for other in frame.interferers:
            against = self.level(other.sid, rsid, other.freq, other.power_dbm)
            if not self.audible(against, other.bw, other.sf):
                continue        # this receiver never heard the other frame
            if level - against < self.physics.capture_db:
                return "crc"
        return "clean"

    def deliver_end(self, frame, rsid, slot, level):
        """Close out one receiver's reception of a frame, at its stated end."""
        verdict = self.verdict_for(frame, rsid, level)
        self.send(rsid, {"type": "rx_end", "slot": slot, "id": frame.eid,
                         "t": self.now(), "verdict": verdict,
                         "payload": frame.payload, "rssi": round(level),
                         "snr": round(level - self.noise(frame.bw))})
        log("frame %d from %d -> station %d slot %s: %s at %.1f dBm" % (
            frame.eid, frame.sid, rsid, slot, verdict, level))
        self.raise_event(self.on_rx, rsid, frame.sid, frame.eid, verdict, level)

    def prune(self, now):
        """Drop frames whose air is long gone; collisions can no longer touch them."""
        self.frames = [f for f in self.frames if f.end_us > now]

    def close(self):
        if self.transport is not None:
            self.transport.close()
        if self.record is not None:
            self.record.close()


def parse_bind(text):
    """`host:port` into a tuple, with a bare port allowed."""
    if ":" in text:
        host, _, port = text.rpartition(":")
        return (host or "127.0.0.1", int(port))
    return ("127.0.0.1", int(text))


def read_scenario(path):
    """A scenario file's physics, placements and obstructions, for a run alone.

    `path` is a `scenario.yaml` or the directory holding one. Positions are
    latitude and longitude in the file and metres by the time they are placed,
    which is the one conversion the ether does on the way in.
    """
    import yaml                 # only a standalone run reads a file

    if os.path.isdir(path):
        path = os.path.join(path, "scenario.yaml")
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    origin = tuple(data.get("origin") or (0.0, 0.0))
    physics = Physics.from_dict(data.get("physics"))
    places, names = {}, {}
    for name, node in (data.get("nodes") or {}).items():
        sid = int(node["id"])
        lat, lon = (node.get("pos") or (0.0, 0.0))[:2]
        x, y = project(origin, float(lat), float(lon))
        places[sid] = (x, y, float(node.get("gain_db", 0.0)))
        names[name] = sid
    obstructions = []
    for item in data.get("obstructions") or []:
        a, b = item["between"]
        if a in names and b in names:
            obstructions.append((names[a], names[b], float(item.get("db", 0))))
    return physics, places, obstructions


async def serve(bind, record_path, physics, places, obstructions):
    loop = asyncio.get_running_loop()
    transport, ether = await loop.create_datagram_endpoint(
        lambda: Ether(record_path, physics), local_addr=bind)
    for sid, (x, y, gain) in places.items():
        ether.place(sid, x, y, gain)
    for a, b, db in obstructions:
        ether.obstruct(a, b, db)
    host, port = transport.get_extra_info("sockname")[:2]
    log("ether listening on %s:%d" % (host, port))
    log("recording to %s" % record_path)
    log("physics: %s" % physics.describe())
    log("stations placed: %s" % (", ".join(
        "%d at (%.0f, %.0f) m" % (sid, x, y) for sid, (x, y, _) in
        sorted(places.items())) or "none — nothing can hear anything"))
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
    ap.add_argument("--scenario", metavar="PATH",
                    help="a scenario directory or scenario.yaml: where the "
                         "stations stand (default: nowhere, so nothing is heard)")
    args = ap.parse_args(argv)
    physics, places, obstructions = Physics(), {}, []
    if args.scenario:
        physics, places, obstructions = read_scenario(args.scenario)
    try:
        asyncio.run(serve(parse_bind(args.bind), args.record,
                          physics, places, obstructions))
    except KeyboardInterrupt:
        log("ether stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
