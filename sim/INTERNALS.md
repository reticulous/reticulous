# The simulated testbed — internals

Why the testbed is shaped the way it is, and the rules anything added to it
has to obey. [README.md](README.md) is how to run it and where every file
lives; this is the reasoning underneath.

## The idea in one paragraph

A station is the whole firmware, compiled for ESP-IDF's Linux host target and
run as an ordinary process. The cut between the real code and the simulated
part is the **SPI bus**: everything above it — the LoRa driver, its carrier
sense and airtime accounting, Reticulum, LXMF, the web UI — is the same source
that runs on a board, and what sits below it is a model of an SX1262 that
hands its transmissions to a medium instead of to an antenna.

```
Reticulum / LXMF / web UI          the same code as on a board
        │
   iface-lora                      the same driver, the same IRQ handling
        │  RadioLibHal
   VirtualHal ── GPIO shim         in place of the SPI bus and the pins
        │
   VirtualSx126x                   the chip: commands, registers, frame timing
        │  UDP, JSON
    the ether                      who hears what, and when
```

## Why a host port and not an emulator

An emulator runs the image on a modelled CPU: faithful to the silicon, one
core per station, and slow. A host port compiles the same sources for the
machine you are on: fast enough that two dozen stations are nothing, and
faithful to nothing below the C. The trade is deliberate. What the testbed is
for is protocol behaviour across several nodes over time — announces, paths,
carrier sense, retries, messages — and none of that lives in the instruction
set. What it cannot catch is anything that depends on the chip being a chip:
timing at the microsecond, memory layout, cache behaviour, a peripheral's
errata.

## One process

`simd.py` is the ether, the stations, the proxy and the control server in one
asyncio loop. It could have been four processes talking over sockets, and the
launcher it replaces was three. One is better for one reason that matters:

> **Positions reach the medium by method call.**

Dragging a station on the map is `ether.place()` between two frames. There is
no wire to define, nothing to serialize, no version to keep in step, and no
window in which the map and the medium disagree about where something is. The
ether keeps a `main()` and a `--scenario` flag so it still runs alone for its
own tests, but the testbed never uses that path.

The price is that one crash takes the lot down. That is the right price here:
a testbed is a person's session, and a medium that outlived its stations, or
stations that outlived their medium, would be a worse thing to debug than a
process that stopped.

## Nothing blocks

Every wait is an awaitable. A station's pty is a reader on the loop, its CLI
is spoken over asyncio streams with a timeout, and the websocket handlers are
coroutines. The one rule that follows: **a subscriber must not be able to stop
the medium.** The ether calls its `on_tx`/`on_rx`/`on_station` subscribers
inline and swallows what they raise, because a page that has gone away is not
the air's problem.

## One port, and `Host` decides

The control page and every station's web UI are on the same published port.
The front listener reads just enough of each request to find `Host`:
`<name|id>.sim.localhost` is proxied to that station's own `127.0.0.1<id>:80`,
and everything else is proxied to the aiohttp app, which is bound to a
loopback port nothing outside can reach.

That shape — a proxy in front of the control app, rather than the control app
routing to stations — is what keeps a station's URL space its own. The page's
websocket, the station's websockets and the station's absolute links all work
because after the head is read the connection is a raw byte pump and the
station is answering on its own origin.

A name resolves through the loaded scenario, a number through arithmetic. Both
because a scenario's nodes get renamed and a bookmark should survive it, and
because the proxy alone — for a station set somebody started another way — has
no scenario to ask.

## Two things worth keeping, and why they are not one thing

A network has a design and it has a history, and conflating them makes both
useless. A saved thing that always carried the stations' state could not
describe a network that has not happened yet; a saved thing that never carried
it could not bring one back.

So there are two. A **scenario** is the design — geometry, air, setup lines —
and is one readable file. A **snapshot** is a scenario plus every station's
state store. Loading a scenario is a factory reset of the whole testbed;
loading a snapshot restores a moment.

That split is what makes a scenario *reproducible*: load it twice and you get
the same network twice, because there is nothing in it that could have been
inherited from the last run. A scenario that quietly kept the previous
network's identities would behave differently on the second load than on the
first, and the difference would be invisible in the file.

Half of a network is its identities, keys, paths and message history, which is
why snapshots exist at all and why they are a directory rather than a file.

## The run is a copy, and a snapshot is taken live

The stations run in `sim/run/`, never in a scenario or a snapshot. Without a
working copy there would be no moment at which the thing on disk was not
already changed, since the stations are always writing.

A snapshot is taken **while the stations run**. Waiting for quiescence would
mean stopping a network to photograph it, and a testbed is watched rather than
batched. What makes that honest is the flush below; what makes it safe is that
the firmware's store commits whole files, which is a guarantee it already has
to meet for a power cut.

`run/` is overwritten rather than removed and remade, because the ether's
record is open inside it and belongs to the process rather than to any one
scenario. Logs and the record stay there and are never copied out: they are an
account of one run, and a scenario is what to run.

## Reset, factory reset, and why neither is called "apply"

Two verbs, and the difference is exactly the difference a person expects:

- **Reset** presses reset. The process exits, the supervisor brings it back,
  and the state store is untouched.
- **Factory reset** stops the station, deletes its `state/`, and starts it
  again. On the way back up the directory is empty, which is precisely what a
  node clicked onto the map for the first time is — so the setup lines run
  again by the ordinary path and not by a special case.

There is deliberately no verb that re-sends the setup lines to a running
station. "Re-apply" reads like a repair and behaves like one sometimes and not
others: a line that is a setting takes effect, a line that is an action happens
twice. What replaces it is **Run command**, which types one CLI line at every
running station and shows what each said. That is more versatile — retune the
whole testbed, survey it, create something on all of it — and it is honest
about being a thing you did rather than a state you restored.

## Why the store is flushed before anything is taken away

`s.storage.flash_delay` is 60 seconds by default: a write sits in RAM for up to
a minute before the store commits. On a board that is a power-cut window and
entirely fair. Here it would make two things lie.

A station **reset moments after its setup lines ran** would come back with none
of them, and the testbed would be claiming a configuration it never made
durable. So simd sends `save` at the end of the setup lines, and again a few
seconds later — not everything setup asks for lands at once, and an LXMF
identity reaches the store some seconds after the command that created it has
returned.

A **snapshot** copied out of a store with a minute of writes still in RAM would
be a picture of a moment that never quite existed. So every running station is
flushed before the copy, and before any stop or reset.

All of it is best effort with a short timeout. A station that will not answer
is one whose store cannot be flushed, and refusing to stop it over that would
be worse than losing the last minute.

`save` is simd's to send rather than the file's because it is not a setting.
Everything in the file describes what a station *is*; `save` is about making
that description stick.

## Setup lines, and the macros

A scenario configures its stations by typing at them. The alternative — a
schema of settings the testbed knows the names of — would have to grow every
time the firmware grew one, and would be a second place for a setting's name to
live.

The lines are expanded per station: `{name}`, `{id}` and `{addr}`. That is what
lets one shared list say node-specific things, and it is why simd adds no
settings of its own. An earlier design had simd send `hostname <name>` ahead of
the lists, on the grounds that the name is the node's identity in three places
at once — the map label, the proxy hostname and the station's own name — and
must not drift. The macro gets that guarantee more cheaply: `hostname {name}`
is one shared line, visible in the file and editable, and it cannot disagree
per node because there is only one of it.

A macro the list does not define is left exactly as written. A CLI line is
somebody's text and may legitimately contain braces, and silently emptying
something that only looked like a macro is worse than passing it through for
the station to complain about.

The lines run only on an empty store, which is what makes it safe for them to
contain `lxmf create {name}` — a line that is emphatically not idempotent.
Nothing re-runs them against a configured station; a factory reset empties the
store first.

Whether a station has been set up is sampled **at the fork**, not when the CLI
answers: the firmware writes `state/boot` moments after it starts, and the
answer the setup step needs is the one from before it ran.

## The WebRTC relay, and why it is a relay rather than a second transport

A station's web UI reads every `s.*` value over a `storage:1` WebRTC
DataChannel. There is no HTTP path for it, so without a DataChannel the page
loads and then knows nothing: no hostname, no settings, no Activity.

The obvious fix was a host-only WebSocket transport carrying the same
merge-patches. It was the wrong one. A transport that exists only in the
testbed is a code path a board never runs, so the thing being tested stops
being the thing that ships — which is the one promise this testbed makes.

So the station speaks real WebRTC here, from the same source, and what is
sim-only is the plumbing that makes it reachable:

- **Signalling.** simd keeps `/webrtc` for itself — the one station route the
  front listener does not forward — and terminates the WebSocket on both
  sides, so the SDP answer arrives as a parsed message. It rewrites the
  connection line and candidate to the relay's own address and drops the
  station's, which point inside the container and would only cost the browser
  timeouts. The browser's cookie goes up with it, because signalling is behind
  the station's own login and a relay that dropped it would be introducing a
  stranger.
- **Media.** One UDP port in front of every station. The first packet of an
  ICE session is a STUN binding request whose USERNAME begins with the
  answerer's ufrag — the same ufrag simd read out of that station's answer —
  so the packet says which station it belongs to without simd having to
  understand anything else about it. After that the browser's address is
  pinned to a flow with its own socket, and both directions are forwarded
  bytes-for-bytes.

Nothing is decrypted or inspected past the STUN username: DTLS and SCTP are
end-to-end between the browser and the station exactly as on a board.

What this cost the firmware is three functions —
`webrtc_port.{h,cpp}`: the local addresses to advertise, the address the socket
binds, and a CRC32 the chip has in ROM. The rest of ICE, DTLS and SCTP built
for the host unchanged. The bind is the one that matters and is easy to miss:
a chip has a network stack to itself and binds the wildcard, while here every
station is a process on one stack, so each binds its own loopback address or
the second one to start finds the port taken.

## Status, and what `up` means

`stopped` → `starting` → `setup` → `up`, with `restarting` for the gap after an
exit nobody asked for. `up` is **the station answering on its TCP CLI**, not
the process existing: a firmware process that has forked but not finished
booting is not a station you can do anything with, and the map should not
claim otherwise.

`transport` is not status and is not read from the scenario. simd asks each
running station for `s.rnsd.transport_enabled` every few seconds, because the
setting is live and a person can flip it in that station's own web UI — the
map should show what the station thinks, not what the scenario last said.

## The page

One Pinia store owns the websocket and everything on it. Every component reads
the store; every action is one store method that sends one message. A
reconnect replays the `snapshot`, so the page holds no state simd cannot
restate — which is the whole of what makes simd restartable under a page that
is open.

The map is a **canvas**. A grid, hundreds of pulses a minute and a drag at
60 Hz are all much cheaper drawn than laid out, and none of them wants to be
an element. The projection is the ether's own, equirectangular around the
scenario origin, so a pixel and a metre agree by construction rather than by
two implementations staying in step.

A drag sends `node_move` at a few Hz with `settle: false`, and once more on
release with `settle: true`. The ether is updated on every one, so a link
visibly fades as a station is pulled away; the scenario file is written only
on the last, so a drag is one edit rather than fifty.

Frames arrive as `tx` and `rx` and are drawn on the **browser's** clock: the
ether's microseconds are its own, and the only thing in a `tx` that means
anything here is how long the frame occupies the air.

## What a station has instead of hardware

| | On a board | Here |
|---|---|---|
| identity | the chip's MAC | the node id, from the environment |
| `/fixed` | a read-only image in flash | a link to the build's merged data tree |
| `/state` | LittleFS on a partition | a directory the process `chdir()`ed into |
| NVS | a flash partition | a file under `/tmp`, sized from the built partition table |
| addresses | WiFi | one loopback address per station |
| console | a serial port | a pty, bridged to the map's terminal window, plus a TCP CLI |
| radio | an SX1262 | a model, and the ether |

The pty matters: a station's stdin and stdout **are** its serial console, so
the supervisor holds the master end and the console window is that pty over a
websocket. Keystrokes go as binary frames and the terminal size as a JSON text
frame, so no byte a person can type is special to the transport.

## The one rule that makes interrupts real

The GPIO shim ([`hw-linux`](../../hw-linux/README.md)) is a pin table, and the
whole reason it exists is a single behaviour:

> a level-triggered pin whose interrupt is enabled while its line is asserted
> fires immediately.

That is the property the LoRa driver's interrupt handling rests on — the
trampoline disables the interrupt, the task drains whatever raised it, and
re-enables; a line still high re-fires. Without it, a frame that completed
behind a disabled interrupt would be a hung task here and a serviced one on
hardware, and the testbed would be lying about the one path it most needs to
tell the truth about.

A handler runs on whichever task moved the line, which is what an interrupt
does. The shim reads and writes its table under a critical section but calls
the handler outside it: a handler ends in a yield, and on this port a critical
section is a per-thread signal mask with a global nesting count, so yielding
from inside it hands the section to the wrong thread.

## The seam: a bus, not a chip class

The model implements the **wire**, not the driver's idea of a radio. A
transmission arrives at it as a byte frame with an opcode, exactly as the
driver would put it on a bus, and the reply comes back as the status byte and
the data the datasheet describes. That placement is what gives the testbed its
value: the driver's own command sequences, its IRQ masks, its read-modify-write
of the sensitivity register and its interrupt handling all execute, unchanged
and unaware.

The model is deliberately shallow where depth would buy nothing: mode
transitions are instantaneous, BUSY is never busy, and the GFSK and LR-FHSS
modems, CAD and duty-cycled receive are refused. A `ready_at` field rides on
the wire from the start so the datasheet's timing table can be added later
without moving anything else.

Two things it is **not** shallow about, because everything above the bus
reads them:

- **A frame takes its time on the air.** The transmit timeline is the
  AN1200.13 time-on-air for the modem as configured, so a 250-byte frame at
  SF8/BW125 occupies the medium for two thirds of a second, carrier sense has
  something to sense, and two stations can be talking at once. A model that
  finished a transmission the instant it started would make every collision
  in the testbed impossible, and it would do it silently.
- **A receiver follows one frame at a time.** The medium reports every frame
  that reaches the antenna; the chip locks onto the first, and a frame that
  starts while it is demodulating is not received at all unless it leads the
  one in progress by the capture margin, in which case the receiver drops
  what it had and takes the louder one. Without that the driver would be
  handed whichever frame ended last, and the medium's verdict — which says
  one of the two survived — would mean nothing above the bus.

## Time

Real time, throughout. `esp_timer` is `CLOCK_MONOTONIC` in microseconds from
the first reading, and every timed event in the model — the instant a
preamble ends, a header lands, a frame finishes — is an `esp_timer` one-shot.
The FreeRTOS tick is 100 Hz, so nothing is accurate below ten milliseconds;
the frames the driver sends take tens to hundreds of milliseconds, which is
why that is survivable.

A station's `t` fields are its own clock. They are meaningful only against
each other inside one message, and the ether rebases every frame onto its own
clock before scheduling. The offsets within a frame are the transmitter's and
travel unchanged, because the offsets are what a receiver actually needs.

## The rules a host-only file obeys

Five, and they are not negotiable — each one is a way this port breaks.

**No FreeRTOS task blocks in a host system call.** The port only knows a task
is blocked when it blocked on a FreeRTOS primitive; a task sitting in `recv`
is, to the scheduler, the running task, and it starves everything below it.
So sockets and stdin are non-blocking, and the only waits are `select()` —
which IDF interposes when lwIP is off, polling and then sleeping on a delay —
FreeRTOS primitives, and `vTaskDelay`. No busy-waiting.

**The console writes with `write(2)`.** The tick signal can land inside a libc
call that is not async-signal-safe and switch to a task that makes the same
call. The log sink and the CLI assemble their line and put it on the
descriptor.

**Every task stack is at least 20 KB.** A task is a pthread and its stack is a
real mapping; the port's own floor is 16 KB and a host stack frame is several
times a Xtensa one. `spawnTask` raises anything smaller, and drops core
affinity — there is one core, and asking for the second is an assertion
failure.

**Chip-only code leaves, host-only code arrives.** Behind
`#if !CONFIG_IDF_TARGET_LINUX` or out of the source list; a separate file in
`src/host/` in preference to an `#ifdef` inside a function.

**The board is reached through weak symbols, never through a dependency.**
Host-only code keeps wanting the station's identity and addresses, and those
belong to the board straddle — which arrives with `--with` and is in nobody's
`requires:`. A platform or feature straddle may not depend on one. So a file
that needs `hwLinuxNodeId()`, `hwLinuxBindAddr()` or `hwLinuxEtherAddr()`
declares it `extern "C" __attribute__((weak))` with a sane default and lets it
resolve at executable link time. The same rule is why the chip model lives
with the interface that drives it rather than with the board that wires it.

## What this cannot tell you

- Anything timed below the tick, and anything that depends on the chip's own
  timing — BUSY after a wake, a peripheral's ramp, an errata.
- Memory: the heap ignores capabilities and wraps libc, so PSRAM pressure,
  DMA-capable allocation and internal-RAM exhaustion are all invisible.
- The radio's physics below the path-loss model. There is no fading, no
  antenna pattern, no per-spreading-factor sensitivity threshold and no noise
  that varies with what else is in the air; a level is computed once from the
  geometry and is the same for every frame between one pair. See
  [`../ether/INTERNALS.md`](../ether/INTERNALS.md) for what the medium does
  and does not decide.
- Anything below the C: the compiler, the ABI and the word size are the
  host's. Code that assumes a 32-bit `long` or pointer fails here and not
  on the chip, which makes the host build a free audit of width assumptions,
  and a clean run here is not a clean run on a board.
