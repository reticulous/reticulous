# The ether — the medium between simulated stations

One UDP endpoint that carries LoRa frames between stations. A station tells it
what its radio is doing; when one transmits, the ether decides who hears it,
starts their reception and closes it out at the right instant.

It is the medium and nothing else. It holds no firmware, speaks no Reticulum,
and knows a frame only as a carrier, a duration and a payload it never opens.

```sh
python3 ether.py --bind 127.0.0.1:7000 --record record.tsv
```

Stations reach it through the testbed launcher, which starts it for you — see
[`../sim/README.md`](../sim/README.md). [INTERNALS.md](INTERNALS.md) is how it
works inside.

## What it models

A frame is heard by a station whose radio last said it was **receiving** on
the same **carrier, bandwidth, spreading factor and sync word**. There is one
piece of physics: if two frames share a carrier and any instant of air, both
end as a CRC failure, for every receiver at once.

Everything else is absent at this depth — no positions, no path loss, no
antenna gain, no capture effect, no referee. Level, RSSI and SNR are constants
(−80 dBm and 10 dB), so a station either hears a frame cleanly or does not
hear it at all.

## The wire

JSON, one message per datagram, payloads base64, times in microseconds.

**Station → ether**

| Message | Says |
|---|---|
| `hello` | this station exists, and which radio slots it has |
| `state` | a slot's mode and carrier — the ether matches on these |
| `tx` | a transmission: its carrier, its three instants, and its payload |

**Ether → station**

| Message | Says |
|---|---|
| `welcome` | joined; the ether's clock origin, its seed, and that it runs in real time |
| `rx_begin` | a frame is arriving: when its preamble, header and end fall, and how strongly |
| `rx_end` | that frame is over: the verdict, the payload, RSSI and SNR |

A station's `t` fields are its own clock and mean something only against each
other within one message. Unknown message types are ignored, on both sides.

## The record

Every datagram in and out is one tab-separated line of `record.tsv`:

```
<wall-clock stamp>  <in|out>  <station id>  <json>
```

It is the account of what actually happened on the air, against which a
station's own view can be checked — which frames went out, who was told about
them, and how each reception ended.

## Tests

No firmware needed; they speak the wire over real UDP sockets.

```sh
python3 -m pytest test_ether.py
```
