#!/usr/bin/env python3
"""A hostname-routing HTTP reverse proxy in front of the stations.

Only published container ports are reachable from a browser outside the
container, so one port fronts every station: `Host: <label>.sim.localhost` is
routed to that station's own loopback address, `127.0.0.1<id>:80`, which keeps
each station's canonical URL space and its websockets intact. The label is the
station's name or its id, so `alpha.sim.localhost` and `1.sim.localhost` reach
the same station and a renamed node is still reachable by number.

Which labels exist is not the proxy's business: `serve()` takes a `resolve`
callable that turns one label and request path into a `(host, port)` to dial,
or None for a request with nowhere to go. The path is there so a caller can
keep one route to itself — simd takes `/webrtc` that way, to stand in the
middle of a station's signalling. The label is None when the `Host` is not a
`.sim.localhost` name at all, which is how simd puts the control page on the
same port as the stations it fronts: one listener, and the `Host` decides
whether a request is for a station or for the map. Run alone, the proxy
resolves station numbers and nothing else.

The request head is parsed only far enough to read `Host`; after it is
forwarded the connection is a raw two-way byte pump, so keep-alive, chunked
bodies and a WebSocket upgrade all pass through untouched.
"""

import argparse
import asyncio
import re
import sys

HOST_PATTERN = re.compile(rb"^host:[ \t]*([^\r\n]+)", re.IGNORECASE | re.MULTILINE)
LABEL_PATTERN = re.compile(r"^([A-Za-z0-9-]{1,63})\.sim\.localhost$", re.IGNORECASE)

MAX_HEAD = 64 * 1024
STATION_PORT = 80
MAX_NODE_ID = 99


def log(msg):
    sys.stderr.write("proxy: %s\n" % msg)
    sys.stderr.flush()


def label_of(host_header):
    """The `<label>` out of a `Host: <label>.sim.localhost[:port]`, or None."""
    name = host_header.decode("latin-1").strip()
    name = name.rsplit(":", 1)[0] if name.count(":") == 1 else name
    found = LABEL_PATTERN.match(name)
    return found.group(1).lower() if found else None


def resolve_by_id(label, path=""):
    """The fallback resolver: a bare station number, and nothing else."""
    if not label or not label.isdigit():
        return None
    node_id = int(label)
    if not 1 <= node_id <= MAX_NODE_ID:
        return None
    return ("127.0.0.1%d" % node_id, STATION_PORT)


PATH_PATTERN = re.compile(rb"^[A-Z]+ ([^ \r\n]*)")


def request_path(head):
    """The path off the request line, for a resolver that routes on it."""
    found = PATH_PATTERN.match(head)
    return found.group(1).decode("latin-1") if found else ""


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


async def handle(client_reader, client_writer, resolve):
    peer = client_writer.get_extra_info("peername")
    upstream_writer = None
    try:
        head = await read_head(client_reader)
        if not head:
            return
        found = HOST_PATTERN.search(head)
        target = resolve(label_of(found.group(1)) if found else None,
                         request_path(head))
        if target is None:
            log("no station in %r from %s" % (
                found.group(1)[:60] if found else None, peer))
            await refuse(client_writer, "404 Not Found",
                         "No station for that hostname. "
                         "Use http://<name>.sim.localhost:<port>/ or "
                         "http://<id>.sim.localhost:<port>/\n")
            return

        host, port = target
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        except OSError as err:
            log("cannot reach %s:%d (%s)" % (host, port, err))
            await refuse(client_writer, "502 Bad Gateway",
                         "Nothing is answering at %s:%d.\n" % (host, port))
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


async def serve(host, port, resolve=resolve_by_id):
    """Start the proxy and give back the server, for a caller to close."""
    async def on_client(reader, writer):
        await handle(reader, writer, resolve)

    server = await asyncio.start_server(on_client, host, port)
    bound = ", ".join("%s:%d" % s.getsockname()[:2] for s in server.sockets)
    log("listening on %s, routing <name|id>.sim.localhost to the station's "
        "own 127.0.0.1<id>:%d" % (bound, STATION_PORT))
    return server


async def serve_forever(host, port, resolve=resolve_by_id):
    server = await serve(host, port, resolve)
    async with server:
        await server.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description="hostname-routing proxy for the stations")
    ap.add_argument("--bind", default="0.0.0.0:9011",
                    help="host:port to listen on (default 0.0.0.0:9011)")
    args = ap.parse_args(argv)
    host, _, port = args.bind.rpartition(":")
    try:
        asyncio.run(serve_forever(host or "0.0.0.0", int(port)))
    except KeyboardInterrupt:
        log("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
