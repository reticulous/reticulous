# Reticulous on sim-mesh: what its station has instead of hardware

| | On a board | Here |
|---|---|---|
| identity | the chip's MAC | the node id, from the environment |
| `/fixed` | a read-only image in flash | a link to the build's merged data tree |
| `/state` | LittleFS on a partition | a directory the process `chdir()`ed into |
| NVS (non-volatile storage) | a flash partition | a file under `/tmp`, sized from the built partition table |
| addresses | WiFi | one loopback address per station |
| console | a serial port | a pty, bridged to the map's terminal window, carrying framed RPC as a board's USB console does |
| radio | an SX1262 | sim-mesh's virtual SX1262, libsimradio-sx1262.so, and the ether |

The pty matters: a station's stdin and stdout **are** its serial console, so
sim-mesh's supervisor holds the master end and the console window is that
pty over a websocket.

## The one rule that makes interrupts real

Reticulous's LoRa driver waits on DIO1, the chip's interrupt line; a driver
that polls the IRQ (interrupt request) register over the bus never meets
this. The GPIO (general-purpose input/output) shim (hw-linux) is a pin
table, and the whole reason it exists is a single behaviour:

> a level-triggered pin whose interrupt is enabled while its line is asserted
> fires immediately.

That is the property the LoRa driver's interrupt handling rests on — the
trampoline disables the interrupt, the task drains whatever raised it, and
re-enables; a line still high re-fires. Without it, a frame that completed
behind a disabled interrupt would be a hung task here and a serviced one on
hardware.

A handler runs on whichever task moved the line, which is what an interrupt
does. The shim reads and writes its table under a critical section but calls
the handler outside it: a handler ends in a yield, and on this port a critical
section is a per-thread signal mask with a global nesting count, so yielding
from inside it hands the section to the wrong thread.

## The radio's services, and the board's clock

iface-lora's `src/host/simradio_glue.cpp` hands the virtual radio this
port's services (esp_timer, a FreeRTOS critical section, a FreeRTOS task over
a non-blocking socket) from a constructor, before anything reaches the
library, and implements hw-linux's clock hooks (`hwLinuxClock*`) on the
library's public node-time calls. The firmware links the radio by name
(`-lsimradio-sx1262`) from sim-mesh's `radio/build`, or `SIM_MESH_RADIO_DIR`.

## The rules a host-only file obeys on ESP-IDF's host target

Six, and they are not negotiable — each one is a way this port breaks.

**No FreeRTOS task blocks in a host system call.** The port only knows a task
is blocked when it blocked on a FreeRTOS primitive; a task sitting in `recv`
is, to the scheduler, the running task, and it starves everything below it.
So sockets and stdin are non-blocking, and the only waits are `select()`,
FreeRTOS primitives and `vTaskDelay`. `select()` is hw-linux's: it blocks the
task until a descriptor is ready, woken by a signal that lands on the running
task's thread the way the tick does, so a quiet station's tasks sleep rather
than poll. No busy-waiting.

**Nothing wakes at tick rate while nothing happens.** The kernel is tickless:
while every task is blocked the tick stops, and a station costs nothing
until the first of them is due. A task that loops on a one-tick delay or a
one-tick timeout keeps the tick running, and in a virtual-time run wakes the
whole run every 10 ms of T. A task waits on what it serves — its
notification, `hwLinuxWait()` on its descriptors and its inbox — for as long
as nothing comes, and a timeout is the time something is actually due,
rounded **up** to a whole tick: rounded down, a deadline inside the current
tick is a wait of zero, and the task spins until it comes — on a chip for up
to a tick, and in a virtual-time run until the busy watchdog lets T move,
20 ms of wall each time. Where the shared source polls on a chip, the host's
behaviour goes behind the board's weak `hwLinuxWait` or into `src/host/`,
and the chip's path stays as it is.

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
resolve at executable link time. The same rule is why the virtual radio is
linked by the interface that drives it rather than by the board that wires it.

## The board's clock in virtual time

FreeRTOS's tickless idle runs only when every task is blocked and knows the
tick the first of them is due at; the glue's `hwLinuxClockIdle` says the
station is idle from there. `esp_timer` counts node time from the whole
second of it in which the station joined, its next expiry and the next tick a
task waits for are wakes, so `until` is the earlier of the two, and the link
opens at board bring-up, since nothing in the station can wait on time
before the ether has said what T is. The tick is not a timer: the board steps
the tick count to node time every time T moves (`simradio_on_advance`),
before anything due at the new T runs, and the port's `setitimer` is
stopped. A station whose tasks sleep for a second wakes the run once in that
second, not a hundred times; and because every station's clock is a whole
number of seconds from every other's, the ticks of all of them fall on the
same instants of T and share their barriers.

## Its firmware zip

`spangap make-builds` in a catalogue whose `hw-sim-mesh-<arch>` entries say
`category: reticulum` and `driver: sim-mesh/driver.py` leaves the zip sim-mesh
installs, `builds/<catalogue>/reticulous_hw-sim-mesh-<arch>_<stamp>.zip`: the
station, its `/fixed` tree, this directory's `driver.py`, the shared
libraries beyond libc and libstdc++ (zlib, libbsd, libmd) under `lib/`, and a
node.yaml naming the firmware `reticulous-<catalogue>-sx1262_<arch>_<stamp>`.
`sim firmware add` takes it as it is.
