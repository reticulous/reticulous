# The simulated testbed

Stations are firmware processes on the Linux host target, each on its own
loopback address, talking to one another over a virtual radio: every chip
model sends what it transmits to the ether (`../ether/ether.py`) as UDP JSON,
and the ether hands the frame to every station whose receiver is listening on
the same carrier, bandwidth, spreading factor and sync word.

It is the same firmware, built for a different target — not an emulator and
not a way to run this software on a Linux machine. [INTERNALS.md](INTERNALS.md)
says how it works and why it is built this way.

```
station 1 (127.0.0.11)  ─┐                            browser on the host
station 2 (127.0.0.12)  ─┼─ UDP ─► ether 127.0.0.1:7000      │
station 3 (127.0.0.13)  ─┘         record.tsv                ▼
        ▲ :80                                    proxy 0.0.0.0:9011
        └─────────────────────────────────────────────────────┘
                 Host: <id>.sim.localhost → 127.0.0.1<id>:80
```

## The build

The launcher builds nothing. Build the station binary once, for the
`spangap/hw-linux` board, from the workspace root:

```sh
spangap build reticulous/reticulous --with spangap/hw-linux \
    -x reticulous/rnsh -x reticulous/iface-auto -x reticulous/iface-ble \
    -x reticulous/rnode-ble -x reticulous/nomad -x reticulous/maps \
    -x spangap/viewer -x spangap/acme -x spangap/duckdns -x spangap/sshd \
    -x spangap/upnp -x spangap/wg
```

The excluded straddles are the ones not built for this target. The result is
`../esp-idf/build/reticulous.elf`, which is what the launcher starts.

One build directory serves both targets, so switching between this and a chip
build is a full rebuild unless you park the tree you are not using beside it.

## Three stations

```
python3 run.py --nodes 3
```

That starts the ether on `127.0.0.1:7000`, the proxy on `0.0.0.0:9011` and
stations 1, 2 and 3 from `../esp-idf/build/reticulous.elf`. Ctrl-C stops
everything.

What it leaves behind, all under this directory:

| Path | What it is |
|---|---|
| `nodes/<id>/` | station `<id>`'s state directory: `state/`, `fixed` → the build's `data_merged`, and `log` |
| `nodes/<id>/log` | everything the station wrote to its console, across restarts |
| `record.tsv` | every message in and out of the ether: stamp, direction, station, JSON |

A station that exits is started again — a restart on this target is a process
exit — so a station rebooting itself comes back on the same address with the
same directory.

Useful flags: `--nodes N`, `--console <id>`, `--ether-only`, `--no-proxy`,
`--elf <path>`, `--fixed <dir>`, `--ether host:port`, `--proxy-bind host:port`.

## First run

A fresh station asks for the same answers a board on a cable asks for, and
gives nothing away until it has them. On each station's console, or its TCP
CLI on `127.0.0.1<id>:8081`:

```
auth passwd admin <pw>          Reticulum is held until a password is set
hostname alpha                  the device's own name; the prompt changes to it
lora up                         enable the radio
lora 0 freq 869.525             a radio with no region will not start
lora 0 sf 8
lora 0 bw 125
lxmf create alpha               an identity to send and receive messages as
```

The hostname and the LXMF identity are two different names: the first is the
device — its prompt, its web UI, the name it answers to on the network — and
the second is who a message is from. Give a station both and it is legible
everywhere.

All of it persists in `nodes/<id>/state/`, so a station that restarts comes
back configured; a station whose directory has been deleted starts over.

After that, `lora` shows the radio and its traffic, `lora n` the stations it
can hear, `lxmf announces` the identities it has heard, and `rns` whether the
Reticulum stack is up.

## After a container restart

Stations are processes, not a service: stopping the container stops them, and
nothing brings them back on its own. Their state is not in the container
though — `nodes/` and the station binary both live in the workspace, which is
a bind mount from the host — so there is nothing to set up again:

```sh
spangap docker bash          # on the host; any spangap verb starts the container
cd <workspace>/reticulous/sim
python3 run.py --nodes 3
```

The stations come back on the same addresses with the same directories, so
their names, radio settings, identities and message history are as they were.
Only the run itself is new.

Two things do not survive, and neither matters: a station's emulated NVS,
which is a temporary file the process makes fresh every start in any case, and
the ether's `record.tsv`, which is appended to rather than replaced — delete
it between runs if you want one run's account on its own.

## From a browser

Each station serves its web UI on port 80 of its own loopback address, which
is invisible outside the container; the proxy on the published port 9011
routes by hostname:

    http://1.sim.localhost:9011/        station 1
    http://2.sim.localhost:9011/        station 2

Chrome and Firefox resolve any `.localhost` name to loopback with no
configuration. Safari does not, and needs entries in `/etc/hosts` on the Mac:

    127.0.0.1  1.sim.localhost 2.sim.localhost 3.sim.localhost

Port 9011 is the testbed's own: the container publishes it at the same number
on the host, beside flashmon's 9010 and clear of the 9000–9009 range
`spangap dev` allocates from, so a testbed and a dev server can run at once.
A container made before that mapping existed does not have it — the next host
`spangap` command recreates it, which costs nothing since all state is in bind
mounts. The proxy is plain HTTP and passes WebSocket upgrades straight
through, so the UI's live updates work.

## Consoles

A station's stdin and stdout are its serial console. The launcher gives each
one a pty and keeps the master end, so:

- `python3 run.py --nodes 3 --console 1` puts this terminal on station 1 —
  first-run setup and every CLI command, exactly as a board on a cable.
  Ctrl-`]` detaches (the station keeps running, its output keeps going to its
  log); Ctrl-C then stops the run.
- `nc 127.0.0.11 8081` reaches station 1's TCP CLI, the second door, and
  needs no console attachment.
- `tail -f nodes/1/log` follows a station that has no terminal.

## The ether alone

The medium is its own program with its own docs:
[`../ether/README.md`](../ether/README.md) for what it does and the wire it
speaks, [`../ether/INTERNALS.md`](../ether/INTERNALS.md) for how. Run it
against hand-written stations, or with the launcher's stations pointed at it:

```
python3 ../ether/ether.py --bind 127.0.0.1:7000 --record record.tsv
```

It logs joins, frames and deliveries to stderr and writes every datagram to
the record. Its own tests need no firmware:

```
python3 -m pytest ../ether/test_ether.py
```

The proxy also runs on its own, for a station set started some other way:

```
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
| [`../ether/`](../ether/README.md) | the medium: which stations hear a frame, when, and how it comes out |
| `run.py`, `proxy.py` | the launcher and the hostname proxy |

Nothing above the bus is aware of any of it: the LoRa driver, its CSMA and
airtime accounting, Reticulum, LXMF and the web UI are the same code that runs
on a board.
