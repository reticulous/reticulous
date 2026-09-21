# The ether — internals

How the medium decides who hears what, and the choices behind it.
[README.md](README.md) is what it does and the wire it speaks.

## Shape

One `asyncio.DatagramProtocol` on one UDP socket, single-threaded. Everything
is driven from arriving datagrams and from timers the event loop holds:

- a **placement** is where a station stands, in metres, and what its antenna
  adds — held whether or not that station has ever been heard from;
- a **station** is an address, per slot the last `state` message it sent, and
  the instant its own transmission stops occupying it;
- a **frame** is a transmission in flight, on the ether's clock, with the
  receivers it reached and the other frames it shared the air with.

A station is created the first time it is heard from, whatever the message, and
its address is updated on every one — so a station that restarts on a new
ephemeral port is simply followed. A placement is not created that way: it
comes from whatever is driving the run, and a station with none is not in the
medium at all.

The ether runs **inside** the testbed's process rather than beside it, so
positions arrive by method call. That is why there is no wire for them and
nothing to keep in step: dragging a station on the map is one `place()` between
two frames.

## Clocks

Three clocks meet here and only one of them is authoritative.

A station stamps its messages with its own `esp_timer`, which starts at zero
when its process does. Those numbers mean nothing between stations. So on every
`tx` the ether reads only the **offsets** — `t_pre − t0`, `t_hdr − t0`,
`t_end − t0` — and rebases them onto its own monotonic clock at the instant the
datagram arrived. The `rx_begin` it sends carries ether microseconds; the
receiver, in turn, cares only about the gaps between them and schedules from its
own clock. Each hop keeps what it can trust and discards what it cannot.

The offsets are clamped: negative is zero, anything beyond the frame's own span
is the span, and a stated timeline longer than a minute is junk and is cut. A
station cannot make the ether schedule something absurd by stating it.

## The level, and why it is computed

A link used to be a number somebody wrote down. It is now geometry:

```
L = P_tx + G_tx + G_rx − PL(d)
PL(d) = FSPL(1 m, f) + 10·n·log10(d) + obstruction(tx, rx)
```

The free-space term is taken at the **frame's own carrier**, because a
scenario that moves band should see the difference without anything being
rewritten. The exponent is the scenario's, and it is the one knob that decides
how big a network feels: at 2.7 a station is audible for tens of kilometres and
almost everything hears almost everything, and at 3.6 the same field breaks into
neighbourhoods. That is a property of the air, not of the map, which is why it
sits in `physics:` beside the noise figure and the capture margin.

Distance is floored at one metre. Without that, two stations dropped at the
same point make `log10(0)`, and the answer a person wants there is "very loud",
not a crash.

What the levels are *for* has not changed: the medium needs to know which of two
frames is the louder one, and by how much. Computing them rather than reading
them buys the thing a testbed most wants — that moving a station changes what it
can hear, and that the change is the same change a person would reason about.

## The sensitivity threshold

`N = −174 + 10·log10(BW) + NF`, and a frame that does not clear the SNR its
spreading factor needs above it is not delivered: no `rx_begin`, nothing
scheduled, nothing to rule on.

That constant is doing the job the old topology file's silence did. With stated
links, a pair nobody wrote down could not hear each other and that was the end
of it. With positions there is no such thing as no link — path loss is finite
everywhere — so something has to say where earshot stops, and the only honest
place is the noise.

It is **per spreading factor** because that is the whole point of one: SF12 buys
17.5 dB of reach over SF7 and pays for it in air time, and a flat cut would make
the two identical to the medium. It was flat once — 30 dB under the floor, below
every SF's sensitivity — and the cost was a medium that delivered frames no real
receiver could demodulate: a scenario laid out against it drew links that did not
exist and behaved far worse than it looked. The CRC band just above the
threshold, where a frame locks but fails at a probability, is the next thing this
could learn; `welcome` already carries a seed for it.

## Who hears a frame

For each other station, for each of its slots: it must be placed and above its
modem's sensitivity, it must not be transmitting itself, the slot must have last
said `RX`, and its stated `freq`, `bw`, `sf` and `sync` must equal the
transmission's. That last list is one constant — the thing to extend when the
medium learns to care about coding rate, header type or preamble length.

Matching is on the **last stated** values, not on anything the ether infers.
This is why a station publishes a `state` on every command that changes its mode
or carrier, and why a model that forgot to would go deaf silently. The one thing
the ether does infer is a transmitter's own deafness: a `tx` is not a `state`,
so a station that never said it had left `RX` would otherwise be told about
frames arriving during its own, and a hidden terminal would look like a station
ignoring what it could hear.

## Collisions, and why each receiver rules for itself

Two frames interfere when they share a carrier and overlap in time at all. Each
frame keeps the list of frames it shared air with, and the verdict is computed
**per receiver** at that receiver's `rx_end`: the frame survives if its level
there leads every interferer that station could hear by the capture margin, and
is `crc` otherwise.

That is the difference between a medium and a referee. A collision is not a
property of a transmission — it is what happened at one antenna. Two stations
that cannot hear each other transmit over one another constantly; the station
between them keeps whichever frame is loud enough to be worth keeping, and a
station out of one transmitter's reach never notices the collision at all.

The interference list is held on the frame rather than recomputed from the
frames still in flight, so a reception that ends long after its interferer has
been pruned still knows what spoiled it. Scheduling an `rx_end` does not settle
it: a frame still in the air when a second one starts is spoiled retroactively
for receivers already told it was arriving, which is exactly what a radio does.

An interferer's level is recomputed at the verdict rather than remembered from
when it was delivered. A station can be dragged across the map while two frames
are in the air, and the answer that matters is where it was when the reception
ended.

## A frame's two names

A station numbers its own transmissions and knows nothing of anyone else's, so
two stations can have frames in the air under the same number — which happens
the moment a testbed is restarted, since every station starts counting again. So
the ether gives each frame a number of its own and uses that one in `rx_begin`
and `rx_end`. A receiver following one frame while a second arrives, and a
reader matching an end back to its beginning, both need a name that is unique
across the air, and only the medium can issue one.

## Delivery

`rx_begin` goes out immediately, so the receiver can arm its preamble, sync and
header interrupts on the offsets. `rx_end` is a timer at the frame's stated
span. Nothing re-reads the frame in between — a receiver that leaves `RX`
mid-frame is not told, and discards the reception itself.

Both carry the link's level: `rx_begin` as `level`, which is what an
instantaneous RSSI reads and what carrier sense acts on, and `rx_end` as `rssi`
with `snr` the same figure above the noise floor. Both are rounded to whole dB
on the wire, because the station reads them as integers; the verdict is decided
on the unrounded figures, so a pair 6.4 dB apart captures and a pair 5.6 dB
apart does not, whatever the numbers the receiver is shown.

The payload is passed through as the base64 string it arrived as. The ether
never decodes it, which is what keeps it honest: it cannot accidentally know
anything about Reticulum.

## Events, and the rule about subscribers

`on_tx`, `on_rx` and `on_station` exist so something can watch the air without
reading the record — the map, at sixty frames a second. They are plain
callables, and an exception from one is logged and swallowed. A page that has
gone away, or a subscriber with a bug in it, must not be able to stop the
medium: the ether's job is the frames, and everything watching is optional.

## What is deliberately not here

- **Fading and per-frame variation.** A level is computed once from the
  geometry and is the same for every frame between one pair. No shadowing, no
  multipath, no antenna pattern, no rain.
- **The CRC band.** The threshold is the spreading factor's own, and above it a
  frame is delivered. A real receiver also has a few dB above that threshold
  where a frame locks but fails its CRC at a probability. `welcome` already
  carries a `seed` so that band, when it arrives, has a reproducible generator
  to draw from.
- **Orthogonality.** Two frames on one carrier interfere whatever their
  spreading factors, though a real receiver can often demodulate through a
  frame at another SF. The medium is pessimistic here, and knowingly.
- **A referee.** The ether does not judge a station's behaviour — it does not
  check that a transmission was preceded by carrier sense, or that a duty cycle
  was respected. The record is there so something else can.
- **Virtual time.** It runs on the event loop's real clock. Stations are
  processes in real time and a person is in the loop.
