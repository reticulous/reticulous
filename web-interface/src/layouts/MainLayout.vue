<template>
  <q-layout v-if="authChecked" view="hHh Lpr fFf" class="main-layout">
    <q-header class="bg-dark text-white no-shadow app-header">
      <q-toolbar class="topbar" style="min-height: 38px">
        <q-toolbar-title class="topbar-title">{{ displayTitle }}</q-toolbar-title>
        <!-- Straddle-owned status indicators self-mount here (left of the power
             button) through the top-bar icon registry — a staged straddle
             registers its icon from its register* module, so this layout never
             imports straddle packages and never needs editing as the set changes. -->
        <TopbarIcons />
        <!-- Power button → Log out. Inline SVG (the SPA uses Quasar's
             svg-material-icons set, so string icon names don't render as a
             webfont). Shown only when auth is active. -->
        <q-btn v-if="authActive" flat dense round aria-label="Log out" @click="onLogout">
          <svg class="power-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 3.5 V11.5" />
            <path d="M7.3 6.5 a7 7 0 1 0 9.4 0" />
          </svg>
          <q-tooltip>Log Out</q-tooltip>
        </q-btn>
      </q-toolbar>
    </q-header>

    <q-page-container class="main-page-container">
      <!-- No click-outside scrim: the settings pane now lives inside the
           Settings app window, which owns its own close/back chrome. -->
      <UsableArea :dismiss-overlay="false">
        <router-view />
        <TerminalWindow
          :visible="cliVisible"
          :focus-token="cliFocus"
          title="CLI"
          dc-label="cli:1"
          config-prefix="cli"
          @update:visible="v => cliVisible = v"
        />
        <LogWindow
          :visible="logVisible"
          :focus-token="logFocus"
          title="System Log"
          @update:visible="v => logVisible = v"
        />
        <!-- Every straddle-owned window (rns, lxmf, nomad, viewer, lcdmirror,
             …) self-mounts through the window-mount registry — a staged
             straddle registers its window component from its register* module
             in straddles.gen.ts, so this layout never imports straddle
             packages and never needs editing when the staged set changes.
             Only spangap-web's own framework windows are mounted here. -->
        <StraddleWindows />
        <SettingsWindow
          :visible="settingsVisible"
          :focus-token="settingsFocus"
          @update:visible="v => settingsVisible = v"
        />
        <Dock />
      </UsableArea>
    </q-page-container>
    <ConnectionOverlay />
    <!-- Single-session blocker: shown when the device reports another
         session holds the slot (BUSY) or when this session was evicted by a
         takeover (kicked). Teleported to <body> so it covers the whole
         viewport, header included. -->
    <Teleport to="body">
      <div v-if="sessionBlocked" class="session-blocker" role="alertdialog">
        <div class="session-blocker-box">
          <div class="session-blocker-title">{{ blockerTitle }}</div>
          <div class="session-blocker-body">{{ blockerBody }}</div>
          <button class="session-blocker-btn" @click="onSessionAction">{{ blockerAction }}</button>
        </div>
      </div>
    </Teleport>
  </q-layout>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, onUnmounted, watchEffect } from 'vue'
import { useRouter } from 'vue-router'
import { useDeviceStore } from 'spangap-browser/stores/device'
import { checkAuth, isAdminUnset, authLogout } from 'spangap-browser/lib/auth'
import { getSession, type SessionState } from 'spangap-browser/lib/webrtc-session'
import TerminalWindow from 'spangap-browser/components/TerminalWindow.vue'
import LogWindow from 'spangap-browser/components/LogWindow.vue'
import UsableArea from 'spangap-browser/components/UsableArea.vue'
import Dock from 'spangap-browser/components/Dock.vue'
import SettingsWindow from 'spangap-browser/components/SettingsWindow.vue'
import ConnectionOverlay from 'spangap-browser/components/ConnectionOverlay.vue'
import { settingsVisible, settingsFocus } from 'spangap-browser/modules/advanced'
import StraddleWindows from 'spangap-browser/components/StraddleWindows.vue'
import TopbarIcons from 'spangap-browser/components/TopbarIcons.vue'
import { cliVisible, logVisible, cliFocus, logFocus } from 'spangap-browser/modules/advanced'
import { startLogStream, installConsoleHooks } from 'spangap-browser/stores/log'

const router = useRouter()
const device = useDeviceStore()
const authChecked = ref(false)
const authActive = ref(false)

async function onLogout() {
  try { await authLogout() } catch { /* ignore */ }
  window.location.reload()   /* drop all in-memory session/UI state, re-run auth */
}


/* progName mirrors MenuBar's fallback chain; hostName is the device's configured
 * hostname. The host segment is dropped when it is empty or case-insensitively
 * equal to the program name (e.g. reticulous / Reticulous). */
const progName = computed(() => {
  const p = device.get('s.sys.progname')
  if (typeof p === 'string' && p.trim()) return p.trim()
  const proj = device.get('s.sys.project')
  if (typeof proj === 'string' && proj) return proj.charAt(0).toUpperCase() + proj.slice(1)
  return 'Spangap'
})
const hostName = computed(() => {
  const h = device.get('s.net.hostname')
  return typeof h === 'string' ? h.trim() : ''
})
const showHost = computed(() => {
  const host = hostName.value
  return !!host && host.toLowerCase() !== progName.value.toLowerCase()
})
/* Top-bar header: "<hostname> - <program>". */
const displayTitle = computed(() =>
  showHost.value ? `${hostName.value} - ${progName.value}` : progName.value)
/* Browser tab: "<hostname> - Web UI - <program>", naming the role between them. */
const tabTitle = computed(() =>
  showHost.value ? `${hostName.value} - Web UI - ${progName.value}` : `Web UI - ${progName.value}`)
watchEffect(() => { document.title = tabTitle.value })

/* ── Single-session state (BUSY / kicked) ── */
const session = getSession()
const sessionState = ref<SessionState>(session.state)
let sessionUnsub: (() => void) | null = null

/* A reconnect (page reload, dropped link) can briefly land on BUSY: the
 * device hasn't yet noticed our previous signalling WS closed and rejects
 * the new connect as a second session. That clears itself within a beat as
 * the stale slot is reclaimed. Only surface the blocker if we're STILL busy
 * after this settle window, so a transient reject never flashes the overlay.
 * A takeover ('kicked') is deliberate — no race — so it shows immediately. */
const BUSY_SETTLE_MS = 1200
const busyShown = ref(false)
let busyTimer: ReturnType<typeof setTimeout> | null = null

function clearBusyTimer() {
  if (busyTimer) { clearTimeout(busyTimer); busyTimer = null }
}

function onSessionState(s: SessionState) {
  sessionState.value = s
  if (s === 'busy') {
    if (!busyShown.value && !busyTimer) {
      busyTimer = setTimeout(() => { busyTimer = null; busyShown.value = true }, BUSY_SETTLE_MS)
    }
  } else {
    clearBusyTimer()
    busyShown.value = false
  }
}

const sessionBlocked = computed(() => busyShown.value || sessionState.value === 'kicked')
const blockerTitle = computed(() =>
  sessionState.value === 'busy' ? 'Device busy' : 'Session ended')
const blockerBody = computed(() => sessionState.value === 'busy'
  ? 'Another session is active. Taking over will end the other session.'
  : 'Another session took over this device.')
const blockerAction = computed(() =>
  sessionState.value === 'busy' ? 'Take over' : 'Resume')

function onSessionAction() {
  /* Busy: force-evict the other session. Kicked: retry without force
     (may land back on BUSY if the new occupant is still there). */
  const force = sessionState.value === 'busy'
  session.connect({ force })
}

onMounted(async () => {
  try {
    const unset = await isAdminUnset()
    if (unset) { router.replace('/setup'); return }
    const auth = await checkAuth()
    if (auth.enabled && !auth.realm) { router.replace('/login'); return }
    authActive.value = auth.enabled
  } catch { /* proceed */ }
  authChecked.value = true
  sessionUnsub = session.onStateChange(onSessionState)
  device.connect()
  startLogStream()
  installConsoleHooks()
})

onUnmounted(() => {
  if (sessionUnsub) { sessionUnsub(); sessionUnsub = null }
  clearBusyTimer()
})
</script>

<style scoped>
.main-layout {
  height: 100vh;       /* fallback for browsers without dvh */
  height: 100dvh;      /* dynamic viewport: excludes the mobile URL/toolbar chrome,
                          so the bottom Dock isn't pushed below the visible fold */
  overflow: hidden;
}
.main-page-container {
  height: 100%;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.power-icon {
  width: 20px;
  height: 20px;
  display: block;
}
.app-header {
  box-shadow: none !important;
  border-bottom: 2px solid rgba(255, 255, 255, 0.12);
  background-image: linear-gradient(
    180deg,
    rgba(255, 255, 255, 0.055) 0%,
    rgba(255, 255, 255, 0) 42%
  );
}
.session-blocker {
  position: fixed;
  inset: 0;
  /* Above ConnectionOverlay (100000) — the actionable dialog wins when both
     a link-down scrim and a takeover/busy state are in play. */
  z-index: 100001;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.session-blocker-box {
  background: #1a1a1a;
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 8px;
  padding: 24px 28px;
  max-width: 420px;
  width: 90%;
  text-align: center;
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.5);
  color: #fff;
}
.session-blocker-title {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 12px;
}
.session-blocker-body {
  font-size: 14px;
  line-height: 1.4;
  opacity: 0.85;
  margin-bottom: 20px;
}
.session-blocker-btn {
  background: #2a6fc4;
  color: white;
  border: none;
  border-radius: 6px;
  padding: 10px 22px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}
.session-blocker-btn:hover { background: #3b82d9; }
</style>
