#!/usr/bin/env python3
"""A hostname-routing HTTP reverse proxy in front of the stations.

Only published container ports are reachable from a browser outside the
container, so one port fronts every station: `Host: <id>.sim.localhost` is
routed to that station's own loopback address, `127.0.0.1<id>:80`, which
keeps each station's canonical URL space and its websockets intact.

The request head is parsed only far enough to read `Host`; after it is
forwarded the connection is a raw two-way byte pump, so keep-alive,
chunked bodies and a WebSocket upgrade all pass through untouched.
"""

import argparse
import asyncio
import re
import sys

HOST_PATTERN = re.compile(rb"^host:[ \t]*([^\r\n]+)", re.IGNORECASE | re.MULTILINE)
STATION_PATTERN = re.compile(r"^(\d{1,3})\.sim\.localhost$", re.IGNORECASE)

MAX_HEAD = 64 * 1024
STATION_PORT = 80


def log(msg):
    sys.stderr.write("proxy: %s\n" % msg)
    sys.stderr.flush()


def station_host(host_header):
    """The loopback address behind `<id>.sim.localhost`, or None."""
    name = host_header.decode("latin-1").strip()
    name = name.rsplit(":", 1)[0] if name.count(":") == 1 else name
    found = STATION_PATTERN.match(name)
    if not found:
        return None
    node = int(found.group(1))
    if not 1 <= node <= 99:
        return None
    return "127.0.0.1%d" % node


async def read_head(reader):
    """Everything up to and including the blank line that ends the head."""
    head = b""
    while b"\r\n\r\n" not in head and b"\n\n" not in head:
        chunk = await reader.read(4096)
        if not chunk:
            return head or None
        head += chunk
        if len(head) > MAX_HEAD:
            return None
    return head


async def pump(reader, writer):
    """Copy one direction until it closes, then half-close the far side."""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def refuse(writer, status, text):
    body = text.encode("utf-8")
    writer.write(b"HTTP/1.1 " + status.encode("ascii") + b"\r\n"
                 b"Content-Type: text/plain; charset=utf-8\r\n"
                 b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                 b"Connection: close\r\n\r\n" + body)
    try:
        await writer.drain()
    except ConnectionError:
        pass


async def handle(client_reader, client_writer):
    peer = client_writer.get_extra_info("peername")
    upstream_writer = None
    try:
        head = await read_head(client_reader)
        if not head:
            return
        found = HOST_PATTERN.search(head)
        target = station_host(found.group(1)) if found else None
        if target is None:
            log("no station in %r from %s" % (
                found.group(1)[:60] if found else None, peer))
            await refuse(client_writer, "404 Not Found",
                         "No station for that hostname. "
                         "Use http://<id>.sim.localhost:<port>/\n")
            return

        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                target, STATION_PORT)
        except OSError as err:
            log("cannot reach %s:%d (%s)" % (target, STATION_PORT, err))
            await refuse(client_writer, "502 Bad Gateway",
                         "Station at %s:%d is not answering.\n" % (target, STATION_PORT))
            return

        upstream_writer.write(head)
        await upstream_writer.drain()
        await asyncio.gather(pump(client_reader, upstream_writer),
                             pump(upstream_reader, client_writer))
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        for writer in (upstream_writer, client_writer):
            if writer is not None:
                try:
                    writer.close()
                except OSError:
                    pass


async def serve(host, port):
    server = await asyncio.start_server(handle, host, port)
    bound = ", ".join("%s:%d" % s.getsockname()[:2] for s in server.sockets)
    log("listening on %s, routing <id>.sim.localhost to 127.0.0.1<id>:%d"
        % (bound, STATION_PORT))
    async with server:
        await server.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description="hostname-routing proxy for the stations")
    ap.add_argument("--bind", default="0.0.0.0:9011",
                    help="host:port to listen on (default 0.0.0.0:9011)")
    args = ap.parse_args(argv)
    host, _, port = args.bind.rpartition(":")
    try:
        asyncio.run(serve(host or "0.0.0.0", int(port)))
    except KeyboardInterrupt:
        log("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
