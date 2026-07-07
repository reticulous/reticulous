# reticulous — internals

Maintainer reference for the **buildable**. The [README](README.md) is the
operator/project overview; this document is for changing how the device image is
assembled without breaking it. It is self-authoritative.

`reticulous/reticulous` is an **assembler**, not a component. It ships no
firmware sources and no board HAL of its own — its `esp-idf/main/` carries only
the build-generated entry point. What it owns is the recipe: which straddles
compose into the image, the browser SPA shell, the LittleFS data image, the LCD
launcher icons, and the partition/flash configuration the build derives.

## 1. What this straddle adds

Relative to a bare ESP-IDF project, the buildable contributes:

- **The composition manifest** — `straddle.yaml`'s `additional_installs:` list,
  which names the reticulous mesh family + the spangap IP/web platform that get
  staged into the image (§2).
- **No application sources.** `esp-idf/main/` registers exactly one generated
  translation unit (`staging/spangap_init_dispatch.gen.cpp`) plus any
  `conditional/<name>/` app-level code (none today). There is no `main.cpp` and
  no hand-written `app_main` (§3).
- **The browser SPA shell** — `web-interface/`, a Quasar/Vue app that hosts each
  staged straddle's browser half. Its straddle registrations and `package.json`
  dependency list are build-generated (§4).
- **The factory data image** — `data/`, baked into the read-only `fixed`
  partition: the compiled web root (`data/webroot/`) and the LittleFS
  `factory_state/` storage seed (§5).
- **The LCD launcher icons** — `assets/lcd-icons/*.svg`, rasterized into the
  image only when a screen board is in the build (§6).
- **Firmware identity** — `stub`, `display_name`, `banner`, `default_hostname`,
  `version`, exposed read-only to the browser as `fw.*`. The buildable wins these
  over every dependency.

Everything else — the RNS engine, the interfaces, messaging, the platform
runtime — lives in the straddle that owns it. This straddle decides only what
goes in and how it is laid out.

## 2. The `additional_installs` cascade and `--without`

`straddle.yaml`'s `additional_installs:` is the kitchen sink in one place:

```
additional_installs:
  - reticulous/rns
  - reticulous/iface-lora
  - reticulous/iface-tcp
  - reticulous/iface-auto
  - reticulous/iface-espnow
  - reticulous/lxmf
  - reticulous/nomad
  - reticulous/maps
  - spangap/spangap-net
  - spangap/spangap-web
```

`spangap build` resolves this list transitively and stages each resolved
straddle into `esp-idf/staging/components/<short>/` (short, fork-friendly
component names), picked up by `esp-idf/CMakeLists.txt` via
`EXTRA_COMPONENT_DIRS`. Two list members are deliberately implicit:

- **The remote-access services** (ACME / UPnP / WireGuard / DuckDNS) are *not*
  listed — `spangap-net` `additional_installs` them itself, so requiring net is
  enough to pull them in. (That is why `staging/components/` contains `acme`,
  `upnp`, `wg`, `duckdns`, `sshd` even though `straddle.yaml` never names them.)
- **The on-device LCD UI** (`spangap-lcd`) is *not* listed either — it rides on
  the board. A board with a screen (`hw-tdeck`) `additional_installs`
  `spangap-lcd` itself; a screenless board (`hw-heltecv4`) builds headless with
  no extra flags.

**`--without <name>`** drops a straddle from the staged set, and the cascade
takes its hard dependents with it. Everything in the list is default-on and
droppable this way. Conversely, **`--with <board>`** adds a board straddle whose
HAL sources (`tdeckStart`, `gpsInit`, …) the generated `main` links against — a
buildable's `main` REQUIRES the *full* staged set, including `--with`'d
straddles it never names (`staging/main_requires.cmake`, written by
spangap-inside).

**Why the managed-component dance.** Staged straddles are *not* listed in
`esp-idf/main/idf_component.yml` — listing them there too would double-stage them
under the manager's `managed_components/<org>__<repo>/` namespace and cause
duplicate-symbol link errors. But the component manager does not recurse into
locally-staged components' dependencies, so the buildable's `idf_component.yml`
must surface every *third-party managed* dependency any staged straddle (or
`--with`'d board) transitively uses — today `jgromes/radiolib` (iface-lora's
radio) and `espressif/esp_lcd_touch_gt911` (the hw-tdeck input HAL, fetched but
never linked on a Heltec build).

## 3. The build-generated entry point

The buildable ships no `main.cpp`. spangap-inside writes the entire entry point
into `esp-idf/staging/spangap_init_dispatch.gen.cpp` on every build, by scanning
each staged straddle's `services:` / `start:` / `init:` / `settings:`
declarations. The generated `app_main()` is a fixed skeleton:

```
app_main():
  spangapRegisterServices()      // construct + register every staged straddle's
                                 //   Service, in init_order (platform band
                                 //   core/net/web/lcd, then dependency-topo)
  serviceRunStart()              // onStart walk: bare-hardware board bring-up
                                 //   (power rails, HAL register) the platform
                                 //   depends on — e.g. tdeckStart, tdeckLcdStart
  spangapInit()                  // platform core foundations (spangap-core)
  spangapSettingsGenDefaults()   // declarative `settings:` storage defaults
  serviceRunInit()               // onInit walk: every staged straddle, ecosystem up
  spangapSettingsGenRegister()   // declarative `settings:` LCD panes
                                 //   (only when spangap-lcd is staged)
  spangapPostAppInit()           // finalise
```

`spangapRegisterServices()` is generated in the same file: it constructs each
staged straddle's boot object and appends it to one ordered registry, mixing
`services:` classes (constructed via a per-straddle trampoline,
`spangapService_<Class>`) with legacy `start:`/`init:` hooks (`storageInit`,
`netInit`, `rnsdInit`, `loraInit`, `tcpInit`, `lxmfInit`, …) that the generator
wraps in adapter Services. `serviceRunStart` / `serviceRunInit` then walk that one
registry, and an object joins a phase by overriding its `onStart` / `onInit`.
Adding a straddle that declares `services:` (or a `start:`/`init:` hook) is enough
to have it run; there is no file to edit. `when:`-gated entries appear only when
their gating straddle is staged.

The `spangap*` platform prototypes (`spangapInit`, `spangapPostAppInit`, the
settings generators) come from `spangap.h`, and the registry entry points
(`serviceRegister`/`serviceRunStart`/`serviceRunInit`) from `service.h`; hook
forward decls use default C++ linkage. `main/CMakeLists.txt` registers the `.gen.cpp`
guarded on `EXISTS`, so a raw `idf.py` invocation without a prior `spangap build`
still configures (it simply has no `app_main` to link).

The declarative `settings:` blocks of each straddle are compiled here too:
`spangapSettingsGenDefaults()` emits the `storageDefault(...)` calls (e.g.
`s.rnsd.enable=1`, `s.lora.0.mode="gateway"`) and `spangapSettingsGenRegister()`
emits the LCD settings panes — one source feeding both storage defaults and the
on-device UI. The browser half of those same descriptors is rendered at runtime
by `GeneratedPanel.vue` (§4).

## 4. The browser SPA shell

`web-interface/` is a Quasar/Vue 3 SPA (`name: reticulous-web`) that is the host
shell — `MainLayout.vue` owns the layout, router, auth gate, and floating
windows, and **composes each staged straddle's browser half** into itself. It
hosts the platform baseline windows (CLI, System Log, Settings from
`spangap-web`) alongside the straddle-provided ones it imports directly
(`NodesWindow`/`MapWindow` from rns, `MessagesWindow` from lxmf, `NomadWindow`,
`ViewerWindow`). The composition is build-generated:

- `web-interface/src/boot/straddles.gen.ts` is written each build (parallel to
  the firmware's generated boot registration). It imports and calls every staged
  straddle's `browser_register:` entry (`registerNet`, `registerRnsd`,
  `registerLora`, `registerTcp`, …) in init order, and carries
  `GENERATED_PANELS` (the declarative `settings:` descriptors) + `APP_ICONS`
  (the launcher SVGs for the bottom Dock). Each `browser_register:` entry is a
  `call:` (the `registerXxx` hook name) plus an optional `module:` — the import
  path inside the package, needed only when the browser entry file's name
  differs from the repo, e.g. `rns → modules/rnsd`, `iface-tcp → modules/tcp`;
  `lxmf`/`nomad` omit it and default to `modules/<repo>`. So declaring
  `browser_register:` in a straddle's `straddle.yaml` is the *only* step to
  surface its UI — no `straddles.gen.ts` and no `modules.ts` edit.
- `web-interface/src/boot/modules.ts` loads it once
  (`registerStraddles()`), wrapped by the spangap baseline (`registerSystem` /
  `registerAdvanced`). It is hand-maintained only for the baseline; the staged
  registrations flow through `straddles.gen.ts`.
- The straddle browser halves are pulled in as `file:../../<straddle>/browser`
  npm dependencies in `package.json`. That dependency list is **auto-maintained
  by spangap-inside** (`write_browser_dispatch`) each build and the packages are
  npm-linked from the staged set — a fresh checkout, or a post-`reallyclean`
  tree, builds without manual symlinks. Do not hand-edit the `file:` dependency
  block: `write_browser_dispatch` also *prunes* any `file:../../<k>/browser`
  entry whose straddle does not declare `browser_register:`, so a hand-added dep
  is silently removed on the next build.

`web-interface/src/pages/IndexPage.vue` is a placeholder landing page; the live
UI surfaces are the per-straddle panels and windows the registrations install.

**HISTORY (retired 2026-06).** The mesh family used to be hand-imported in
`modules.ts` while *not* declaring `browser_register:`, so their `file:` deps
were never in `package.json` (and would have been pruned if added). They
resolved only through orphan `node_modules` symlinks that happened to survive
incremental builds; a `reallyclean` wiped those, and the next build failed in
Vite with e.g. `failed to resolve import 'rns/modules/rnsd'`. Converting the
family to `browser_register:` (so the deps are generated and npm-linked)
retired that fragile hack; do not reintroduce hand-imports.

## 5. The factory data image

`data/` is baked by `spangap_create_factory_image(DATA_DIR …)` into the
read-only `fixed` partition (a LittleFS image):

- **`data/webroot/`** — the compiled, gzipped browser SPA (`app.js.gz`,
  `index.html.gz`, `style.css.gz`), produced by `spangap_browser_build` from
  `web-interface/` earlier in the same CMake run.
- **`data/factory_state/storage/`** — the **storage seed**: the LittleFS layout
  the device's storage module mounts on first boot, mirroring the runtime
  `storage/external/<key>.json` shape. Today it seeds `s.lxmf.json` (`{}`). This
  is the floor state shipped in the image; declarative `settings:` defaults
  (§3) are applied on top at boot via `storageDefault`.
- **`data/build_times`** — a 12-byte stamp file written by
  `tools/write_build_times.py`: three little-endian `uint32`s — the max mtime of
  any other file under `data/`, the UTC build time, and a CRC32 over the
  `webroot/` contents (path-sorted, so it is stable across rebuilds when the
  gzipped bytes don't change).

## 6. LCD icon staging

`assets/lcd-icons/*.svg` (`rns.svg`, `lxmf.svg`, `nomad.svg`, `maps.svg`,
`viewer.svg`) are the launcher icons for the on-device Dock. They are rasterized
into the image by `spangap_lcd_icons(SRC_DIR …)`, which lives in spangap-lcd's
`project_include.cmake` — so the call is guarded by `if(COMMAND
spangap_lcd_icons)` and a `--no-lcd` / screenless build skips the rasterizer
entirely (there are no LCD icons to ship without the launcher).

The parallel `web-interface/src/app-icons/*.svg` are the *browser* Dock icons,
bundled into the SPA, separate from the LCD set.

## 7. Partitions, flash size, and the OTA omission

`esp-idf/partitions.csv` is **generated by the build** — do not edit it. The
build derives the layout from flash-size, app-percentage, and whether an OTA
slot is staged. The current table is the size-agnostic floor image:

```
nvs,    data, nvs,     0x00009000, 0x00005000,
app,    app,  factory, 0x00010000, 0x00330000,
fixed,  data, spiffs,  0x00340000, 0x0006b000, readonly
```

Notes that matter when reasoning about it:

- **No `state` partition is listed.** The runtime read/write `state` partition
  is created at runtime by the filesystem layer (it grows into free flash, or
  lives on SD), not declared here. The `fixed` partition is the read-only
  factory image from §5.
- **OTA is intentionally omitted** — conceptually and otherwise. There is no
  `ota`/`updater` straddle in the staged set and **no OTA public key shipped**,
  so the build emits a single-`factory`-app, no-`otadata`, no-A/B layout.
  Because no straddle hand-owns the partition contents, the generator is free to
  shrink-wrap. The board sets flash size via kconfig; the buildable does not.
- The bootstrap sets `SPANGAP_FIXED_PARTITION` (the image target for
  `spangap_create_factory_image` / `spangap_lcd_icons`) to `fixed` when OTA is
  off, `fixed_a` when on. With OTA off it is `fixed`.
- **Where the size actually comes from.** `hw-tdeck` (a T-Deck Plus, ESP32-S3,
  16 MB flash) pins `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y` in its `straddle.yaml`
  `kconfig:` block — a board straddle is non-buildable, so its
  `sdkconfig.defaults` would be ignored; everything describing the hardware
  MUST live in `kconfig:` to survive `--with`. That flows through
  `staging/sdkconfig.spangap-fragments` and seeds a fresh
  `esp-idf/sdkconfig`. **Crucially, board fragments seed only on the *first*
  build / regen: once `esp-idf/sdkconfig` exists, `SDKCONFIG_DEFAULTS` can no
  longer override it.** So a boardless build (or one with the wrong `--with`)
  writes a 2 MB `sdkconfig` that the board fragment cannot later fix, and the
  partition generator then fails with `Partitions table occupies 8.0MB … does
  not fit in configured flash size 2MB`. The fix is to **delete
  `esp-idf/sdkconfig` and rebuild with the board** — *not* to force
  `--flash-size`, which only papers over the stale file.

## 8. System architecture (where the parts live)

The buildable composes a task-per-concern system; the durable contracts are
owned by the individual straddles and documented there. The shape:

- **`rnsd`** ([rns](https://github.com/reticulous/rns)) owns the entire RNS
  protocol state — identity, Transport, path table, Links, Resources — in one
  FreeRTOS task with **zero networking or radio dependencies**. Everything else
  is a consumer or an interface that talks to it over ITS.
- **Interfaces self-register.** Each `iface-*` straddle is an independent task
  that watches its own config, brings up its own link (LoRa radio, TCP socket,
  UDP multicast, ESP-NOW), and registers with `rnsd` over `RNSD_PORT_IFACE`.
  `rnsd` has zero compile-time knowledge of which interfaces exist — it reacts
  to whatever connects. Adding an interface is a new staged straddle, no `rns`
  change.
- **Consumers** ([lxmf](https://github.com/reticulous/lxmf),
  [nomad](https://github.com/reticulous/nomad)) sit on top of `rnsd` via its
  byte-array C API and ITS ports; they never include a `RNS::` type.
- **The platform** (spangap-core/-net/-web/-lcd) provides ITS, storage, the
  filesystem, logging, cron, the IP stack, the web server, and the LCD shell.

Straddles do not call each other directly. Cross-module wiring goes over two
decoupling buses — the storage bus (a task publishes `s.<task>.*` /
`<task>.*`, interested tasks react via `storageSubscribeChanges`) and ITS ports
— so no straddle hard-depends on another's symbols. That is what lets the
composition be additive: a straddle either reacts to config a peer published or
opens/dials an ITS port, never links against it.

Storage follows the platform convention: `s.<task>.*` for persistent settings,
`secrets.<task>.*` for secret material (wiped by factory reset), bare `<task>.*`
for ephemeral runtime/telemetry published by the owning task. Numeric-keyed
objects (`s.tcp.peers.0.host`, `s.lora.0.*`) model arrays. The exhaustive key,
port, and CLI surface for each subsystem lives in that subsystem's own README +
INTERNALS — this straddle does not redefine them.

## 9. Test harness

reticulous is exercised against a **Python companion** running Mark Qvist's
reference `Reticulum` + `LXMF` — the same upstream code the firmware
interoperates with, used two ways from one install:

- **A dev-loop `rnsd`** — one long-lived daemon with a persistent identity and
  path cache that survives firmware reflashes, so the device has a warm peer to
  dial across many flash cycles. It advertises a `TCPServerInterface` the
  firmware dials and a shared-instance socket the `rn{status,path,probe}` CLIs
  attach to. Its config dir and ports are kept isolated from any Reticulum
  activity elsewhere on the host.
- **An automated test substrate** — pytest with a **subprocess-per-peer** model.
  `RNS.Reticulum` is a process-level singleton (`RNS.Transport` carries
  module-global state), so two peers in one process cross-talk; every additional
  peer is a `subprocess.Popen`. The test process itself counts as one peer via a
  session-scoped in-process client. Peers are spawned on a fresh ephemeral
  config dir and print `READY <dest_hash_hex>` so the fixture waits on readiness
  rather than a sleep. Pinned `(priv_hex, identity_hash)` tuples give peers
  stable destination hashes across runs for cheap assertions.

Host-side gotchas that are durable Reticulum truths (not harness quirks):

- **A bare-constructed `TCPClientInterface` is broken.** Reticulum interfaces
  are not usable straight out of their constructor — `RNS.Reticulum.__init__`
  performs a long post-construction attribute setup (`announce_rate_target`,
  IFAC fields, ingress/egress caps) before `final_init`. Manually appending an
  interface to `RNS.Transport.interfaces` crashes the announce path with a
  missing `announce_rate_target`. **The interface must be in the config file
  before `RNS.Reticulum(configdir=…)`** — which is why the in-process client is
  session-scoped with a static interface to a fixed port, and the default peer
  binds that same port.
- **`LoopbackInterface` does not exist** in `RNS/Interfaces/`. Don't put
  `type = LoopbackInterface` in a config — Reticulum logs an error and ignores
  it. Intra-process loopback happens automatically via `share_instance = Yes`.

(The harness scaffolding — `tests/`, `scripts/rns`, a `Makefile`, and a
gitignored `research/` clone of upstream — is not committed in this repository
today; the facts above are the design and the host-side contracts it rests on.)

## 10. Pitfalls

- **Nothing here is the source of truth for a component's behavior.** Storage
  keys, ports, and CLI verbs belong to the owning straddle. If a value here
  conflicts with the owner, the owner wins — fix the reference, not the value.
- **`partitions.csv` and `staging/*.gen.*` are regenerated every build.** Don't
  hand-edit them; edit the inputs (`straddle.yaml`, the straddles' hooks /
  `settings:` blocks). A raw `idf.py` build with stale or missing `staging/`
  silently links no `app_main`.
- **Don't list staged straddles in `main/idf_component.yml`.** They are staged
  via `additional_installs` + `EXTRA_COMPONENT_DIRS`; double-listing them
  duplicate-stages under the manager namespace and breaks the link with
  duplicate symbols. Only *third-party managed* deps belong in that file.
- **Don't hand-edit `web-interface/package.json`'s `file:` block** or
  `boot/straddles.gen.ts` — both are build-maintained from the staged set.
- **The board owns flash size, not the buildable.** A stale `sdkconfig` can
  pin the wrong size; the board's kconfig is authoritative. Recover by deleting
  `esp-idf/sdkconfig` and rebuilding with the board, never by forcing
  `--flash-size` (§7).
- **Always build the buildable WITH a board.** The canonical invocation is
  `spangap build reticulous/reticulous --with spangap/hw-tdeck` (or
  `--with spangap/hw-heltecv4` for the screenless board); `spangap/hw-tdeck`
  additionally pulls in `spangap-lcd`, so the on-device LCD C++ (each straddle's
  `esp-idf/conditional/spangap-lcd/src/`) only compiles when a screen board is
  in the build. A bare `spangap build` replays the last explicit invocation
  stored — target first — in the workspace's `.spangap-build`
  (`/home/spangap/reticulous/.spangap-build`).
- **Never run `spangap build` from inside a sub-straddle directory.** The build
  wrapper walks up to the nearest `straddle.yaml` and **overwrites**
  `.spangap-build` with *that* straddle as the target — so a later bare
  `spangap build` builds the wrong thing. Run from the workspace root (the shim)
  or the `reticulous/` straddle dir.
- **`spangap build` is quiet** (a couple of lines) unless you pass `-v`; for the
  real exit status run `spangap build … ; echo $?` and do **not** pipe through
  `| tail`, which masks the build's exit code with `tail`'s.
