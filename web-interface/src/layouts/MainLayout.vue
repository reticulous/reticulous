<template>
  <q-layout v-if="authChecked" view="hHh Lpr fFf" class="main-layout">
    <q-header class="bg-dark text-white no-shadow app-header">
      <MenuBar />
    </q-header>

    <!-- Settings pane. On a phone it sweeps in full-screen (overlay, full
         width); on desktop it's a side rail that pushes the content. Both
         still slide in from the left. -->
    <q-drawer
      :model-value="panelOpen"
      side="left"
      :width="drawerWidth"
      :behavior="compact ? 'mobile' : 'desktop'"
      :overlay="compact"
      bordered
      class="settings-drawer bg-grey-10 text-white"
      :breakpoint="0"
      @update:model-value="onDrawerToggle"
    >
      <SettingsPanel />
    </q-drawer>

    <q-page-container class="main-page-container">
      <UsableArea>
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
        <AnnouncesWindow
          :visible="announcesVisible"
          title="Announces"
          @update:visible="v => announcesVisible = v"
        />
        <NomadWindow
          :visible="nomadVisible"
          :focus-token="nomadFocus"
          title="Nomad Browser"
          @update:visible="v => nomadVisible = v"
        />
      </UsableArea>
    </q-page-container>
  </q-layout>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, onUnmounted, watchEffect } from 'vue'
import { useQuasar } from 'quasar'
import { useRouter } from 'vue-router'
import { useMenuStore } from 'spangap-browser/stores/menu'
import { useDeviceStore } from 'spangap-browser/stores/device'
import { useCompact } from 'spangap-browser/lib/viewport'
import { checkAuth, isAdminUnset } from 'spangap-browser/lib/auth'
import { getSession, type SessionState } from 'spangap-browser/lib/webrtc-session'
import MenuBar from 'spangap-browser/components/MenuBar.vue'
import SettingsPanel from 'spangap-browser/components/SettingsPanel.vue'
import TerminalWindow from 'spangap-browser/components/TerminalWindow.vue'
import LogWindow from 'spangap-browser/components/LogWindow.vue'
import UsableArea from 'spangap-browser/components/UsableArea.vue'
import MapWindow from 'rns/panels/MapWindow.vue'
import NodesWindow from 'rns/panels/NodesWindow.vue'
import AnnouncesWindow from 'lxmf/panels/AnnouncesWindow.vue'
import MessagesWindow from 'lxmf/panels/MessagesWindow.vue'
import NomadWindow from 'nomad/panels/NomadWindow.vue'
import { cliVisible, logVisible, cliFocus, logFocus } from 'spangap-browser/modules/advanced'
import { mapVisible, nodesVisible } from 'rns/modules/rnsd'
import { messagesVisibleById, messagesFocusById, announcesVisible,
         useLxmf, FALLBACK_ID } from 'lxmf/modules/lxmf'
import { nomadVisible, nomadFocus } from 'nomad/modules/nomad'
import { startLogStream, installConsoleHooks } from 'spangap-browser/stores/log'

const $q = useQuasar()
const router = useRouter()
const menuStore = useMenuStore()
const device = useDeviceStore()
const compact = useCompact()
const authChecked = ref(false)

const panelOpen = computed(() => menuStore.activePanel !== null)

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

const drawerWidth = computed(() =>
  compact.value
    ? $q.screen.width
    : Math.min(420, Math.max(280, Math.floor($q.screen.width * 0.38))),
)

function onDrawerToggle(val: boolean) {
  if (!val) menuStore.closePanel()
}

/* Dismiss the settings pane on any click outside it, while letting the click
 * fall through to whatever it hit (no scrim, no preventDefault — it cascades).
 * The menu bar and its dropdowns are exempt so they keep driving pane state
 * (switching panes from the menu must not slam the new pane shut). */
function onDocClick(e: MouseEvent) {
  if (!panelOpen.value) return
  const t = e.target as HTMLElement | null
  if (!t) return
  // Teleported overlays (dialogs, menus, tooltips) render outside the layout
  // root — e.g. a settings pane's "Add Peer" q-dialog lives under <body>. Only
  // a click *inside* the layout, but outside the pane and the menu bar, should
  // dismiss the pane. (The menu bar is exempt so menu-driven pane switching
  // isn't slammed shut.)
  if (!t.closest('.q-layout')) return
  if (t.closest('.settings-drawer') || t.closest('.menu-bar')) return
  // Capture phase + Vue's deferred DOM flush means the drawer collapse happens
  // after this click resolves on its real target — so the click still lands.
  menuStore.closePanel()
}

const session = getSession()
const sessionState = ref<SessionState>(session.state)
let sessionUnsub: (() => void) | null = null

onMounted(async () => {
  try {
    const unset = await isAdminUnset()
    if (unset) { router.replace('/setup'); return }
    const auth = await checkAuth()
    if (auth.enabled && !auth.realm) { router.replace('/login'); return }
  } catch { /* proceed */ }
  authChecked.value = true
  sessionUnsub = session.onStateChange((s) => { sessionState.value = s })
  device.connect()
  startLogStream()
  installConsoleHooks()
  document.addEventListener('click', onDocClick, true)
})

onUnmounted(() => {
  if (sessionUnsub) { sessionUnsub(); sessionUnsub = null }
  document.removeEventListener('click', onDocClick, true)
})
</script>

<style scoped>
.main-layout {
  height: 100vh;
  overflow: hidden;
}
.main-page-container {
  height: 100%;
  overflow: hidden;
  display: flex;
  flex-direction: column;
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
