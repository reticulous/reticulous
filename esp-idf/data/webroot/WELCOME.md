# Welcome to Reticulous

This device is a complete, self-contained **Reticulum** mesh node
(reticulum.network). It carries the **Messages** (LXMF) app, the
**Nomad** page browser, a remote shell that works over the mesh, and
interfaces that run Reticulum over LoRa radio, WiFi and TCP — all driven
from a full web interface and a unix-like command line. It interoperates
with the wider Reticulum ecosystem: Sideband, MeshChat, NomadNet and any
other RNS node.

There are no accounts and no servers. Your identity is a keypair
generated on this device, addresses are cryptographic hashes, and
everything is end-to-end encrypted by Reticulum itself.

*(Reticulous is young software. It is built to be securable, but don't
bet a life on it being unhackable yet.)*

---

## First steps

**1. Get it on your WiFi.** Fresh out of the box the device starts its
own open access point, named `reticulous_` plus four hex characters.
Connect to it and browse to **https://192.168.1.1** (accept the
certificate warning — expected on first contact). Set the device password
on the setup page, then join your own network under
**Settings → Internet → WiFi**. From then on the device is at
**https://reticulous.local**. CLI equivalent: `net add MyNetwork mypass`.

**2. Turn on a mesh interface.** They are all **off** by default — see
the next section.

**3. Create a messaging identity.** Open **Messages** and add one, or in
the CLI: `lxmf create alice`.

The device announces itself periodically (every 30 minutes, or
`lxmf announce` to do it now) — and you're on the network.

## Mesh interfaces

Reticulum doesn't care what medium a packet travels over, and this device
can use several at once. Configure them under
**Settings → Mesh Network → RNS Interfaces**, or with the CLI verbs
below.

### LoRa — long-range radio

For boards with a LoRa radio (T-Deck, Heltec). You **must** choose the
frequency and TX power yourself: legal bands, duty-cycle limits and
antenna rules differ per region, and the right values are those of the
network you want to join.

```
lora 0 freq 869.525   # MHz
lora 0 txp 14         # dBm
lora 0 up
lora                  # status, RSSI/SNR, traffic
```

Defaults follow the common Reticulum-on-LoRa conventions: 125 kHz
bandwidth, spreading factor 7, coding rate 4/5, sync word 0x42. Change
them with `lora 0 bw|sf|cr|sync ...` — both ends of a link must match.

### ESP-NOW — nearby devices, no infrastructure

Mesh over raw WiFi frames between ESP32 devices in range of each other;
no access point involved. All peers must share one 2.4 GHz channel
(`s.espnow.channel`, default 1). Enable with `espnow up`. When the device
is also on a WiFi network, the radio has to follow the access point's
channel; on a mismatch the interface stands down by default
(`s.espnow.conflict_policy`).

### Auto — your local network

Zero-configuration Reticulum over the LAN the device is on. It finds
desktop Reticulum peers (the RNS `AutoInterface`) by itself: `auto up`,
then `auto peers` lists what it heard.

### TCP — the internet

Bridges mesh islands over ordinary TCP. Add an outbound peer — a hub you
run yourself with desktop `rnsd`, or a public Reticulum testnet entry
point:

```
tcp peer add hub.example.net:4965
tcp                   # peers + server status
```

To *accept* inbound connections: `set s.tcp.server_enable=1` (port 4965).

### Routing for others

By default the device is an endpoint. `set s.rnsd.transport_enabled=1`
turns it into a **transport node** that routes traffic for the mesh
around it — good for an always-on node with a wide view. `rnstatus`,
`rnpath` and `rnprobe` are on board for network diagnostics.

For private networks, every interface accepts an IFAC network name and
passphrase (`ifac_netname` and a `secrets. ... .ifac_netkey`) so only
peers holding the key can join.

## Messaging

Use **Messages** in the web UI, the **LXMF** app on the screen, or the
CLI:

```
lxmf create alice       # new identity (up to 4)
lxmf announces          # who's out there
lxmf send <peer> hello  # peer = hash, listing number, or name
lxmf chats              # conversations
lxmf read 3             # message 3 of the last listing
```

Contacts appear from announces and inbound messages; an address is a
32-character destination hash. A message is *sent* once it left over the
best available path, and *delivered* once the peer cryptographically
confirmed it (direct links only — very small messages may travel as
single opportunistic packets, which carry no receipt).

LXMF **stamps** (proof-of-work postage against spam) are supported: the
device pays the cost a peer advertises and advertises cost 16 itself
(`s.lxmf.stamp_cost`; `s.lxmf.enforce_stamps=1` to require stamps on
inbound mail).

Your identity keys exist only on this device (`secrets.rnsd.identity`
and `secrets.lxmf.id.<n>.privkey`). Back them up by reading those keys on
the CLI and storing the values somewhere safe — there is no recovery
without them.

## Browsing

Nomad Network is Reticulum's "text web": nodes host pages you browse
entirely over the mesh. Open the **Nomad** browser in the web UI or on
the screen — nodes the device has heard announce appear as a list. CLI:

```
nomad nodes             # heard nodes
nomad go <hash>         # fetch a node's index page
nomad bookmark add <hash> MyNode
```

Links are live everywhere: `lxmf@<hash>` contact links in pages and page
links in messages open in the right app when tapped.

## Remote shell

The device CLI is reachable *over the mesh* — no IP connectivity needed:

```
auth passwd admin <pw>        # once; shared with web + SSH
set s.rnsh.server.enabled=1
show rnsh.server.dest         # this device's shell address
```

From another Reticulous device: `rnsh <that-hash>`; type `..!` on a line
by itself to disconnect. On IP networks the built-in SSH server does the
same job: `ssh admin@reticulous.local` (after the password is set).

## Maps (T-Deck)

The **Maps** app shows offline map tiles from the SD card, centred on the
GPS fix. Bake tiles for your region on a computer (see the `maps` repo at
github.com/reticulous) and put them under `/sdcard/maps`. Drag to pan,
hold the centre button to re-centre. `gps` in the CLI shows receiver
state; `set s.gps.enable=0` turns the receiver off.

## The device itself

- **Screen (T-Deck):** app launcher with status bar (clock, WiFi,
  battery). Swipe up from the bottom edge to go Home — stop halfway for
  the recents switcher. Escape goes back; holding the home key returns
  to the launcher. `bat` shows the battery.
- **Web UI:** a dock of floating apps — Messages, Nomad, Settings,
  Terminal, Log and this Viewer — live-synced with the device.
- **CLI everywhere:** USB serial, the web Terminal, the LCD CLI app,
  SSH and rnsh all reach the same command line. Start with `help`.
- **One config store:** `show <prefix>`, `set <key>=<value>`, `save`.
  Keys under `s.*` persist and sync to all UIs; `secrets.*` persist but
  never leave the device; everything else is live status. Set your
  timezone: `set s.ntp.tz=Europe/Berlin`.
- **Internet extras** under Settings → Internet: WireGuard tunnel, UPnP
  port mapping, DuckDNS and Let's Encrypt certificates, for reaching the
  web UI from outside your network.

## Good to know

- `set s.rnsd.enable=0` keeps the whole mesh stack down (takes a
  reboot). Everything else in this manual toggles live.
- Path and transport tables are not yet persisted across reboots; the
  mesh re-learns them quickly.
- [BUILD.md](BUILD.md) lists the exact commit of every component in this
  image.

---

*This viewer: press **Space** for the address bar, use **Back** to
retrace links. [About the viewer](ABOUT.md) — source & docs:
github.com/reticulous*
