#!/usr/bin/env python3
"""One firmware process, its pty, its log and its supervisor.

A station is the whole firmware built for the Linux host target and run as an
ordinary process. It gets:

- a **directory** under the run, `run/nodes/<name>/`, which is its `/state`;
- a **pty**, because its stdin and stdout are its serial console — the
  supervisor holds the master end, appends everything the station writes to
  `log`, and passes the same bytes to whoever is watching the console;
- a **loopback address**, `127.0.0.1<id>`, where its web UI and its TCP CLI
  answer;
- a **supervisor** that starts it again when it exits, because a restart on
  this target is a process exit.

Status is what the map draws: `stopped` before anything is started and after
it is told to stop, `starting` from the fork until the CLI answers, `setup`
while its setup lines are going in, `up` once it is answering, `restarting`
in the gap after an unasked-for exit. Nothing here decides when `setup`
happens — the caller does that, through `on_status`.
"""

import asyncio
import os
import pty
import sys
import tty

RESTART_DELAY = 0.5         # seconds before a station that exited comes back
MAX_NODE_ID = 99            # 127.0.0.1<id> must stay a legal address

STOPPED, STARTING, SETUP, UP, RESTARTING = (
    "stopped", "starting", "setup", "up", "restarting")


def log(msg):
    sys.stderr.write("sim: %s\n" % msg)
    sys.stderr.flush()


def bind_addr(node_id):
    """The station's own loopback address."""
    return "127.0.0.1%d" % node_id


class Station:
    """One firmware process, its pty and its log."""

    def __init__(self, name, node_id, directory, elf, fixed, ether_addr,
                 on_status=None, on_output=None):
        self.name = name
        self.node_id = node_id
        self.dir = directory
        self.elf = elf
        self.fixed = fixed
        self.ether_addr = ether_addr
        self.on_status = on_status      # (station, status)
        self.on_output = on_output      # (station, bytes)
        self.master = None
        self.proc = None
        self.log_file = None
        self.status = STOPPED
        self.transport = None           # last read of s.rnsd.transport_enabled
        # Whether this station had been through a first boot when it was last
        # started. Sampled at the fork, because the station writes `state/boot`
        # moments later and the answer the setup step needs is the one from
        # before it ran.
        self.was_configured = False
        self.stopping = False
        self.supervisor = None
        self.consoles = set()           # websockets watching the pty

    # ---- identity --------------------------------------------------------

    @property
    def addr(self):
        return bind_addr(self.node_id)

    @property
    def log_path(self):
        return os.path.join(self.dir, "log")

    @property
    def state_dir(self):
        return os.path.join(self.dir, "state")

    @property
    def configured(self):
        """True when this station has been through its first boot already.

        The firmware writes `state/boot` once it has run; a directory without
        it is a station that has never been set up, which is what decides
        whether the scenario's setup lines go in.
        """
        return os.path.exists(os.path.join(self.state_dir, "boot"))

    def set_status(self, status):
        if status == self.status:
            return
        self.status = status
        if self.on_status is not None:
            self.on_status(self, status)

    # ---- the process -----------------------------------------------------

    def env(self):
        env = dict(os.environ)
        env.update(SPANGAP_NODE_ID=str(self.node_id),
                   SPANGAP_NODE_DIR=self.dir,
                   SPANGAP_BIND_ADDR=self.addr,
                   SPANGAP_ETHER=self.ether_addr,
                   SPANGAP_FIXED_DIR=self.fixed)
        return env

    async def start(self):
        self.was_configured = self.configured
        os.makedirs(self.state_dir, exist_ok=True)
        if self.log_file is None:
            self.log_file = open(self.log_path, "ab", buffering=0)
        self.log_file.write(b"\n--- station %s starting ---\n"
                            % self.name.encode("utf-8"))

        master, slave = pty.openpty()
        tty.setraw(slave)       # a serial line has no echo and no translation
        self.master = master
        self.set_status(STARTING)
        self.proc = await asyncio.create_subprocess_exec(
            self.elf, cwd=self.dir, env=self.env(),
            stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.set_blocking(master, False)
        asyncio.get_running_loop().add_reader(master, self.readable)
        log("station %s (%d) up as pid %d on %s" % (
            self.name, self.node_id, self.proc.pid, self.addr))

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
        if self.on_output is not None:
            self.on_output(self, data)

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

    def resize(self, cols, rows):
        """Tell the station's console how wide its terminal is."""
        if self.master is None:
            return
        import fcntl
        import struct
        import termios
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    # ---- the supervisor --------------------------------------------------

    async def supervise(self, after_start=None):
        """Keep the station running until it is told to stop.

        `after_start` is awaited once per start, with the station, and is
        where the caller waits for the CLI and sends the setup lines. It is
        cancelled when the process exits under it.
        """
        while not self.stopping:
            await self.start()
            watcher = None
            if after_start is not None:
                watcher = asyncio.ensure_future(after_start(self))
            code = await self.proc.wait()
            if watcher is not None:
                watcher.cancel()
            self.detach_reader()
            if self.stopping:
                return
            self.set_status(RESTARTING)
            log("station %s exited (%s), restarting" % (self.name, code))
            await asyncio.sleep(RESTART_DELAY)

    def run(self, after_start=None):
        """Start the supervisor as a task and keep hold of it."""
        self.stopping = False
        self.supervisor = asyncio.ensure_future(self.supervise(after_start))
        return self.supervisor

    async def stop(self):
        """Stop the process and the supervisor, and wait for both to go."""
        self.stopping = True
        if self.supervisor is not None:
            self.supervisor.cancel()
            try:
                await self.supervisor
            except asyncio.CancelledError:
                pass
            self.supervisor = None
        self.detach_reader()
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                log("station %s would not stop; killing it" % self.name)
                self.proc.kill()
                await self.proc.wait()
        self.proc = None
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None
        self.set_status(STOPPED)

    async def restart(self):
        """Stop the process and let the supervisor bring it back."""
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
