#!/usr/bin/env python3
"""Start the ether, the proxy and N stations, and keep them running.

Each station is one firmware process on its own loopback address, with a pty
for its serial console: the launcher holds the master end, appends everything
the station writes to `nodes/<id>/log`, and with `--console <id>` puts the
terminal on that pty so typing reaches the station exactly as a cable would.
A station that exits is started again — a restart on this target is a process
exit — and Ctrl-C brings the whole set down.

  run.py --nodes 3                 three stations, the ether and the proxy
  run.py --console 1               the same, with the terminal on station 1
  run.py --ether-only              the ether alone

The proxy makes every station reachable from a browser outside the container
at http://<id>.sim.localhost:9011/ .
"""

import argparse
import asyncio
import os
import pty
import signal
import sys
import termios
import tty

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
BUILDABLE = os.path.dirname(SIM_DIR)
ETHER_PY = os.path.join(BUILDABLE, "ether", "ether.py")
PROXY_PY = os.path.join(SIM_DIR, "proxy.py")
DEFAULT_ELF = os.path.join(BUILDABLE, "esp-idf", "build", "reticulous.elf")
DEFAULT_FIXED = os.path.join(BUILDABLE, "esp-idf", "build", "data_merged")
NODES_DIR = os.path.join(SIM_DIR, "nodes")

DETACH_KEY = 0x1D           # Ctrl-] leaves an attached console
RESTART_DELAY = 0.5         # seconds before a station that exited comes back
MAX_NODE = 99               # 127.0.0.1<id> must stay a legal address

RAW_TERMINAL = False        # log lines need \r while a console holds the tty


def log(msg):
    sys.stderr.write("sim: %s%s" % (msg, "\r\n" if RAW_TERMINAL else "\n"))
    sys.stderr.flush()


def bind_addr(node_id):
    """The station's own loopback address."""
    return "127.0.0.1%d" % node_id


class Station:
    """One firmware process, its pty and its log."""

    def __init__(self, launcher, node_id):
        self.launcher = launcher
        self.node_id = node_id
        self.dir = os.path.join(NODES_DIR, str(node_id))
        self.log_path = os.path.join(self.dir, "log")
        self.master = None
        self.proc = None
        self.log_file = None

    def env(self):
        env = dict(os.environ)
        env.update(SPANGAP_NODE_ID=str(self.node_id),
                   SPANGAP_NODE_DIR=self.dir,
                   SPANGAP_BIND_ADDR=bind_addr(self.node_id),
                   SPANGAP_ETHER=self.launcher.ether_addr,
                   SPANGAP_FIXED_DIR=self.launcher.fixed)
        return env

    async def start(self):
        os.makedirs(self.dir, exist_ok=True)
        if self.log_file is None:
            self.log_file = open(self.log_path, "ab", buffering=0)
        self.log_file.write(b"\n--- station %d starting ---\n" % self.node_id)

        master, slave = pty.openpty()
        tty.setraw(slave)       # a serial line has no echo and no translation
        self.master = master
        self.proc = await asyncio.create_subprocess_exec(
            self.launcher.elf, cwd=SIM_DIR, env=self.env(),
            stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.set_blocking(master, False)
        asyncio.get_running_loop().add_reader(master, self.readable)
        log("station %d up as pid %d on %s" % (
            self.node_id, self.proc.pid, bind_addr(self.node_id)))

    def readable(self):
        """Drain the pty: everything a station prints lands in its log."""
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""          # the station let go of the far end
        if not data:
            self.detach_reader()
            return
        self.log_file.write(data)
        if self.launcher.console == self.node_id:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def detach_reader(self):
        if self.master is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)
        except OSError:
            pass
        self.master = None

    def write(self, data):
        """Type into the station's console."""
        if self.master is not None:
            try:
                os.write(self.master, data)
            except OSError:
                pass

    async def supervise(self):
        """Keep the station running for as long as the launcher is up."""
        while not self.launcher.stopping:
            await self.start()
            code = await self.proc.wait()
            self.detach_reader()
            if self.launcher.stopping:
                return
            log("station %d exited (%s), restarting" % (self.node_id, code))
            await asyncio.sleep(RESTART_DELAY)

    def stop(self):
        self.detach_reader()
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass


class Launcher:
    """The ether, the proxy, the stations and the terminal in one place."""

    def __init__(self, args):
        self.args = args
        self.ether_addr = args.ether
        self.elf = args.elf
        self.fixed = args.fixed
        self.console = None
        self.stopping = False
        self.stations = []
        self.helpers = []       # the ether and the proxy
        self.saved_tty = None

    # ---- the helper processes -------------------------------------------

    async def start_ether(self):
        record = os.path.join(SIM_DIR, "record.tsv")
        out = None
        if self.args.console is not None:
            out = open(os.path.join(SIM_DIR, "ether.log"), "ab", buffering=0)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, ETHER_PY, "--bind", self.ether_addr,
            "--record", record, cwd=SIM_DIR,
            stdout=out or sys.stderr, stderr=out or sys.stderr)
        self.helpers.append(proc)
        log("ether on %s, record %s" % (self.ether_addr, record))
        await asyncio.sleep(0.5)
        if proc.returncode is not None:
            raise SystemExit("the ether exited at once (%s)" % proc.returncode)

    async def start_proxy(self):
        out = None
        if self.args.console is not None:
            out = open(os.path.join(SIM_DIR, "proxy.log"), "ab", buffering=0)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, PROXY_PY, "--bind", self.args.proxy_bind,
            cwd=SIM_DIR, stdout=out or sys.stderr, stderr=out or sys.stderr)
        self.helpers.append(proc)
        port = self.args.proxy_bind.rpartition(":")[2]
        log("proxy on %s: browse http://1.sim.localhost:%s/" % (
            self.args.proxy_bind, port))

    # ---- the terminal ----------------------------------------------------

    def attach_console(self, node_id):
        """Put this terminal on a station's pty until the detach key."""
        global RAW_TERMINAL
        if not sys.stdin.isatty():
            log("stdin is not a terminal; station %d's console stays in its log"
                % node_id)
            return
        self.console = node_id
        self.saved_tty = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        RAW_TERMINAL = True
        asyncio.get_running_loop().add_reader(sys.stdin.fileno(), self.stdin_readable)
        log("attached to station %d; Ctrl-] detaches, then Ctrl-C quits" % node_id)

    def stdin_readable(self):
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            data = b""
        if not data:
            return
        station = self.station(self.console)
        if bytes([DETACH_KEY]) in data:
            data = data.split(bytes([DETACH_KEY]))[0]
            if station is not None and data:
                station.write(data)
            self.detach_console()
            return
        if station is not None:
            station.write(data)

    def detach_console(self):
        global RAW_TERMINAL
        if self.console is None:
            return
        node_id = self.console
        self.console = None
        try:
            asyncio.get_running_loop().remove_reader(sys.stdin.fileno())
        except (ValueError, OSError):
            pass
        self.restore_terminal()
        log("detached from station %d; its output continues in nodes/%d/log"
            % (node_id, node_id))

    def restore_terminal(self):
        global RAW_TERMINAL
        if self.saved_tty is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.saved_tty)
            self.saved_tty = None
        RAW_TERMINAL = False

    def station(self, node_id):
        for station in self.stations:
            if station.node_id == node_id:
                return station
        return None

    # ---- the run ---------------------------------------------------------

    async def run(self):
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig, lambda: done.done() or done.set_result(None))

        if not self.args.ether_only and not os.path.exists(self.elf):
            raise SystemExit("no station binary at %s" % self.elf)

        supervisors = []
        try:
            await self.start_ether()
            if not self.args.ether_only:
                if not self.args.no_proxy:
                    await self.start_proxy()
                os.makedirs(NODES_DIR, exist_ok=True)
                for node_id in range(1, self.args.nodes + 1):
                    self.stations.append(Station(self, node_id))
                if self.args.console is not None:
                    self.attach_console(self.args.console)
                supervisors.extend(asyncio.ensure_future(s.supervise())
                                   for s in self.stations)
            await done
        finally:
            await self.shutdown(supervisors)

    async def shutdown(self, supervisors):
        self.stopping = True
        self.detach_console()
        self.restore_terminal()
        log("stopping")
        for task in supervisors:
            task.cancel()
        for station in self.stations:
            station.stop()
        for proc in self.helpers:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        waits = [p.wait() for p in self.helpers]
        waits += [s.proc.wait() for s in self.stations if s.proc is not None]
        if waits:
            try:
                await asyncio.wait_for(asyncio.gather(*waits, return_exceptions=True), 5)
            except asyncio.TimeoutError:
                log("something would not stop; killing it")
                for station in self.stations:
                    if station.proc is not None and station.proc.returncode is None:
                        station.proc.kill()
                for proc in self.helpers:
                    if proc.returncode is None:
                        proc.kill()


def parse_args(argv):
    ap = argparse.ArgumentParser(description="run a testbed of stations on one machine")
    ap.add_argument("--nodes", type=int, default=3, help="how many stations (default 3)")
    ap.add_argument("--console", type=int, metavar="ID",
                    help="attach this terminal to station ID's console")
    ap.add_argument("--ether-only", action="store_true", help="start the ether and nothing else")
    ap.add_argument("--no-proxy", action="store_true", help="do not start the proxy")
    ap.add_argument("--elf", default=DEFAULT_ELF, help="the station binary")
    ap.add_argument("--fixed", default=DEFAULT_FIXED, help="the /fixed image directory")
    ap.add_argument("--ether", default="127.0.0.1:7000", help="host:port for the ether")
    ap.add_argument("--proxy-bind", default="0.0.0.0:9011", help="host:port for the proxy")
    args = ap.parse_args(argv)
    if not 1 <= args.nodes <= MAX_NODE:
        ap.error("--nodes must be between 1 and %d" % MAX_NODE)
    if args.console is not None and not 1 <= args.console <= args.nodes:
        ap.error("--console must name a station between 1 and %d" % args.nodes)
    args.elf = os.path.abspath(args.elf)
    args.fixed = os.path.abspath(args.fixed)
    return args


def main(argv=None):
    args = parse_args(argv)
    launcher = Launcher(args)
    try:
        asyncio.run(launcher.run())
    except KeyboardInterrupt:
        pass
    finally:
        launcher.restore_terminal()
    return 0


if __name__ == "__main__":
    sys.exit(main())
