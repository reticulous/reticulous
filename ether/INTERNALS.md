# The ether — internals

How the medium decides who hears what, and the choices behind it.
[README.md](README.md) is what it does and the wire it speaks.

## Shape

One `asyncio.DatagramProtocol` on one UDP socket, single-threaded. Everything
is driven from arriving datagrams and from timers the event loop holds:

- a **station** is an address and, per slot, the last `state` message it sent;
- a **frame** is a transmission in flight, on the ether's clock, with the list
  of receivers that locked onto it and one verdict they share.

A station is created the first time it is heard from, whatever the message,
and its address is updated on every one — so a station that restarts on a new
ephemeral port is simply followed.

## Clocks

Three clocks meet here and only one of them is authoritative.

A station stamps its messages with its own `esp_timer`, which starts at zero
when its process does. Those numbers mean nothing between stations. So on
every `tx` the ether reads only the **offsets** — `t_pre − t0`, `t_hdr − t0`,
`t_end − t0` — and rebases them onto its own monotonic clock at the instant
the datagram arrived. The `rx_begin` it sends carries ether microseconds; the
receiver, in turn, cares only about the gaps between them and schedules from
its own clock. Each hop keeps what it can trust and discards what it cannot.

The offsets are clamped: negative is zero, anything beyond the frame's own
span is the span, and a stated timeline longer than a minute is junk and is
cut. A station cannot make the ether schedule something absurd by stating it.

## Who hears a frame

For each other station, for each of its slots: the slot must have last said
`RX`, and its stated `freq`, `bw`, `sf` and `sync` must equal the
transmission's. That list is one constant — the thing to extend when the
medium learns to care about coding rate, header type or preamble length.

Matching is on the **last stated** values, not on anything the ether infers.
This is why a station publishes a `state` on every command that changes its
mode or carrier, and why a model that forgot to would go deaf silently.

## Collisions, and why the verdict is shared

Two frames collide when they share a carrier and overlap in time at all. Both
are marked `crc`, and the mark lives on the **frame**, not on the delivery: a
collision spoils a transmission for every receiver at once, and each scheduled
`rx_end` reads the verdict when it fires, not when it was scheduled. So a
frame that is still in the air when a second one starts is spoiled
retroactively for receivers that have already been told it was arriving —
which is exactly what a radio does.

Frames are pruned once their air is past; after that nothing can collide with
them.

## Delivery

`rx_begin` goes out immediately, so the receiver can arm its preamble, sync
and header interrupts on the offsets. `rx_end` is a timer at the frame's
stated span. Nothing re-reads the frame in between — a receiver that leaves
`RX` mid-frame is not told, and discards the reception itself.

The payload is passed through as the base64 string it arrived as. The ether
never decodes it, which is what keeps it honest: it cannot accidentally know
anything about Reticulum.

## What is deliberately not here

- **Physics.** No positions, no path loss, no antenna pattern, no noise floor,
  no capture. Levels are constants. A frame is heard by everyone tuned to it
  or by nobody, and the only way to lose one is a collision.
- **A referee.** The ether does not judge a station's behaviour — it does not
  check that a transmission was preceded by carrier sense, or that a duty
  cycle was respected. The record is there so something else can.
- **Virtual time.** It runs on the event loop's real clock. Stations are
  processes in real time and a person is in the loop.

Each of those is an addition the wire already has room for: `welcome` carries
a `mode` and a `seed` so a later virtual-time or reproducible mode has
somewhere to announce itself, and `rx_begin` carries a `level` that is a
constant today and need not stay one.
