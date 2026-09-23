#!/usr/bin/env python3
"""The simulated testbed as one process: ether, stations, proxy and control.

One command starts this, and this is the whole testbed. It holds

- the `Ether` and its UDP endpoint, in this process's own event loop, so
  positions reach the medium by direct call and never over a wire;
- the stations, one firmware process on a pty each, with a supervisor apiece;
- one listener on the bind port, which routes by `Host`: a
  `<name|id>.sim.localhost` request is proxied to that station's own web UI,
  and everything else is the control page and its websockets;
- the loaded scenario, the run directory it was copied into, and whether it
  has been changed since.

The page gets one `snapshot` on connect and deltas after it; everything a
person does to the network is one message the other way. See README.md for
the protocol and INTERNALS.md for why it is shaped this way.

Nothing here blocks. Every wait is an awaitable, the stations' CLI is spoken
over asyncio streams with a timeout, and a pty is a reader on the loop.
"""

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys

import aiohttp
from aiohttp import WSMsgType, web

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
BUILDABLE = os.path.dirname(SIM_DIR)
sys.path.insert(0, SIM_DIR)
sys.path.insert(0, os.path.join(BUILDABLE, "ether"))

import ether as ether_module       # noqa: E402 - the paths are set just above
import proxy                       # noqa: E402
import scenario as scenario_module  # noqa: E402
import setup as setup_module       # noqa: E402
import stations as stations_module  # noqa: E402
import webrtc as webrtc_module   # noqa: E402

DEFAULT_ELF = os.path.join(BUILDABLE, "esp-idf", "build.linux", "reticulous.elf")
DEFAULT_FIXED = os.path.join(BUILDABLE, "esp-idf", "build.linux", "data_merged")
UI_DIST = os.path.join(SIM_DIR, "ui", "dist", "spa")

TRANSPORT_KEY = "s.rnsd.transport_enabled"
TRANSPORT_POLL_S = 6.0      # how often a station is asked whether it forwards
SETUP_TIMEOUT_S = 90.0      # how long a fresh station has to answer its CLI
SHUTDOWN_TIMEOUT_S = 5.0    # how long a connection may hold up a stop
FLUSH_TIMEOUT_S = 8.0       # how long a station gets to commit its store
SETTLE_S = 15.0             # how long a first boot gets to finish landing what setup asked for
WEBRTC_PORT = 4433          # the station's own DataChannel port (s.net.webrtc_port)
SIGNAL_PATH = "/webrtc"     # the one station route simd keeps for itself

log = stations_module.log


# ---------------------------------------------------------------------------
# The process
# ---------------------------------------------------------------------------

class Simd:
    """The testbed: the medium, the stations, the scenario and the page."""

    def __init__(self, args):
        self.args = args
        self.elf = args.elf
        self.fixed = args.fixed
        self.ether_addr = args.ether
        self.ether = None
        self.ether_transport = None
        self.scenario = None                # a `Scenario`, or None until loaded
        self.stations = {}                  # node name -> Station
        self.pages = set()                  # the control websockets
        self.consoles = {}                  # node name -> set of websockets
        self.poller = None
        self.stopping = False
        self.control_port = None            # where the aiohttp app really listens
        self.front = None
        self.runner = None
        self.relay = None                   # the WebRTC UDP relay
        self.starter = None                 # the staggered start, while it runs

    # ---- lookups ---------------------------------------------------------

    def name_of(self, sid):
        """The node name behind a station id, or None if the scenario has none."""
        if self.scenario is None:
            return None
        for name, node in self.scenario.nodes.items():
            if node["id"] == sid:
                return name
        return None

    def node_for_label(self, label):
        """The node behind a `<label>.sim.localhost`, by name or by number."""
        if self.scenario is None or label is None:
            return None
        node = self.scenario.nodes.get(label)
        if node is None and label.isdigit():
            node = next((n for n in self.scenario.nodes.values()
                         if n["id"] == int(label)), None)
        return node

    def resolve_host(self, label, path=""):
        """Where a request with this `Host` label goes: a station, or simd.

        A label is a name or an id out of `<label>.sim.localhost`; anything
        else — `localhost`, an IP, no Host at all — is the control page, which
        is why one port serves both.

        One station route is simd's own: `/webrtc`, the DataChannel's
        signalling. simd stands in the middle of it so it can point the SDP
        answer at the relay, because the address the station would offer is
        inside the container and the browser is outside it.
        """
        if label is None:
            return ("127.0.0.1", self.control_port)
        node = self.node_for_label(label)
        if node is None:
            return None
        if path.split("?", 1)[0] == SIGNAL_PATH:
            return ("127.0.0.1", self.control_port)
        return (stations_module.bind_addr(node["id"]), proxy.STATION_PORT)

    # ---- the medium ------------------------------------------------------

    async def start_ether(self):
        loop = asyncio.get_running_loop()
        record = os.path.join(scenario_module.RUN_DIR, "record.tsv")
        os.makedirs(scenario_module.RUN_DIR, exist_ok=True)
        bind = ether_module.parse_bind(self.ether_addr)
        self.ether_transport, self.ether = await loop.create_datagram_endpoint(
            lambda: ether_module.Ether(record), local_addr=bind)
        self.ether.on_tx = self.ether_tx
        self.ether.on_rx = self.ether_rx
        self.ether.on_station = self.ether_station
        log("ether on %s, recording to %s" % (self.ether_addr, record))

    def apply_to_ether(self):
        """Put the loaded scenario's geometry into the medium, wholesale.

        Cheap enough to redo outright on every change: a scenario is tens of
        nodes, and a partial update is a chance for the map and the medium to
        disagree about where something is.
        """
        self.ether.clear()
        if self.scenario is None:
            return
        self.ether.physics = ether_module.Physics.from_dict(self.scenario.physics)
        origin = self.scenario.origin
        for node in self.scenario.nodes.values():
            x, y = ether_module.project(origin, node["pos"][0], node["pos"][1])
            self.ether.place(node["id"], x, y, node.get("gain_db", 0.0))
        for wall in self.scenario.obstructions:
            a, b = wall["between"]
            nodes = self.scenario.nodes
            if a in nodes and b in nodes:
                self.ether.obstruct(nodes[a]["id"], nodes[b]["id"], wall["db"])

    def ether_tx(self, sid, eid, freq, t_start, t_end):
        name = self.name_of(sid)
        if name is not None:
            self.broadcast({"type": "tx", "name": name, "eid": eid, "freq": freq,
                            "t_start": t_start, "t_end": t_end})

    def ether_rx(self, sid, from_sid, eid, verdict, level):
        name, sender = self.name_of(sid), self.name_of(from_sid)
        if name is not None and sender is not None:
            self.broadcast({"type": "rx", "name": name, "from": sender,
                            "eid": eid, "verdict": verdict, "level": round(level, 1)})

    def ether_station(self, sid, state):
        """A station said what its radio is doing; the map draws only the carrier."""
        name = self.name_of(sid)
        if name is None:
            return
        self.broadcast({"type": "radio", "name": name, "mode": state.get("mode"),
                        "freq": state.get("freq"), "sf": state.get("sf"),
                        "bw": state.get("bw")})

    # ---- stations --------------------------------------------------------

    def make_station(self, name):
        node = self.scenario.node(name)
        return stations_module.Station(
            name, node["id"], self.scenario.node_dir(name),
            self.elf, self.fixed, self.ether_addr,
            on_status=self.station_status, on_output=self.station_output)

    def station_status(self, station, status):
        self.broadcast(self.node_message(station.name))

    def station_output(self, station, data):
        """Console bytes go to whoever has that station's terminal open."""
        text = data.decode("utf-8", "replace")
        for socket in list(self.consoles.get(station.name, ())):
            asyncio.ensure_future(self.send_console(socket, text))

    async def send_console(self, socket, text):
        try:
            await socket.send_str(text)
        except (ConnectionError, RuntimeError):
            pass

    async def after_start(self, station):
        """Wait for a station's CLI, and set it up if it has never been set up.

        A station with state is left alone: its state is the scenario's, and
        re-running the lines would be re-answering questions it has already
        answered. A station without is a node somebody has just put on the
        map, and it should be a working station rather than a dot waiting for
        someone to type.
        """
        if not await setup_module.wait_until_up(station.node_id, SETUP_TIMEOUT_S):
            log("station %s never answered its CLI" % station.name)
            return
        if not station.was_configured:
            station.set_status(stations_module.SETUP)
            try:
                await self.send_setup(station)
            except setup_module.CliError as err:
                self.error("setting up %s: %s" % (station.name, err))
        station.set_status(stations_module.UP)
        await self.read_transport(station)
        if not station.was_configured:
            # Not everything the setup lines ask for lands at once: an LXMF
            # identity reaches the store some seconds after the command that
            # created it has returned. One more flush once that has settled, so
            # a station reset moments after its first boot keeps what it was
            # given rather than coming back half-configured.
            await asyncio.sleep(SETTLE_S)
            await self.flush_station(station)

    async def send_setup(self, station):
        """The scenario's lines then the node's own, with the macros filled in.

        simd adds no *settings* of its own: the lines in the file are the whole
        of what a station is told, including its hostname, because `{name}` in
        a shared line is what lets one list say node-specific things.

        It does add `save`. The store coalesces writes for
        `s.storage.flash_delay` seconds — a minute by default — so a station
        set up and then reset inside that window comes back with none of it. On
        a board that is a power cut and fair; here, Reset is a button a person
        presses the moment a node comes up, and a testbed that lost its own
        setup that way would be lying about what it had configured. `save` is
        not a setting, which is why it is simd's to send and not the file's.
        """
        lines = self.scenario.lines_for(station.name)
        if lines:
            await setup_module.send(station.node_id, lines + ["save"])

    async def start_station(self, name):
        station = self.make_station(name)
        self.stations[name] = station
        station.run(self.after_start)
        self.broadcast(self.node_message(name))

    async def flush_station(self, station):
        """Ask a station to commit its store before we take it away from it.

        The store coalesces writes for a minute by default, and some of what a
        station records — an LXMF identity among them — lands there a little
        after the command that asked for it. So anything that stops or resets a
        station flushes it first, and so does taking a snapshot: a snapshot
        copied out of a store with a minute of writes still in RAM would be a
        picture of a moment that never quite existed.

        Best effort. A station that will not answer is one whose store we
        cannot flush, and refusing to stop it over that would be worse.
        """
        if station.status not in (stations_module.UP, stations_module.SETUP):
            return
        with contextlib.suppress(setup_module.CliError):
            await setup_module.ask(station.node_id, "save", timeout=FLUSH_TIMEOUT_S)

    async def flush_all(self):
        await asyncio.gather(*(self.flush_station(s) for s in self.stations.values()),
                             return_exceptions=True)

    async def stop_station(self, name, flush=True):
        station = self.stations.pop(name, None)
        if station is None:
            return
        if flush:
            await self.flush_station(station)
        await station.stop()

    async def start_all(self):
        """Start every station that is not running, spread over a minute.

        Not all at once. Two dozen firmware processes forking in the same
        instant is a thundering herd against one host, and the network it
        produces is worse than the load: stations that boot together finish
        booting together, so their first announces land on top of each other
        and the opening minute is a collision storm no real fleet powered up by
        hand would ever have. Spreading the starts costs nothing and makes the
        first minute of the air look like a first minute.
        """
        if self.scenario is None:
            return
        pending = [name for name in self.scenario.nodes
                   if name not in self.stations]
        if not pending:
            return
        gap = self.args.stagger / len(pending)
        for index, name in enumerate(pending):
            if index:
                await asyncio.sleep(gap)
            # A minute is long enough for the scenario to have moved on.
            if self.stopping or self.scenario is None:
                return
            if name in self.scenario.nodes and name not in self.stations:
                await self.start_station(name)

    def begin_start_all(self):
        """Run the staggered start in the background.

        The caller is handling a message from the page, and a start that took
        a minute to return would hold every other message behind it for that
        minute. Held as a task so the next Load or Stop can cancel it rather
        than race it.
        """
        self.cancel_start()
        self.starter = asyncio.ensure_future(self.start_all())

    def cancel_start(self):
        if self.starter is not None:
            self.starter.cancel()
            self.starter = None

    async def stop_all(self, flush=True):
        self.cancel_start()
        await asyncio.gather(*(self.stop_station(name, flush)
                               for name in list(self.stations)))

    # ---- the transport poll ----------------------------------------------

    async def read_transport(self, station):
        """Ask a station whether it is forwarding for others, and say so.

        Read live rather than taken from the scenario, because the setting is
        live: a person can flip it in that station's own web UI, and the map
        should show it without the scenario knowing.
        """
        try:
            reply = await setup_module.ask(station.node_id,
                                           "show %s" % TRANSPORT_KEY)
        except setup_module.CliError:
            return
        value = setup_module.parse_setting(reply, TRANSPORT_KEY)
        transport = value not in (None, "0", "")
        if transport != station.transport:
            station.transport = transport
            self.broadcast(self.node_message(station.name))

    async def poll_transport(self):
        while not self.stopping:
            await asyncio.sleep(TRANSPORT_POLL_S)
            running = [s for s in self.stations.values()
                       if s.status == stations_module.UP]
            if running:
                await asyncio.gather(*(self.read_transport(s) for s in running),
                                     return_exceptions=True)

    # ---- talking to the page ---------------------------------------------

    def radio_of(self, sid):
        """The station's last stated radio, so a page that connects late has it.

        A station states its radio when it changes and not otherwise, so a page
        opened on a quiet network would wait for the next change to learn what
        anything is tuned to. The ether already holds the last one.
        """
        station = self.ether.stations.get(sid) if self.ether else None
        state = station.state(0) if station else None
        if not state:
            return {}
        return {"mode": state.get("mode"), "freq": state.get("freq"),
                "sf": state.get("sf"), "bw": state.get("bw")}

    def node_message(self, name):
        node = self.scenario.nodes.get(name) if self.scenario else None
        station = self.stations.get(name)
        if node is None:
            return {"type": "node_gone", "name": name}
        return {"type": "node", "name": name, "id": node["id"],
                "pos": list(node["pos"]), "gain_db": node.get("gain_db", 0.0),
                "setup": list(node.get("setup") or []),
                "status": station.status if station else stations_module.STOPPED,
                "transport": station.transport if station else None,
                **self.radio_of(node["id"])}

    def snapshot(self):
        return {"type": "snapshot",
                "scenario": self.scenario.as_dict() if self.scenario else None,
                "scenarios": scenario_module.scenarios(),
                "snapshots": scenario_module.snapshots(),
                "nodes": [self.node_message(name)
                          for name in (self.scenario.nodes if self.scenario else ())],
                "port": self.args.public_port}

    def broadcast(self, message):
        text = json.dumps(message)
        for socket in list(self.pages):
            asyncio.ensure_future(self.send_page(socket, text))

    async def send_page(self, socket, text):
        try:
            await socket.send_str(text)
        except (ConnectionError, RuntimeError):
            self.pages.discard(socket)

    def error(self, text):
        log("error: %s" % text)
        self.broadcast({"type": "error", "text": text})

    def scenario_changed(self):
        self.broadcast({"type": "scenario",
                        **(self.scenario.as_dict() if self.scenario else
                           {"name": None, "dirty": False}),
                        "scenarios": scenario_module.scenarios(),
                        "snapshots": scenario_module.snapshots()})

    # ---- what the page asks for ------------------------------------------

    async def handle(self, msg):
        """One message from the page. Anything unknown is ignored, as on the wire."""
        kind = msg.get("type")
        handler = getattr(self, "do_" + kind, None) if kind else None
        if handler is None:
            return
        try:
            await handler(msg)
        except (scenario_module.ScenarioError, setup_module.CliError) as err:
            self.error(str(err))
        except OSError as err:
            self.error("%s: %s" % (kind, err))

    def need_scenario(self):
        if self.scenario is None:
            raise scenario_module.ScenarioError("no scenario is loaded")
        return self.scenario

    async def do_node_add(self, msg):
        sc = self.need_scenario()
        node = sc.add_node(msg["name"], msg["pos"])
        sc.flush()
        self.apply_to_ether()
        self.scenario_changed()
        await self.start_station(msg["name"])
        log("node %s added as station %d" % (msg["name"], node["id"]))

    async def do_node_move(self, msg):
        sc = self.need_scenario()
        sc.move_node(msg["name"], msg["pos"])
        self.apply_to_ether()
        # A drag sends these at a few Hz; the file is written when it settles,
        # which `settle` says it has.
        if msg.get("settle", True):
            sc.flush()
        self.scenario_changed()
        self.broadcast(self.node_message(msg["name"]))

    async def do_node_remove(self, msg):
        sc = self.need_scenario()
        name = msg["name"]
        await self.stop_station(name)
        sc.remove_node(name)
        sc.flush()
        self.apply_to_ether()
        self.scenario_changed()
        self.broadcast({"type": "node_gone", "name": name})

    async def do_node_reset(self, msg):
        """Press reset: the process goes, the supervisor brings it back.

        Its state store is untouched, so it comes back as the station it was —
        which is what a reset button does and the whole of why this is not
        called anything else.
        """
        station = self.stations.get(msg["name"])
        if station is None:
            await self.start_station(msg["name"])
        else:
            await self.flush_station(station)
            await station.restart()

    async def do_node_factory_reset(self, msg):
        """Throw away a station's state and start it again from the lines.

        Stopped rather than reset, because the state has to go while nothing is
        holding it; on the way back up the directory is empty, which is exactly
        the station a node clicked onto the map for the first time is, so the
        setup lines run again by the ordinary path and not by a special case.
        """
        sc = self.need_scenario()
        name = msg["name"]
        sc.node(name)
        await self.stop_station(name, flush=False)
        scenario_module.wipe_state(sc.run_dir, name)
        await self.start_station(name)
        log("factory reset %s" % name)

    async def do_factory_reset_all(self, msg):
        sc = self.need_scenario()
        await self.stop_all(flush=False)
        scenario_module.wipe_state(sc.run_dir)
        self.begin_start_all()
        log("factory reset the whole testbed")

    async def do_reset_all(self, msg):
        """Press reset on every station, spread over the start window.

        Pressing them all at once is the same collision storm `start_all`
        spreads out, and for the same reason: every station comes up and
        announces itself into the same air, so the mesh spends its first
        minutes talking over itself and half the fleet learns nothing. The
        stagger is what a fleet of real boards has for free.
        """
        await self.flush_all()
        self.cancel_start()
        self.starter = asyncio.ensure_future(self.restart_all())

    async def restart_all(self):
        stations = list(self.stations.values())
        if not stations:
            return
        gap = self.args.stagger / len(stations)
        for index, station in enumerate(stations):
            if index:
                await asyncio.sleep(gap)
            if self.stopping:
                return
            await station.restart()

    async def do_command(self, msg):
        """Run one CLI line on every running station and report what each said.

        The macros are expanded per station, so `lxmf create {name}` or
        `hostname {name}` does the right thing across the whole testbed in one
        go — which is what makes this general enough to have replaced a verb
        that only ever re-sent the setup lines.
        """
        sc = self.need_scenario()
        line = (msg.get("line") or "").strip()
        if not line:
            return
        targets = [(name, s) for name, s in self.stations.items()
                   if s.status in (stations_module.UP, stations_module.SETUP)]
        # Spread over this many seconds. A command that puts something on the
        # air — `lora 0 a` above all — fired at two dozen stations in the same
        # instant is a collision storm rather than a measurement, and the
        # answers are about the storm. Zero keeps them simultaneous, which is
        # what you want for a question nobody transmits to answer.
        spread = max(0.0, float(msg.get("stagger") or 0))
        gap = spread / len(targets) if spread and targets else 0.0

        async def run(index, name, station):
            if gap:
                await asyncio.sleep(index * gap)
            try:
                text = await setup_module.ask(
                    station.node_id, scenario_module.expand(line, name, sc.node(name)))
                return name, text.rstrip("\n")
            except (setup_module.CliError, scenario_module.ScenarioError) as err:
                return name, "! %s" % err

        results = dict(await asyncio.gather(
            *(run(i, n, s) for i, (n, s) in enumerate(targets))))
        self.broadcast({"type": "command_result", "line": line, "results": results})
        log("ran %r on %d station(s)%s" % (line, len(results),
            " over %.0fs" % spread if spread else ""))

    async def do_node_setup(self, msg):
        sc = self.need_scenario()
        sc.set_node_setup(msg["name"], msg.get("lines") or [])
        sc.flush()
        self.scenario_changed()
        self.broadcast(self.node_message(msg["name"]))

    async def do_start_all(self, msg):
        self.begin_start_all()

    async def do_stop_all(self, msg):
        await self.stop_all()
        for name in (self.scenario.nodes if self.scenario else ()):
            self.broadcast(self.node_message(name))

    async def do_physics(self, msg):
        sc = self.need_scenario()
        sc.set_physics({k: v for k, v in msg.items() if k != "type"})
        sc.flush()
        self.apply_to_ether()
        self.scenario_changed()

    async def do_setup(self, msg):
        sc = self.need_scenario()
        sc.set_setup(msg.get("lines") or [])
        sc.flush()
        self.scenario_changed()

    async def do_obstruction(self, msg):
        sc = self.need_scenario()
        sc.set_obstruction(msg["between"][0], msg["between"][1], msg.get("db", 0))
        sc.flush()
        self.apply_to_ether()
        self.scenario_changed()

    async def do_levels(self, msg):
        """What one station's neighbours would hear from it, for the hover card."""
        sc = self.need_scenario()
        node = sc.node(msg["name"])
        freq = msg.get("freq") or 869_525_000
        heard = self.ether.levels(node["id"], freq)
        self.broadcast({"type": "levels", "name": msg["name"],
                        "heard": {self.name_of(sid): round(level, 1)
                                  for sid, level in heard.items()
                                  if self.name_of(sid) is not None}})

    # ---- the scenario verbs ----------------------------------------------

    async def adopt(self, sc):
        """Make this the loaded scenario: stop what was running, start what is."""
        await self.stop_all()
        self.scenario = sc
        self.apply_to_ether()
        self.broadcast(self.snapshot())     # a new scenario is a fresh page
        self.begin_start_all()

    async def do_scenario_new(self, msg):
        await self.adopt(scenario_module.create(msg["name"]))
        log("new scenario %s" % msg["name"])

    async def do_scenario_load(self, msg):
        """A scenario is a design, so loading one is a factory-fresh network."""
        await self.adopt(scenario_module.load_scenario(msg["name"]))
        log("loaded scenario %s" % msg["name"])

    async def do_scenario_save(self, msg):
        self.need_scenario().save()
        self.scenario_changed()
        log("saved scenario %s" % self.scenario.name)

    async def do_scenario_save_as(self, msg):
        self.need_scenario().save_as(msg["name"])
        self.scenario_changed()
        log("saved scenario as %s" % self.scenario.name)

    async def do_snapshot_load(self, msg):
        """A snapshot is a moment, so loading one brings its state back too."""
        await self.adopt(scenario_module.load_snapshot(msg["name"]))
        log("loaded snapshot %s" % msg["name"])

    async def do_snapshot_save_as(self, msg):
        sc = self.need_scenario()
        await self.flush_all()   # the stations keep running; the copy is of now
        sc.save_snapshot(msg["name"])
        self.scenario_changed()
        log("saved snapshot %s" % msg["name"])

    # ---- the HTTP side ---------------------------------------------------

    async def ws_page(self, request):
        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)
        self.pages.add(socket)
        await socket.send_str(json.dumps(self.snapshot()))
        try:
            async for message in socket:
                if message.type is WSMsgType.TEXT:
                    try:
                        await self.handle(json.loads(message.data))
                    except ValueError:
                        pass
        finally:
            self.pages.discard(socket)
        return socket

    async def ws_console(self, request):
        """The station's pty over a websocket.

        A **binary** frame is keystrokes and goes to the pty as it stands; a
        **text** frame is a control message, of which there is one — `resize`,
        carrying the terminal's size. Splitting them by frame type rather than
        by an escape in the stream means no byte a person can type is special.
        """
        name = request.match_info["name"]
        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)
        self.consoles.setdefault(name, set()).add(socket)
        try:
            async for message in socket:
                station = self.stations.get(name)
                if station is None:
                    continue
                if message.type is WSMsgType.BINARY:
                    station.write(message.data)
                elif message.type is WSMsgType.TEXT:
                    try:
                        control = json.loads(message.data)
                    except ValueError:
                        continue
                    if control.get("type") == "resize":
                        station.resize(int(control.get("cols", 80)),
                                       int(control.get("rows", 24)))
        finally:
            self.consoles.get(name, set()).discard(socket)
        return socket

    async def ws_signalling(self, request):
        """Stand in the middle of a station's WebRTC signalling.

        Terminated on both sides rather than pumped as bytes, so the SDP
        answer arrives as a parsed message and can be pointed at the relay.
        Everything else is passed through exactly as it came: simd is a relay
        here, not a participant, and the offer, the answer's fingerprint and
        the DTLS behind it are all the station's own.
        """
        label = request.headers.get("Host", "").split(".", 1)[0].lower()
        node = self.node_for_label(label)
        if node is None:
            return web.Response(status=404, text="no such station\n")
        station_url = "http://%s%s" % (stations_module.bind_addr(node["id"]),
                                       SIGNAL_PATH)

        # The browser's credentials go up with it. Signalling is behind the
        # station's own login, and a relay that dropped the session cookie
        # would be asking the station to talk to a stranger.
        passed = {name: request.headers[name]
                  for name in ("Cookie", "Authorization", "Origin")
                  if name in request.headers}

        browser = web.WebSocketResponse(heartbeat=30)
        await browser.prepare(request)
        learned = []
        session = aiohttp.ClientSession()
        try:
            async with session.ws_connect(station_url, headers=passed) as upstream:

                async def to_station():
                    async for message in browser:
                        if message.type is WSMsgType.TEXT:
                            await upstream.send_str(message.data)
                        elif message.type is WSMsgType.BINARY:
                            await upstream.send_bytes(message.data)

                async def to_browser():
                    async for message in upstream:
                        if message.type is WSMsgType.TEXT:
                            await browser.send_str(self.rewrite_signal(
                                message.data, node, learned))
                        elif message.type is WSMsgType.BINARY:
                            await browser.send_bytes(message.data)
                    if upstream.close_code not in (None, 1000, 1001):
                        log("signalling: %s closed by the station (%s)"
                            % (label, upstream.close_code))

                # Whichever side ends first ends the session: the station
                # allows one at a time and frees its handle when the signalling
                # closes, so a half that kept waiting on the other would leave
                # the station busy for a browser that had already gone.
                halves = [asyncio.ensure_future(to_station()),
                          asyncio.ensure_future(to_browser())]
                finished, pending = await asyncio.wait(
                    halves, return_when=asyncio.FIRST_COMPLETED)
                for half in pending:
                    half.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await half
                for half in finished:
                    if half.exception() is not None:
                        log("signalling for %s: %r" % (label, half.exception()))
                await upstream.close()
        except (aiohttp.ClientError, OSError) as err:
            log("signalling for %s: %s" % (label, err))
        finally:
            await session.close()
            for ufrag in learned:
                self.relay.forget(ufrag)
        return browser

    def rewrite_signal(self, text, node, learned):
        """Point an SDP answer at the relay, and remember whose it is."""
        if self.relay is None:
            return text
        try:
            message = json.loads(text)
        except ValueError:
            return text
        if message.get("type") != "answer" or not message.get("sdp"):
            return text
        sdp = message["sdp"]
        ufrag = webrtc_module.answer_ufrag(sdp)
        if ufrag:
            self.relay.learn(ufrag, stations_module.bind_addr(node["id"]),
                             WEBRTC_PORT)
            learned.append(ufrag)
        message["sdp"] = webrtc_module.rewrite_answer(
            sdp, self.args.relay_host, self.args.relay_port)
        return json.dumps(message)

    async def api_scenarios(self, request):
        return web.json_response({"scenarios": scenario_module.scenarios(),
                                  "snapshots": scenario_module.snapshots(),
                                  "loaded": self.scenario.name if self.scenario
                                  else None})

    async def serve_page(self, request):
        """The built page, with every unknown path falling back to it.

        One page and no router beyond it, so a deep link is the same document;
        a request for a file that is really there gets that file.
        """
        path = request.match_info.get("tail", "")
        if path and not path.startswith("."):
            candidate = os.path.normpath(os.path.join(UI_DIST, path))
            if candidate.startswith(UI_DIST) and os.path.isfile(candidate):
                return web.FileResponse(candidate)
        page = os.path.join(UI_DIST, "index.html")
        if os.path.isfile(page):
            return web.FileResponse(page)
        return web.Response(
            text="The control page has not been built.\n\n"
                 "From the workspace: `spangap sim` builds it, or in "
                 "reticulous/sim/ui run `npm install && npx quasar build`.\n"
                 "`spangap sim --dev` serves it hot from Vite instead.\n",
            content_type="text/plain")

    def app(self):
        app = web.Application()
        app.router.add_get(SIGNAL_PATH, self.ws_signalling)
        app.router.add_get("/ws", self.ws_page)
        app.router.add_get("/ws/console/{name}", self.ws_console)
        app.router.add_get("/api/scenarios", self.api_scenarios)
        app.router.add_get("/{tail:.*}", self.serve_page)
        return app

    async def start_http(self):
        """The control app on a loopback port, and the front listener on the bind.

        The app is never bound to the outside: the front listener is what the
        world reaches, and it dials the app for anything that is not a station.
        One port, and `Host` decides.
        """
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.control_port = self.runner.addresses[0][1]

        host, _, port = self.args.bind.rpartition(":")
        self.front = await proxy.serve(host or "0.0.0.0", int(port),
                                       self.resolve_host)
        webrtc_module.log = log
        self.relay = await webrtc_module.serve(
            host or "0.0.0.0", self.args.relay_bind_port,
            self.args.relay_host, self.args.relay_port)
        log("webrtc relay on udp/%d, offered to browsers as %s:%d"
            % (self.args.relay_bind_port, self.args.relay_host,
               self.args.relay_port))
        log("control page on http://localhost:%s/" % self.args.public_port)
        log("stations at http://<name>.sim.localhost:%s/" % self.args.public_port)

    # ---- the run ---------------------------------------------------------

    async def run(self):
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(
                    sig, lambda: done.done() or done.set_result(None))

        if not os.path.exists(self.elf):
            raise SystemExit(
                "no station binary at %s — build it for the host target first "
                "(see sim/README.md)" % self.elf)
        os.makedirs(scenario_module.SCENARIOS_DIR, exist_ok=True)
        os.makedirs(scenario_module.SNAPSHOTS_DIR, exist_ok=True)

        await self.start_ether()
        await self.start_http()
        self.poller = asyncio.ensure_future(self.poll_transport())
        try:
            await done
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Stop everything, in the order that lets each step finish.

        The aiohttp side goes first because it owns the websockets, and a
        proxied websocket is a front-listener handler that will not return
        until its connection does — closing the front first would wait on a
        page that is still open, for as long as it stayed open. Every wait
        here is bounded for the same reason: a testbed told to stop stops, and
        the sockets go with the process regardless.
        """
        self.stopping = True
        log("stopping")
        if self.poller is not None:
            self.poller.cancel()
        await self.stop_all()
        if self.runner is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.runner.cleanup(), SHUTDOWN_TIMEOUT_S)
        if self.front is not None:
            self.front.close()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.front.wait_closed(), SHUTDOWN_TIMEOUT_S)
        if self.relay is not None:
            self.relay.close()
        if self.ether is not None:
            self.ether.close()


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="the simulated testbed: ether, stations, proxy and control page")
    ap.add_argument("--bind", default="0.0.0.0:9011",
                    help="host:port for the page and the stations (default 0.0.0.0:9011)")
    ap.add_argument("--ether", default="127.0.0.1:7000",
                    help="host:port for the ether's UDP endpoint")
    ap.add_argument("--elf", default=DEFAULT_ELF, help="the station binary")
    ap.add_argument("--fixed", default=DEFAULT_FIXED, help="the /fixed image directory")
    ap.add_argument("--relay-port", type=int, default=0,
                    help="the UDP port a browser sends the DataChannel to, as "
                         "the browser sees it (default: the bind port)")
    ap.add_argument("--stagger", type=float, default=60.0,
                    help="seconds to spread a whole fleet's start over, so the "
                         "stations do not boot and announce in lockstep "
                         "(default 60)")
    ap.add_argument("--relay-host", default="127.0.0.1",
                    help="the address a browser sends the DataChannel to "
                         "(default 127.0.0.1)")
    args = ap.parse_args(argv)
    args.elf = os.path.abspath(args.elf)
    args.fixed = os.path.abspath(args.fixed)
    args.public_port = args.bind.rpartition(":")[2]
    # The relay binds the same number as the page, on UDP — one number to
    # publish and one to remember. What the browser is told may differ, since
    # the container's mapping is the host's business, not simd's.
    args.relay_bind_port = int(args.public_port)
    if not args.relay_port:
        args.relay_port = args.relay_bind_port
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(Simd(args).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
