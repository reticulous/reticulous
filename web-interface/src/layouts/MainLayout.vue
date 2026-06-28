<template>
  <q-layout v-if="authChecked" view="hHh Lpr fFf" class="main-layout">
    <q-header class="bg-dark text-white no-shadow app-header">
      <q-toolbar class="topbar" style="min-height: 38px">
        <q-toolbar-title class="topbar-title">{{ progName }}</q-toolbar-title>
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
        <NodesWindow
          :visible="nodesVisible"
          title="Reticulum Nodes"
          @update:visible="v => nodesVisible = v"
        />
        <MapWindow
          :visible="mapVisible"
          title="Reticulum Map"
          @update:visible="v => mapVisible = v"
        />
        <MessagesWindow
          v-for="w in lxmfWindows"
          :key="w.n"
          :identity="w.n"
          :visible="messagesVisibleById[w.n] ?? false"
          :focus-token="messagesFocusById[w.n] ?? 0"
          :title="w.displayName ? `LXMF Messages - ${w.displayName}` : 'LXMF Messages'"
          @update:visible="v => (messagesVisibleById[w.n] = v)"
        />
        <NomadWindow
          :visible="nomadVisible"
          :focus-token="nomadFocus"
          title="Nomad Browser"
          @update:visible="v => nomadVisible = v"
        />
        <ViewerWindow
          :visible="viewerWebVisible"
          :focus-token="viewerWebFocus"
          title="Viewer"
          @update:visible="v => viewerWebVisible = v"
        />
        <SettingsWindow
          :visible="settingsVisible"
          :focus-token="settingsFocus"
          @update:visible="v => settingsVisible = v"
        />
        <Dock />
      </UsableArea>
    </q-page-container>
    <ConnectionOverlay />
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
import MapWindow from 'rns/panels/MapWindow.vue'
import NodesWindow from 'rns/panels/NodesWindow.vue'
import MessagesWindow from 'lxmf/panels/MessagesWindow.vue'
import NomadWindow from 'nomad/panels/NomadWindow.vue'
import ViewerWindow from 'viewer/panels/ViewerWindow.vue'
import { cliVisible, logVisible, cliFocus, logFocus } from 'spangap-browser/modules/advanced'
import { mapVisible, nodesVisible } from 'rns/modules/rnsd'
import { messagesVisibleById, messagesFocusById,
         useLxmf, FALLBACK_ID } from 'lxmf/modules/lxmf'
import { nomadVisible, nomadFocus } from 'nomad/modules/nomad'
import { viewerWebVisible, viewerWebFocus } from 'viewer/modules/viewer'
import { startLogStream, installConsoleHooks } from 'spangap-browser/stores/log'

const router = useRouter()
const device = useDeviceStore()
const authChecked = ref(false)
const authActive = ref(false)

async function onLogout() {
  try { await authLogout() } catch { /* ignore */ }
  window.location.reload()   /* drop all in-memory session/UI state, re-run auth */
}


/* One Messages window per usable LXMF identity; a single FALLBACK_ID window
 * when there are none (so the "create an identity" guidance stays reachable). */
const lxmf = useLxmf()
const lxmfWindows = computed(() => {
  const u = lxmf.usableIdentities.value
  return u.length
    ? u.map(i => ({ n: i.n, displayName: i.displayName }))
    : [{ n: FALLBACK_ID, displayName: '' }]
})

/* The actual browser window/tab title (document.title): "<program> -
 * <hostname>", collapsed to just the program name when the two are
 * case-insensitively equal (e.g. Reticulous / reticulous). Mirrors MenuBar's
 * progName fallback chain. */
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
watchEffect(() => {
  const prog = progName.value, host = hostName.value
  document.title = host && host.toLowerCase() !== prog.toLowerCase()
    ? `${prog} - ${host}` : prog
})

const session = getSession()
const sessionState = ref<SessionState>(session.state)
let sessionUnsub: (() => void) | null = null

onMounted(async () => {
  try {
    const unset = await isAdminUnset()
    if (unset) { router.replace('/setup'); return }
    const auth = await checkAuth()
    if (auth.enabled && !auth.realm) { router.replace('/login'); return }
    authActive.value = auth.enabled
  } catch { /* proceed */ }
  authChecked.value = true
  sessionUnsub = session.onStateChange((s) => { sessionState.value = s })
  device.connect()
  startLogStream()
  installConsoleHooks()
})

onUnmounted(() => {
  if (sessionUnsub) { sessionUnsub(); sessionUnsub = null }
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
</style>
