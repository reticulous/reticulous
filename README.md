# reticulous

## What is this?

**reticulous** is a Reticulum / LXMF / Nomad Network device built on the
[spangap](https://github.com/spangap) platform. It brings Mark Qvist's
[Reticulum Network Stack](https://reticulum.network) (and the LXMF
messaging and Nomad Network text-web layers that sit on it) to a single
hand-held ESP32-S3 device with LoRa, WiFi, GPS, a screen, and a
keyboard.

The first hardware target is the **LilyGo T-Deck Plus** (ESP32-S3FN16R8,
8 MB octal PSRAM, 16 MB flash, SX1262 LoRa, GPS, 320×240 LCD, QWERTY,
trackball).

## What this org owns

reticulous is decomposed into **straddles** — dual-side (firmware +
browser) building blocks that compose into a final device image.
This org holds the reticulous family of straddles; the
[spangap](https://github.com/spangap) org holds the platform straddles
the device builds on.

| Repo                                                                 | What it adds                                                       |
| -------------------------------------------------------------------- | ------------------------------------------------------------------ |
| [rns](https://github.com/reticulous/rns)     | RNS protocol core (identity, path table, Links, Resources) + the µR fork |
| [iface-tcp](https://github.com/reticulous/iface-tcp)       | RNS-over-TCP transport                                             |
| [iface-auto](https://github.com/reticulous/iface-auto)     | AutoInterface transport (IPv6 link-local multicast + unicast UDP)  |
| [iface-espnow](https://github.com/reticulous/iface-espnow) | RNS-over-ESP-NOW transport (long-range PHY, no IP stack)           |
| [iface-lora](https://github.com/reticulous/iface-lora)     | SX1262 LoRa transport with RNode on-air framing                    |
| [lxmf](https://github.com/reticulous/lxmf)     | LXMF messaging (multi-identity, Link + Resource transfer)          |
| [nomad](https://github.com/reticulous/nomad)   | Nomad Network page client                                          |
| [maps](https://github.com/reticulous/maps)                           | Offline RGB565 slippy-map viewer (LCD + GPS, no RNS dep)           |
| [reticulous](https://github.com/reticulous/reticulous) | The app/buildable: assembles the device image (browser SPA, LittleFS data, LCD icons) and pulls in the whole family + the spangap platform |
| [hw-tdeck](https://github.com/reticulous/hw-tdeck)   | Board HAL straddle: T-Deck Plus board (display/touch/keyboard input HAL, GNSS, RTC) + its pin maps/hardware kconfig |
| [hw-heltecv4](https://github.com/reticulous/hw-heltecv4) | Board HAL straddle: Heltec WiFi LoRa 32 V4 (Vext rail, LoRa CS park) + its pin maps/hardware kconfig |

The buildable is **`reticulous/reticulous`**; the `hw-*` straddles are
board HALs that don't decide what the device does — they only make a
board usable. You build the app *with* a board:

```sh
spangap build reticulous/reticulous --with reticulous/hw-tdeck      # T-Deck Plus (has a screen → on-device UI)
spangap build reticulous/reticulous --with reticulous/hw-heltecv4   # Heltec V4 (headless)
```

`reticulous/reticulous` pulls in the whole reticulous family plus the
spangap IP/web platform; the board supplies pin maps, bring-up hooks and
hardware kconfig (flash size, PSRAM mode, display), and a board with a
screen additionally installs the on-device LCD UI itself.

## Why Reticulum?

Reticulum is a cryptography-based networking stack for building
resilient, self-configuring networks over anything that can carry
packets — LoRa, packet radio, plain TCP/IP, even a serial wire. There
is no central authority and no assigned addresses: every node is a
self-generated cryptographic identity, links are end-to-end encrypted
by default, and the network keeps routing over cheap, low-bandwidth,
intermittently-connected links where conventional stacks fall over.

reticulous is a port of that stack — and of the LXMF messenger and the
Nomad Network page browser — onto a small hand-held device that lives
mostly off-grid.

## How reticulous fits on spangap

The reticulous family is **the first serious application** built on
the spangap platform — it drives spangap's API and tests every assumption
the platform makes. Most reticulous straddles depend on a few spangap
straddles each (spangap-core for the base runtime, spangap-net for the
IP transports, spangap-web for the browser SPA, spangap-lcd for the
on-device LVGL UI). The buildable straddle (`reticulous/reticulous`)
pulls in everything; a board straddle (`hw-tdeck` / `hw-heltecv4`) is
added with `--with` to supply the hardware.

Anything *platform-wide* — ITS, storage, the unified filesystem, the
browser shell, the WebRTC plumbing, remote access (ACME / DuckDNS /
UPnP / WireGuard) — lives in the spangap org and is documented at
[github.com/spangap/spangap](https://github.com/spangap/spangap). Read
that side first if you want to understand the substrate; come back
here for the reticulous-specific protocol pieces and the T-Deck app.

## Status

Past scaffold. The µR fork is ported and patched, and the per-task
firmware is real and hardware-verified against upstream Reticulum /
LXMF — `rnsd` (identity, Transport, path table) and `lxmf` (messaging,
per-contact threads, Link + Resource transfer) are the large modules,
with `tcp` / `auto` / `lora` / `espnow` transports live. The browser
SPA is built out: per-transport settings panes, status floating
windows, full LXMF chat UI. The on-device LVGL UI is built out: LXMF
messenger, Nomad browser, offline maps.

The authoritative architecture + rollout plan lives in
[hw-tdeck/docs/component-plan.md](https://github.com/reticulous/hw-tdeck/tree/main/docs/component-plan.md)
— several decisions there are non-obvious; read it before working on
anything sizeable.

## Hardware

- **LilyGo T-Deck Plus** — primary target. ESP32-S3FN16R8 (16 MB
  flash, 8 MB octal PSRAM), SX1262 LoRa, 320×240 LCD, QWERTY,
  trackball, GPS (Quectel L76K or u-blox MIA-M10Q depending on batch).
- Other ESP32-S3 + SX1262 boards with PSRAM should be feasible with a
  new board-HAL straddle. The Heltec WiFi LoRa 32 V3 was evaluated and
  rejected — no PSRAM.

## On Claude Code

The overwhelming majority of reticulous (and spangap) has been written
by Claude Code, over about a month. A project of this scope would
have been an order of magnitude more work for one person without LLMs.
That said, every public header has been read and iterated on by hand,
and almost every architectural decision is human. The browser code is
mostly Claude's so far. The doc files (this one included) are written
to stay human-readable and to help further development, whether by
humans or AI tools.

### Security note

The design has had separate context spent on security and is at least
*planned* to be securable, but this code as it stands today should not
be used where your life depends on it being unhackable.
