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
machine you are on: fast enough that a dozen stations are nothing, and
faithful to nothing below the C. The trade is deliberate. What the testbed is
for is protocol behaviour across several nodes over time — announces, paths,
carrier sense, retries, messages — and none of that lives in the instruction
set. What it cannot catch is anything that depends on the chip being a chip:
timing at the microsecond, memory layout, cache behaviour, a peripheral's
errata.

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

## What a station has instead of hardware

| | On a board | Here |
|---|---|---|
| identity | the chip's MAC | the node id, from the environment |
| `/fixed` | a read-only image in flash | a link to the build's merged data tree |
| `/state` | LittleFS on a partition | a directory the process `chdir()`ed into |
| NVS | a flash partition | a file under `/tmp`, sized from the built partition table |
| addresses | WiFi | one loopback address per station |
| console | a serial port | stdin and stdout, plus a TCP CLI |
| radio | an SX1262 | a model, and the ether |

## What this cannot tell you

- Anything timed below the tick, and anything that depends on the chip's own
  timing — BUSY after a wake, a peripheral's ramp, an errata.
- Memory: the heap ignores capabilities and wraps libc, so PSRAM pressure,
  DMA-capable allocation and internal-RAM exhaustion are all invisible.
- The radio's physics. At this depth there are no positions, no path loss and
  no capture: a frame is heard by everyone tuned to it, or by nobody. See
  [`../ether/INTERNALS.md`](../ether/INTERNALS.md) for what the medium does
  and does not decide.
- Anything below the C: the compiler, the ABI and the word size are the
  host's. Code that assumes a 32-bit `long` or pointer fails here and not
  on the chip, which makes the host build a free audit of width assumptions,
  and a clean run here is not a clean run on a board.
