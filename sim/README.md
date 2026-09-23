# The simulated testbed

Stations are firmware processes on the Linux host target, each on its own
loopback address, talking to one another over a virtual radio. One command
starts the lot and opens a map in your browser; everything you do to the
network, you do on the map.

```
host                     container                                 browser
spangap sim ──exec──► simd.py ─┬─ ether        (UDP, in-process)
                               ├─ stations     (firmware processes, one pty each)
                               ├─ proxy        <name>.sim.localhost:9011 ─► 127.0.0.1<id>:80
                               └─ control      localhost:9011  ◄──── websocket ──── the map
```

It is the same firmware, built for a different target — not an emulator and
not a way to run this software on a Linux machine. [INTERNALS.md](INTERNALS.md)
says how it works and why it is built this way.

## The build

The testbed builds no firmware. Build the station binary once, for the
`spangap/hw-linux` board, from the workspace root:

```sh
spangap build reticulous/reticulous --with spangap/hw-linux \
    --with reticulous/netgraph \
    -x reticulous/rnsh -x reticulous/iface-auto -x reticulous/iface-ble \
    -x reticulous/rnode-ble -x reticulous/nomad -x reticulous/maps \
    -x spangap/viewer -x spangap/acme -x spangap/duckdns -x spangap/sshd \
    -x spangap/upnp -x spangap/wg
```

The excluded straddles are the ones not built for this target. netgraph is not
in the image by default, but a scenario whose stations share a community
(`s.netgraph.community`) needs it: the community's membership announce is what
carries each node's gateway distance. The result is
`../esp-idf/build.linux/reticulous.elf`, which is what a station is, with its
`/fixed` tree in `../esp-idf/build.linux/data_merged/`.

Every target builds in its own `esp-idf/build.<target>/`, so a chip build
(`build.esp32s3/`) and this one never touch each other's files, and switching
between them rebuilds nothing.

## Running it

```sh
spangap sim          # on the host, from anywhere in the workspace
```

That starts one process in the container — the ether, the stations, the proxy
and the control page — and opens `http://localhost:9011/` when it answers. It
runs in the foreground: Ctrl-C stops it, and stops everything it started.
The first run builds the page, which takes a minute; after that it is built
only when its sources have changed.

`spangap sim --dev` serves the page hot from Vite instead, for working on the
page itself, and opens that instead. The stations stay on 9011 either way —
simd is what fronts them, so their URLs do not move because the page is being
worked on.

Inside the container, `spangap sim` does the same without the browser, and
`python3 simd.py` in this directory is the process on its own. It takes
`--bind` (default `0.0.0.0:9011`), `--ether` (default `127.0.0.1:7000`),
`--elf` and `--fixed`.

## The map

The page opens empty. **Scenario ▸ New…** makes a network, **Load…** opens one
that is already on disk.

| | |
|---|---|
| drag the background | pan |
| wheel | zoom about the cursor |
| right-click the background | **New node here** — asks a name and puts a station down |
| hover a station | the lines to everything within earshot, with the level on each |
| drag a station | move it; the medium is updated as you drag, so you can watch a link fade |
| click a station | its card: status, position, transport, radio, and **Web UI**, **Console**, **Reset**, **Factory reset**, **Setup**, **Remove** |

A dot is grey stopped, amber starting or in setup, white up, red restarting. A
second ring around it means the station is acting as a Reticulum transport
node — read live from the station itself, so flipping it in that station's own
web UI shows here.

A transmission draws an expanding ring from the transmitter for as long as the
frame occupies the air. Each station that hears it flashes green for a clean
reception and red for a CRC failure — so a collision is two rings overlapping
and a row of red flashes.

The grid is in metres, at 1, 2 or 5 times a power of ten, whichever keeps the
lines 60 to 150 pixels apart. The brighter cross is the scenario's origin, and
the view is remembered per scenario.

## A scenario, and a snapshot

Two things are worth keeping, and they are kept apart.

A **scenario** is the network as designed and nothing that has happened to it:
where the stations stand, what the air is like, and the CLI lines each of them
is set up with. It is one file.

```
sim/scenarios/<name>.yaml
```

A **snapshot** is a scenario plus everything the stations have since become —
their identities, keys, paths and message history, as the firmware keeps them.

```
sim/snapshots/<name>/scenario.yaml
sim/snapshots/<name>/nodes/<node name>/state/
```

Loading a scenario gives a factory-fresh network, reproducible from a file you
can read. Loading a snapshot gives back a network that had been running.

```yaml
origin: [52.3740, 4.8897]           # lat, lon of the map's centre
physics: { exponent: 2.7, noise_figure_db: 6, capture_db: 6 }
setup:                              # CLI lines every station is given, in order
  - "hostname {name}"
  - "auth passwd admin admin"
  - "lxmf create {name}"
  - "lora up"
  - "lora 0 freq 869.525"
  - "lora 0 sf 8"
  - "lora 0 bw 125"
nodes:
  alpha:
    id: 1
    pos: [52.3740, 4.8897]
    setup:                          # this station's own lines, after the scenario's
      - "set s.rnsd.transport_enabled 1"
  bravo:   { id: 2, pos: [52.3740, 4.9030] }
  charlie: { id: 3, pos: [52.3740, 4.9295] }
obstructions:
  - { between: [alpha, charlie], db: 60 }
```

**A node is a name and a number.** The name is the station's hostname, the
label on the map and the hostname the proxy routes; the number is its loopback
address, `127.0.0.1<id>`, and its `SPANGAP_NODE_ID`. The proxy answers to both,
so `alpha.sim.localhost` and `1.sim.localhost` are the same station.

**Positions are latitude and longitude in degrees.** The ether projects them to
metres on an equirectangular plane around `origin`, so a scenario placed on
real ground needs only its origin moved.

### Setup lines

They are CLI commands — what you would type at the station — so every setting
the firmware has or grows is reachable without the testbed knowing its name.
The scenario's list runs first, then the node's own.

Three macros are filled in per station, which is what lets one shared list say
node-specific things:

| Macro | Becomes |
|---|---|
| `{name}` | the node's name — `alpha` |
| `{id}` | its station number — `1` |
| `{addr}` | its loopback address — `127.0.0.11` |

Anything else in braces is left exactly as written. The file is the whole of
what a station is told: nothing is added behind your back, which is why
`hostname {name}` is an ordinary line you can see and change.

The lines run **when a station boots with no state** — a node you have just
clicked onto the map, and every node after a factory reset. They do not run
again on an ordinary reset, because the station is already set up.

The one line that is not a setting is `lxmf create {name}`: run twice it makes
two identities. That is safe here because the lines only ever run on an empty
station, but it is worth knowing before putting it through **Run command**.

## The verbs

**On one station** (its card) **and on the whole testbed** (the Simulation menu):

| | |
|---|---|
| **Reset** | presses reset. The process exits and comes straight back; its state is untouched, so it is the same station it was. |
| **Factory reset** | wipes its state, restarts it, and the setup lines run again on the empty store. Identities, keys, paths and message history go; the map and the lines do not. |

Across the whole testbed both are spread over the same `--stagger` window the
start uses, and for the same reason: every station announces itself as it comes
up, and two dozen of those at once is a collision storm no fleet of real boards
would ever have.

**Simulation ▸ Run command…** types one CLI line at every running station and
lists what each one said. The macros are expanded per station, so
`lora 0 freq 869.475` retunes the whole testbed and `rns` surveys it. This is
the general tool: there is no separate verb for re-sending the setup lines.

Beside the line is **spread**, in seconds. Left at 0 every station is asked at
once, which is what a question wants — nothing goes on the air to answer
`show s.net.hostname`. Anything that *transmits* wants a spread: two dozen
stations running `lora 0 a` in the same instant is a collision storm rather
than an announcement, and what comes back describes the storm. Thirty or sixty
seconds across the fleet is enough.

**Scenario** — New, Load, Save, Save As, and Settings (the air and the setup
lines). **Snapshot** — Load and Save As.

| Verb | What it does |
|---|---|
| **Scenario ▸ New** | an empty scenario, written at once so it has a file |
| **Scenario ▸ Load** | the design only: every station comes up with no state and runs its setup lines |
| **Simulation ▸ Start all** | start whatever is stopped |
| **Scenario ▸ Save** | write the map and the lines back over the scenario |
| **Snapshot ▸ Save As** | keep this design *and* every station's state under a new name |
| **Snapshot ▸ Load** | bring both back — the network as it was |

Stations keep running across a snapshot: they are flushed first and the copy is
taken while they run, which for a store that commits whole files is the same
guarantee a power cut gives a board. Loading either kind stops them.

The scenario is **dirty** from the first change after a load or a save — a
moved node, a new one, a removed one, a physics change. The header shows a dot
beside the name, and Load and New ask before discarding it. State is not part
of that: a station writing to its store is not a change to the design.

### The run

The live run is neither a scenario nor a snapshot. It is `sim/run/`:

| Path | What it is |
|---|---|
| `run/scenario.yaml` | the live map, written on every change |
| `run/nodes/<name>/state/` | the station's state store |
| `run/nodes/<name>/log` | everything the station wrote to its console, across restarts |
| `run/record.tsv` | every message in and out of the ether |

Logs and the record are the run's own and are never copied into a save.
`sim/scenarios/`, `sim/snapshots/` and `sim/run/` are all on the workspace bind
mount, so they survive the container and can be copied about. None of the three
is committed.

### Starting a fleet

Stations are started **spread over a minute**, not all at once. Two dozen
firmware processes forking in the same instant is a thundering herd against
one host, and the network it makes is worse than the load: stations that boot
together announce together, so the opening minute is a collision storm that no
fleet powered up by hand would ever have. `simd --stagger <seconds>` changes
the spread; `0` starts them together.

The map fills in as they come up, and the page stays live throughout — the
start runs behind the menu rather than holding it.

## Who can hear whom

There are no stated links. Where a station stands is the whole of it: the level
a frame arrives at is

```
L = P_tx + G_tx + G_rx − PL(d)
PL(d) = FSPL(1 m, f) + 10·n·log10(d) + obstruction(tx, rx)
```

with `n` the scenario's exponent — 2 is free space, 2.7 suburban, and higher
numbers bring the neighbourhoods in closer. A frame that arrives below the SNR
its spreading factor needs — −7.5 dB at SF7, down to −20 dB at SF12 — is not
delivered at all, and that is what "out of range" means here. So a link is in
range only if it is one the modem could actually hold, and moving to a slower
spreading factor really does reach further. An **obstruction** is a per-pair constant in dB, which is how two
stations near each other are put out of each other's reach.

Two frames that share a carrier and any instant of air interfere, and each
receiver rules on them for itself: a frame survives where it leads everything
else that station could hear by the capture margin.
[`../ether/README.md`](../ether/README.md) has the whole of what the medium
decides.

## Reading the record

`seq.py` draws `run/record.tsv` as a sequence diagram — one lifeline per
station, one arrow per station that heard a frame, the verdict at each arrow
head, and the Reticulum packet read out on the right:

```
$ python3 seq.py --tail 4
   t (s)   delta    india    kilo     mike     papa    sierra
   0.118     │        ◀────────┼────────┼────────┤        │  ANNOUNCE  single/4e3874cc  of rnstransport.probe  hops=0  167B
             │        │        │        │        ├────────▶
   0.250     ✗────────┼────────┼────────┼────────┼────────┤  ANNOUNCE  single/edec275b  of rnstransport.probe  hops=0  167B
             │        │        │        │        ✗────────┤
```

One transmission heard by two stations is two arrows on two rows, sharing the
timestamp and the reading: every arrow has one end at the station that
transmitted, so nothing in the picture can be read as a frame travelling
between two stations that cannot hear each other.

The lifelines are named from the loaded scenario. `--only <words>` keeps the
rows whose reading matches, `--record <file>` reads a record kept from an
earlier run, and `--scenario <path>` names a different one.

## A station's own doors

Each station serves its web UI on port 80 of its own loopback address, which is
invisible outside the container; the proxy on the published port 9011 routes by
hostname:

    http://alpha.sim.localhost:9011/    by name
    http://1.sim.localhost:9011/        by number

Chrome and Firefox resolve any `.localhost` name to loopback with no
configuration. Safari does not, and needs entries in `/etc/hosts` on the Mac:

    127.0.0.1  alpha.sim.localhost bravo.sim.localhost charlie.sim.localhost

Port 9011 is the testbed's own: the container publishes it at the same number
on the host, beside flashmon's 9010 and clear of the 9000–9009 range `spangap
dev` allocates from, so a testbed and a dev server can run at once. A container
made before that mapping existed does not have it — the next host `spangap`
command recreates it, which costs nothing since all state is in bind mounts.

A station's web UI is the **whole** UI, not a static shell: it speaks the same
WebRTC DataChannel to the browser that a board does, from the same firmware
source, so the live panes — settings, the log, the CLI, Activity — all work.

Getting that through takes one piece of plumbing, because the DataChannel is
UDP and the station's own address is inside the container:

```
browser ──ws  <name>.sim.localhost:9011/webrtc──► simd ──ws──► station   signalling
browser ──udp localhost:9011───────────────────► simd ──udp─► station   the channel
```

simd stands in the middle of the signalling so it can point the station's SDP
answer at itself, and relays the UDP behind it — picking the station out of
each packet by the ICE ufrag it saw in that answer. Port 9011 is published on
**UDP as well as TCP** for it; a container made before that mapping existed is
recreated by the next host `spangap` command.

Neither end knows. The station is answering ICE from a peer that happens to be
a relay, and the browser is talking to a station that happens to be simulated —
which is the point: the code under test is the shipping code, on both sides.

Besides the map, a station is reachable three other ways:

- **Console** on its card — its serial console, in a terminal window over a
  websocket. First-run setup and every CLI command, exactly as a board on a
  cable.
- `nc 127.0.0.1<id> 8081` — its TCP CLI, the same command line, from a shell
  in the container. This is the door simd itself uses for setup.
- `tail -f run/nodes/<name>/log` — everything it has printed, across restarts.

A station that exits is started again, because a restart on this target is a
process exit: a station rebooting itself comes back on the same address with
the same directory.

## After a container restart

Stations are processes, not a service: stopping the container stops them.
Their state is not in the container though — `scenarios/`, `run/` and the
station binary all live in the workspace, which is a bind mount from the host
— so `spangap sim` again, then **Load** the scenario, and the network comes
back with its names, radio settings, identities and message history as they
were.

## The pieces on their own

The medium is its own program with its own docs:
[`../ether/README.md`](../ether/README.md) for what it does and the wire it
speaks, [`../ether/INTERNALS.md`](../ether/INTERNALS.md) for how. It runs alone
against hand-written stations, taking its positions from a scenario file:

```sh
python3 ../ether/ether.py --bind 127.0.0.1:7000 --record record.tsv \
        --scenario scenarios/<name>
python3 -m pytest ../ether/test_ether.py      # its own tests need no firmware
```

The proxy also runs on its own, for a station set started some other way; alone
it routes by station number only, since names are the scenario's:

```sh
python3 proxy.py --bind 0.0.0.0:9011
```

## Where the code lives

Two halves. The **host port** is what makes the firmware build and run as a
process at all; the **simulation** is what gives it a radio and a medium.

### The host port

| Where | What |
|---|---|
| [`spangap/build-system`](../../spangap/build-system/README.md) | a board straddle's `target:`, exported as `IDF_TARGET`; on `linux`, no flashable image |
| [`spangap/hw-linux`](../../hw-linux/README.md) | the board: station identity and directory, the GPIO shim, esp_timer |
| `spangap-core/esp-idf/src/host/` | no power manager, no USB transport, and deflate over the system zlib |
| `spangap-net/esp-idf/src/net_relay.cpp` | the socket relay, shared with the chip: the event bus, the listen sockets, the byte proxy |
| `spangap-net/esp-idf/src/host/` | the link backend — loopback, up from the first instant — in place of the WiFi state machine |
| `spangap-web/esp-idf/src/host/` | the WebRTC half's boot hook, and nothing behind it |

Everywhere else the rule is the same: chip-only code sits behind
`#if !CONFIG_IDF_TARGET_LINUX` or drops out of the source list, and host-only
code lives in that component's `src/host/`.

### The simulation

| Where | What |
|---|---|
| `iface-lora/esp-idf/src/host/virtual_sx126x.*` | the chip: commands, registers, payload buffer, and the timing of a frame |
| `iface-lora/esp-idf/src/host/virtual_hal.*` | RadioLib's HAL over the GPIO shim and that model, in place of the SPI bus |
| `iface-lora/esp-idf/src/host/ether_task.*` | the station's one UDP link to the ether |
| [`../ether/`](../ether/README.md) | the medium: positions, path loss, who hears a frame and how it comes out |
| `simd.py` | the process: the ether, the stations, the proxy, the control server |
| `stations.py` | one firmware process, its pty, its log, its supervisor |
| `scenario.py` | the scenario directory: load, save, save as, reload, new |
| `setup.py` | the setup lines, sent to a station over its TCP CLI |
| `proxy.py` | the hostname proxy |
| `ui/` | the control page (Quasar 2 on Vue 3, one Pinia store) |
| `seq.py` | the record as a sequence diagram |

Nothing above the bus is aware of any of it: the LoRa driver, its CSMA and
airtime accounting, Reticulum, LXMF and the web UI are the same code that runs
on a board.
