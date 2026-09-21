#!/usr/bin/env python3
"""The WebRTC relay: one UDP port in front of every station's DataChannel.

A station speaks real WebRTC — the same ICE, DTLS and SCTP a board speaks,
from the same source. What it cannot do here is be reached: its candidate is
`127.0.0.1<id>:4433`, an address inside the container, and the browser is
outside it with one published port to work with.

So simd stands in the middle, in the two places ICE needs it:

```
browser ──ws /webrtc──► simd ──ws /webrtc──► station      signalling, rewritten
browser ──udp──────────► simd ──udp────────► station      the DataChannel itself
```

**Signalling.** simd terminates the signalling WebSocket on both sides rather
than pumping bytes, so the SDP answer arrives as a parsed JSON message. It
rewrites the answer's connection line and host candidate to the relay's own
address, and remembers the answer's `ice-ufrag` as the name of that station's
half of the session.

**Media.** The first packet of an ICE session is a STUN binding request whose
USERNAME is `<answerer ufrag>:<offerer ufrag>` — so the ufrag learned during
signalling is what says which station a packet belongs to. After that the
browser's address is pinned to a flow and everything is forwarded both ways.

Nothing about this is visible to the station: it is answering ICE from a peer
that happens to be a relay, which is what a relay is for. And nothing about it
is visible to the browser either, which is the point — the code under test is
the shipping code, on both sides.
"""

import asyncio
import re
import socket
import struct
import time

STUN_MAGIC = 0x2112A442
STUN_USERNAME = 0x0006
STUN_HEADER = 20

FLOW_IDLE_S = 120.0         # how long a silent flow is kept before it is dropped
SWEEP_S = 30.0

log = None                  # set by simd, so the relay logs where everything else does


def _log(msg):
    if log is not None:
        log("relay: %s" % msg)


# ---- STUN ----------------------------------------------------------------

def stun_username(data):
    """The USERNAME of a STUN binding request, or None if it is not one.

    Only enough parsing to read one attribute: the relay is not a STUN server
    and has no business validating anything. What it needs is the name on the
    envelope, and it forwards the envelope untouched either way.
    """
    if len(data) < STUN_HEADER:
        return None
    kind, length, magic = struct.unpack("!HHI", data[:8])
    if magic != STUN_MAGIC or kind & 0xC000:
        return None            # not STUN, or not a request/response at all
    at, end = STUN_HEADER, min(len(data), STUN_HEADER + length)
    while at + 4 <= end:
        attr, alen = struct.unpack("!HH", data[at:at + 4])
        at += 4
        if attr == STUN_USERNAME:
            try:
                return data[at:at + alen].decode("utf-8")
            except UnicodeDecodeError:
                return None
        at += (alen + 3) & ~3   # attributes are padded to four bytes
    return None


def station_ufrag(username):
    """The answering station's ufrag out of a STUN USERNAME."""
    return username.split(":", 1)[0] if username else None


# ---- the SDP answer ------------------------------------------------------

UFRAG_RE = re.compile(r"^a=ice-ufrag:(\S+)", re.MULTILINE)
CONNECTION_RE = re.compile(r"^c=IN IP4 \S+", re.MULTILINE)
MEDIA_RE = re.compile(r"^(m=application )(\d+)( .*)$", re.MULTILINE)
# A whole line, terminator and all: a line removed without its newline leaves
# a blank one, and a browser reads a blank line in an SDP as a broken one.
CANDIDATE_RE = re.compile(r"^a=candidate:[^\r\n]*\r?\n?", re.MULTILINE)


def rewrite_answer(sdp, host, port):
    """Point a station's SDP answer at the relay instead of at itself.

    The station is ICE-lite and offers host candidates for the addresses it
    knows about, all of which are inside the container. Every one of them is
    replaced by a single candidate for the relay — the browser has exactly one
    way in, and offering it addresses it cannot reach only costs it timeouts.
    """
    sdp = CONNECTION_RE.sub("c=IN IP4 %s" % host, sdp)
    sdp = MEDIA_RE.sub(lambda m: "%s%d%s" % (m.group(1), port, m.group(3)), sdp)
    sdp = CANDIDATE_RE.sub("", sdp)
    if not sdp.endswith("\r\n"):
        sdp += "\r\n"
    candidate = ("a=candidate:1 1 UDP 2130706431 %s %d typ host\r\n" % (host, port))
    return sdp + candidate


def answer_ufrag(sdp):
    found = UFRAG_RE.search(sdp)
    return found.group(1) if found else None


# ---- the relay -----------------------------------------------------------

class Flow:
    """One browser's path to one station, and the socket that carries it."""

    def __init__(self, browser_addr, station_addr):
        self.browser_addr = browser_addr
        self.station_addr = station_addr
        self.transport = None
        self.seen = time.monotonic()


class _FlowProtocol(asyncio.DatagramProtocol):
    """The station-facing half of one flow: whatever comes back goes out."""

    def __init__(self, relay, flow):
        self.relay = relay
        self.flow = flow

    def datagram_received(self, data, addr):
        self.flow.seen = time.monotonic()
        self.relay.to_browser(self.flow, data)

    def error_received(self, err):
        pass


class Relay(asyncio.DatagramProtocol):
    """The browser-facing UDP socket, and the flows behind it."""

    def __init__(self, advertise_host, advertise_port):
        self.advertise_host = advertise_host
        self.advertise_port = advertise_port
        self.transport = None
        self.stations = {}          # station ufrag -> (host, port)
        self.flows = {}             # browser addr -> Flow
        self.sweeper = None

    # ---- what signalling teaches it ----

    def learn(self, ufrag, station_host, station_port):
        """Tie a station's ICE ufrag to where its DataChannel really is."""
        self.stations[ufrag] = (station_host, station_port)

    def forget(self, ufrag):
        self.stations.pop(ufrag, None)

    # ---- the socket ----

    def connection_made(self, transport):
        self.transport = transport
        self.sweeper = asyncio.ensure_future(self.sweep())

    def datagram_received(self, data, addr):
        flow = self.flows.get(addr)
        if flow is None:
            ufrag = station_ufrag(stun_username(data))
            station = self.stations.get(ufrag) if ufrag else None
            if station is None:
                return          # nothing has claimed this session; drop it
            flow = Flow(addr, station)
            self.flows[addr] = flow
            asyncio.ensure_future(self.open_flow(flow, data))
            return
        flow.seen = time.monotonic()
        if flow.transport is not None:
            flow.transport.sendto(data)

    async def open_flow(self, flow, first):
        """Give a flow its own socket to the station and send what started it.

        Its own socket, because the station answers to whatever address the
        packet came from: one socket per flow is what keeps two browsers
        talking to one station apart.
        """
        loop = asyncio.get_running_loop()
        try:
            flow.transport, _ = await loop.create_datagram_endpoint(
                lambda: _FlowProtocol(self, flow), remote_addr=flow.station_addr)
        except OSError as err:
            _log("cannot reach %s:%d (%s)" % (*flow.station_addr, err))
            self.flows.pop(flow.browser_addr, None)
            return
        _log("flow %s:%d -> %s:%d" % (*flow.browser_addr, *flow.station_addr))
        flow.transport.sendto(first)

    def to_browser(self, flow, data):
        if self.transport is not None:
            self.transport.sendto(data, flow.browser_addr)

    async def sweep(self):
        """Drop flows nothing has used, so a long session does not accumulate."""
        while True:
            await asyncio.sleep(SWEEP_S)
            now = time.monotonic()
            for addr, flow in list(self.flows.items()):
                if now - flow.seen > FLOW_IDLE_S:
                    if flow.transport is not None:
                        flow.transport.close()
                    del self.flows[addr]

    def close(self):
        if self.sweeper is not None:
            self.sweeper.cancel()
        for flow in self.flows.values():
            if flow.transport is not None:
                flow.transport.close()
        self.flows.clear()
        if self.transport is not None:
            self.transport.close()


async def serve(bind_host, bind_port, advertise_host, advertise_port):
    """Open the relay's public UDP socket and give back the relay."""
    loop = asyncio.get_running_loop()
    transport, relay = await loop.create_datagram_endpoint(
        lambda: Relay(advertise_host, advertise_port),
        local_addr=(bind_host, bind_port), family=socket.AF_INET)
    return relay
