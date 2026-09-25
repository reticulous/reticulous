#!/usr/bin/env python3
"""Setup lines, sent to a station over its TCP CLI.

A scenario configures its stations the way a person would: by typing at them.
The lines are CLI commands, so every setting the firmware has — or grows —
is reachable without this file knowing its name, and since they are settings
rather than actions, sending them again is harmless.

The channel is the station's own TCP CLI on `127.0.0.1<id>:8081`, which needs
no login and is closed, as on a board, until `set s.net.cli_port 8081` opens
it; a station that has not been given that line refuses the connection for
good. It is line-oriented and
echoes nothing; what marks the end of a command's output is the prompt, a
whole line ending in `"$ "`. So an exchange is: drain to the prompt, write
one line, drain to the prompt again — and what came back in between is the
answer.

Nothing here blocks: every wait is an awaitable with a timeout, and a station
that has not finished booting simply refuses the connection, which is the
signal to try again rather than an error.
"""

import asyncio

CLI_PORT = 8081
PROMPT = b"$ "

CONNECT_TIMEOUT = 2.0       # a station still booting refuses at once anyway
COMMAND_TIMEOUT = 10.0      # `lora up` brings a radio up; give it room


def cli_host(node_id):
    """The loopback address a station's CLI answers on."""
    return "127.0.0.1%d" % node_id


class CliError(Exception):
    """The station's CLI could not be reached, or did not answer in time."""


class Cli:
    """One open session on a station's command line."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def open(cls, node_id, timeout=CONNECT_TIMEOUT):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(cli_host(node_id), CLI_PORT), timeout)
        except (OSError, asyncio.TimeoutError) as err:
            raise CliError("station %d: %s" % (node_id, err)) from err
        session = cls(reader, writer)
        await session.drain_to_prompt()     # the banner and the first prompt
        return session

    async def drain_to_prompt(self, timeout=COMMAND_TIMEOUT):
        """Everything up to the next prompt, with the prompt line removed.

        The prompt is the device's name and `$ `, so it is always the last
        line of what comes back; dropping everything after the final newline
        drops exactly it.
        """
        buf = b""
        try:
            while not buf.endswith(PROMPT):
                chunk = await asyncio.wait_for(self.reader.read(4096), timeout)
                if not chunk:
                    break
                buf += chunk
        except asyncio.TimeoutError as err:
            raise CliError("no prompt after %.0fs" % timeout) from err
        newline = buf.rfind(b"\n")
        text = buf[:newline + 1] if newline != -1 else b""
        return text.decode("utf-8", "replace")

    async def run(self, line, timeout=COMMAND_TIMEOUT):
        """Type one line and give back what the station printed for it."""
        self.writer.write((line + "\n").encode("utf-8"))
        await self.writer.drain()
        return await self.drain_to_prompt(timeout)

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass


async def send(node_id, lines, timeout=COMMAND_TIMEOUT):
    """Type these lines at a station, in order, on one session.

    Blank lines and `#` comments are skipped, so a scenario's setup list can
    be annotated the way a script would be.
    """
    lines = [line for line in lines
             if line.strip() and not line.strip().startswith("#")]
    if not lines:
        return []
    session = await Cli.open(node_id)
    try:
        return [await session.run(line, timeout) for line in lines]
    finally:
        await session.close()


async def ask(node_id, command, timeout=COMMAND_TIMEOUT):
    """Run one command and give back its output."""
    session = await Cli.open(node_id)
    try:
        return await session.run(command, timeout)
    finally:
        await session.close()


async def alive(node_id):
    """True when the station's CLI answers — which is what `up` means here."""
    try:
        session = await Cli.open(node_id)
    except CliError:
        return False
    await session.close()
    return True


async def wait_until_up(node_id, timeout=60.0, interval=0.5):
    """Wait for a station's CLI to start answering. False if it never does."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await alive(node_id):
            return True
        await asyncio.sleep(interval)
    return False


def parse_setting(text, key):
    """The value out of a `show <key>` reply, which is `<key>=<value>`."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line[len(key) + 1:]
    return None
