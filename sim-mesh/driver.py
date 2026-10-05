"""Reticulous (spangap/reticulous) on sim-mesh: the `reticulum` driver.

The station is the firmware built for the Linux host target (hw-linux), with
its merged `/fixed` tree beside it in the zip. It is asked things over framed
RPC on its console pty: every line is one frame, answered with what the
command printed. Its role is `transport` while `s.rnsd.transport_enabled` is
on, else `client`. It has a web UI on port 80, a store that coalesces writes
until `save`, and `state/boot` once it has booted.

Its verbs, in its CLI:

    name            hostname <name>
    radio           lora 0 freq|sf|bw|cr|txp|sync|preamble, one per figure given
    radio_up        lora up, confirmed by s.lora.0.enable
    tx_power        lora 0 txp <dBm>
    role            set s.rnsd.transport_enabled 1 (transport) or 0 (client)
    path            rnpath -j -a [<dest>] [-i <iface>]
                                             -> {"paths": [{dest, next_hop, iface, hops, …}]}
    peer_tcp        tcp peer add <addr>:<port>
    lxmf.create     lxmf create <name>, unless `lxmf id` lists one labelled so
    lxmf.identities the labels and destinations `lxmf id` lists, the selected
                    one (`*`) first
    lxmf.announce   lora 0 a (every announce, again); for a named identity,
                    lxmf id <n> and lxmf announce
    lxmf.send       lxmf send <dest> <text>  -> `queued <its own id>`, coupled
                    to sim-mesh's; from a named identity, lxmf id <n> first,
                    which leaves it the selected one

What becomes of a message it reads from the station's console: every log line
that names a message's own id (`mid=o_…`) is reported against sim-mesh's,
`DIRECT delivered` and `DIRECT resource delivered` as `delivered`, a failure
as `failed`.
"""

import asyncio
import json
import os
import re

from sim_mesh.driver import (EXEC_BOUND_S, PROBE, QUERY_TIMEOUT_S, CommandError,
                             parse_setting)
from sim_mesh.reticulum.driver import ReticulumDriver

TRANSPORT_KEY = "s.rnsd.transport_enabled"
BOOTED_KEY = "s.sys.reset_reason"      # written once every service's init has run
IDENTITY = re.compile(r"^\s*(\*?)\s*(\d+)\s+(\S+)\s+([0-9a-f]{32})", re.MULTILINE)
CREATED = re.compile(r"created \".*\" at slot \d+ \(([0-9a-f]{32})\)")
QUEUED = re.compile(r"queued (\S+)")
PATH_KEYS = ("dest", "next_hop", "iface", "hops")
MID = re.compile(r"mid=(o_\S+)")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
STAMP = re.compile(r"^\w{3} +\d+ \d\d:\d\d:\d\d\.\d{3} ")
# The radio's figures as its CLI takes them, in the order they are set.
RADIO_LINES = (("freq_mhz", "lora 0 freq %g"), ("sf", "lora 0 sf %d"), ("bw_khz", "lora 0 bw %g"),
               ("cr", "lora 0 cr %d"), ("tx_dbm", "lora 0 txp %g"),
               ("sync", "lora 0 sync 0x%02x"), ("preamble", "lora 0 preamble %d"))
MARKER_WAIT_S = 20.0        # how long a started station has to print the framed-RPC marker
SLOW_S = EXEC_BOUND_S - 0.5  # a reply this late may have been cut at the bound
SETTLE_POLL_S = 0.5
DIAGNOSTICS = ("rnpath -s", "lxmf unfinished", "lxmf msgs received", "lxmf msgs delivered",
               "lora 0", "lora 0 supe")

# A line whose effect lands after its reply, and the one key that says it has.
CONFIRM = {
    "lora up": ("s.lora.0.enable", "1"),
}


class Reticulous(ReticulumDriver):

    def env(self, station):
        # What this firmware reads: its board (hw-linux) takes its identity,
        # directory, address, ether, /fixed tree and board (its radio's front
        # end and ceiling) from these names.
        env = {"SPANGAP_NODE_ID": str(station.node_id),
               "SPANGAP_NODE_DIR": station.dir,
               "SPANGAP_BIND_ADDR": station.addr,
               "SPANGAP_ETHER": station.ether_addr}
        if self.firmware.get("fixed"):
            env["SPANGAP_FIXED_DIR"] = self.firmware["fixed"]
        if station.board:
            env["SPANGAP_BOARD"] = station.board
        return env

    def configured(self, station):
        return os.path.exists(os.path.join(station.dir, "state", "boot"))

    def web_port(self):
        return 80

    async def wait_up(self, station, timeout):
        """Up once it answers a frame and its boot has initialised every
        service: the console answers from early in boot, and a setting typed
        before a service's own init has run can be undone by that init (a
        radio's frequency is). The boot writes BOOTED_KEY once they all have,
        and on a first boot, which is when a station is set up, it is absent
        until then."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if not await self.rpc_ready(station, timeout, MARKER_WAIT_S):
            return False
        while loop.time() < deadline:
            try:
                if await self.show(station, BOOTED_KEY):
                    return True
            except CommandError:
                pass
            await self.pause(station, SETTLE_POLL_S)
        return False

    async def run(self, station, line, timeout=None):
        return await self.rpc_query(station, line, timeout)

    async def show(self, station, key):
        return parse_setting(await self.run(station, "show %s" % key), key)

    async def settle(self, station, line, elapsed):
        """After a line, wait until what it asked for has landed.

        A reply that came back at the station's exec bound may have been cut
        there with the command still running, so the next frame waits for the
        CLI to answer again. A line in CONFIRM is followed until its key says
        it took.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2 * QUERY_TIMEOUT_S
        if elapsed >= SLOW_S:
            while loop.time() < deadline:
                try:
                    if (await self.run(station, PROBE)).strip():
                        break
                except CommandError:
                    pass
                await self.pause(station, SETTLE_POLL_S)
        want = CONFIRM.get(" ".join(line.split()))
        if want is None:
            return
        key, value = want
        while loop.time() < deadline:
            if await self.show(station, key) == value:
                return
            await self.pause(station, SETTLE_POLL_S)
        raise CommandError("%s: %s never read %s" % (line, key, value))

    async def type(self, station, line):
        """One line as its own frame, confirmed where it has to be."""
        loop = asyncio.get_running_loop()
        began = loop.time()
        reply = await self.run(station, line)
        await self.settle(station, line, loop.time() - began)
        return reply

    async def flush(self, station):
        """`save`: the store coalesces writes for `s.storage.flash_delay`
        seconds — a minute by default — so a station set up and then reset
        inside that window would come back with none of it."""
        try:
            await self.run(station, "save")
        except CommandError:
            pass

    # ---- the reticulum verbs -----------------------------------------------

    async def name(self, station, name):
        await self.type(station, "hostname %s" % name)

    async def role(self, station, role):
        await self.type(station, "set %s %d" % (TRANSPORT_KEY, 1 if role == "transport" else 0))

    async def radio(self, station, **figures):
        for key, line in RADIO_LINES:
            if figures.get(key) is not None:
                await self.type(station, line % figures[key])

    async def radio_up(self, station):
        await self.type(station, "lora up")

    async def tx_power(self, station, dbm):
        await self.type(station, "lora 0 txp %g" % float(dbm))

    async def path(self, station, dest=None, iface=None):
        line = "rnpath -j -a" + (" %s" % dest if dest else "") + (" -i %s" % iface if iface else "")
        out = await self.run(station, line)
        try:
            paths = json.loads(out[out.index("{"):]).get("paths") or []
        except ValueError as err:
            raise CommandError(out.strip()[:200]) from err
        return [{key: entry.get(key) for key in PATH_KEYS} for entry in paths]

    async def peer_tcp(self, station, addr, port=4965):
        await self.run(station, "tcp peer add %s:%d" % (addr, int(port)))

    async def current_role(self, station):
        value = await self.show(station, TRANSPORT_KEY)
        if value is None:
            return None
        return "client" if value in ("0", "") else "transport"

    async def diagnostics(self, station):
        out = {}
        for line in DIAGNOSTICS:
            try:
                out[line] = await self.run(station, line)
            except CommandError as err:
                out[line] = "! %s" % err
        return out

    # ---- LXMF: identities in slots, one of them selected -------------------

    async def slots(self, station):
        """[(slot, label, destination, selected)], as `lxmf id` lists them."""
        return [(int(slot), label, dest, star == "*")
                for star, slot, label, dest in IDENTITY.findall(await self.run(station, "lxmf id"))]

    async def identity_of(self, station, name):
        """The (slot, label, destination, selected) of the identity `name`, or
        the selected one; None when there is none."""
        for row in await self.slots(station):
            if row[1] == name if name is not None else row[3]:
                return row
        return None

    async def select(self, station, name):
        """The identity `name` made the selected one, which is the one that
        sends and announces."""
        row = await self.identity_of(station, name)
        if row is None:
            raise CommandError("%s has no LXMF identity named %s" % (station.name, name))
        if not row[3]:
            await self.run(station, "lxmf id %d" % row[0])

    async def lxmf_create(self, station, name):
        row = await self.identity_of(station, name)
        if row is not None:
            return row[2]
        found = CREATED.search(await self.type(station, "lxmf create %s" % name))
        return found.group(1) if found else None

    async def lxmf_identities(self, station):
        rows = sorted(await self.slots(station), key=lambda row: not row[3])
        return [(label, dest) for _, label, dest, _ in rows]

    async def lxmf_announce(self, station, name=None):
        if name is None:
            await self.run(station, "lora 0 a")
            return
        await self.select(station, name)
        await self.run(station, "lxmf announce")

    async def lxmf_send(self, station, dest, text, mid, sender=None):
        if sender is not None:
            await self.select(station, sender)
        out = await self.run(station, "lxmf send %s %s" % (dest, text))
        found = QUEUED.search(out)
        if not found:
            self.lxmf_status(station, mid, "failed", out.strip()[:300] or "no message id")
            return
        self.lxmf_couple(station, mid, found.group(1))

    # ---- what it prints ----------------------------------------------------

    def console_line(self, station, line):
        line = ANSI.sub("", line)
        found = MID.search(line)
        if not found:
            return
        said = (line[STAMP.match(line).end():] if STAMP.match(line) else line).strip()
        if "delivered mid=" in line:
            self.lxmf_native(station, found.group(1), "delivered")
        elif "failed" in line:
            self.lxmf_native(station, found.group(1), "failed", said)
        else:
            self.lxmf_native(station, found.group(1), "pending", said)


DRIVER = Reticulous
