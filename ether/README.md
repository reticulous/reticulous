# The ether — the medium between simulated stations

One UDP endpoint that carries LoRa frames between stations. A station tells it
what its radio is doing; when one transmits, the ether works out who hears it
and how loudly, starts their reception and closes it out at the right instant.

It is the medium and nothing else. It holds no firmware, speaks no Reticulum,
and knows a frame only as a carrier, a duration and a payload it never opens.

```sh
python3 ether.py --bind 127.0.0.1:7000 --record record.tsv --scenario <dir>
```

Stations reach it through the testbed, which holds it in its own event loop and
places the stations by direct call — see [`../sim/README.md`](../sim/README.md).
[INTERNALS.md](INTERNALS.md) is how it works inside.

## What it models

**Where a station stands is the whole of who can hear it.** Every station has a
position in metres and an antenna gain; the level a frame arrives at is

```
L = P_tx + G_tx + G_rx − PL(d)
PL(d) = FSPL(1 m, f) + 10·n·log10(d) + obstruction(tx, rx)
```

- `FSPL(1 m, f)` is the free-space loss over the first metre at the frame's own
  carrier — 31.2 dB at 868 MHz.
- `n` is the scenario's path-loss **exponent**: 2 is free space, 2.7 suburban.
  Every step of ten in distance costs `10·n` dB, so at 2.7 a kilometre is 27 dB
  down on a metre and two kilometres 8.1 dB down on one.
- An **obstruction** is a constant in dB between one pair, in both directions.
  It is how two stations within reach of each other are put out of it.
- Two stations at the same point are held one metre apart, so nothing divides
  by nothing.

A station's **noise floor** is `−174 + 10·log10(BW) + NF` — about −117 dBm at
125 kHz with the default 6 dB noise figure. A frame that arrives below the SNR
its **spreading factor** needs is not delivered at all and no `rx_begin` goes
out: that is what "out of range" means here. The thresholds are the SX1262's —
−7.5 dB at SF7, 2.5 dB lower per step to −20 dB at SF12 — so SF12 reaches
17.5 dB further than SF7 and pays for it in air time, and a scenario that moves
SF to reach further sees the difference. The SNR reported with a reception is
the level above the floor.

A frame is heard by a station within that range whose radio last said it was
**receiving** on the same **carrier, bandwidth, spreading factor and sync
word**.

Two frames that share a carrier and any instant of air interfere, and **each
receiver rules on them for itself**: a frame survives where it leads everything
else that station could hear at the same instant by the **capture margin** of
6 dB, and is a CRC failure where it does not. So a station beside one of two
transmitters keeps its neighbour's frame, while a station that hears both
equally keeps neither, out of the same collision.

A station is deaf while its own frame is going out — the radio is half duplex —
which is what makes a hidden terminal behave like one: two stations that cannot
hear each other do not defer to each other either.

Absent at this depth: fading, the CRC band just above the sensitivity threshold,
noise that varies with what else is in the air, and a referee. A level is computed once from the geometry and does not change from
one frame to the next.

## Positions

Whatever drives a run places the stations:

```python
ether.place(sid, x_m, y_m, gain_db)     # metres on a flat plane
ether.obstruct(a, b, db)                # extra loss between one pair
ether.physics = Physics(exponent, noise_figure_db, capture_db)
```

Run alone, `--scenario` takes them from a scenario file — a directory or a
`scenario.yaml` — where they are latitude and longitude, and the ether projects
them onto an equirectangular plane around the file's `origin`. See
[`../sim/README.md`](../sim/README.md) for the format.

A station that has not been placed is not on the plane: it hears nothing and
nothing hears it. That is deliberate — a station the scenario never mentioned
being audible is the surprise nobody wants.

## Watching the air

Whatever holds the ether can subscribe to three callbacks, which is how the
testbed's map is fed:

| Callback | Raised when |
|---|---|
| `on_tx(sid, eid, freq, t_start, t_end)` | a frame goes on the air |
| `on_rx(sid, rsid, eid, verdict, level)` | one station's reception of it closes |
| `on_station(sid, state)` | a station states what its radio is doing |

`levels(sid, freq, bw, power)` answers the other question — everything within
earshot of one station right now, and at what level.

## The wire

JSON, one message per datagram, payloads base64, times in microseconds.
Positions are not on it in either direction: a station never learns where it is.

**Station → ether**

| Message | Says |
|---|---|
| `hello` | this station exists, and which radio slots it has |
| `state` | a slot's mode and carrier — the ether matches on these |
| `tx` | a transmission: its carrier, its power, its three instants, and its payload |

**Ether → station**

| Message | Says |
|---|---|
| `welcome` | joined; the ether's clock origin, its seed, and that it runs in real time |
| `rx_begin` | a frame is arriving: when its preamble, header and end fall, and how strongly |
| `rx_end` | that frame is over: the verdict, the payload, RSSI and SNR |

A station's `t` fields are its own clock and mean something only against each
other within one message. Unknown message types are ignored, on both sides.

The `id` in a `tx` is the transmitter's own count of its frames; the `id` in an
`rx_begin` and `rx_end` is the **ether's**, and no two frames share it. A
receiver has to be able to tell two frames apart while both are in the air, and
the number the transmitter gave each of them cannot do that.

## The record

Every datagram in and out is one tab-separated line of `record.tsv`:

```
<wall-clock stamp>  <in|out>  <station id>  <json>
```

It is the account of what actually happened on the air, against which a
station's own view can be checked — which frames went out, who was told about
them, and how each reception ended.
[`../sim/seq.py`](../sim/seq.py) draws it as a sequence diagram.

## Tests

No firmware needed; they speak the wire over real UDP sockets, and each one
says where its stations stand rather than which links exist.

```sh
python3 -m pytest test_ether.py
```
