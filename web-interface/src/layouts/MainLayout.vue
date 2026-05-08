<template>
  <q-layout v-if="authChecked" view="hHh Lpr fFf" class="main-layout">
    <q-header class="bg-dark text-white no-shadow app-header">
      <MenuBar />
    </q-header>

    <q-drawer
      v-if="!stackPanel"
      :model-value="panelOpen"
      side="left"
      :width="drawerWidth"
      behavior="desktop"
      :overlay="false"
      bordered
      class="settings-drawer bg-grey-10 text-white"
      :breakpoint="0"
      @update:model-value="onDrawerToggle"
    >
      <SettingsPanel />
    </q-drawer>

    <q-page-container class="main-page-container">
      <div class="usable-area" :class="{ 'usable-area--stacked': stackPanel && panelOpen }">
        <div class="video-area" :style="videoStyle">
          <router-view />
        </div>
        <div
          v-if="stackPanel && panelOpen"
          class="stacked-panel bg-grey-10 text-white"
        >
          <SettingsPanel />
        </div>
        <div v-if="sessionBlocked" class="session-blocker">
          <div class="session-blocker-box">
            <div class="session-blocker-title">{{ blockerTitle }}</div>
            <div class="session-blocker-body">{{ blockerBody }}</div>
            <button class="session-blocker-btn" @click="onSessionAction">{{ blockerAction }}</button>
          </div>
        </div>
        <div v-if="!stackPanel && panelOpen && menuStore.activePanel !== 'recordings'" class="panel-dismiss-overlay" @click="menuStore.closePanel()" />
        <TerminalWindow
          :visible="cliVisible"
          title="CLI"
          dc-label="cli:1"
          config-prefix="cli"
          @update:visible="v => cliVisible = v"
        />
        <LogWindow
          :visible="logVisible"
          title="System Log"
          @update:visible="v => logVisible = v"
        />
        <EditorWindow
          v-for="ed in editors" :key="ed.id"
          :id="ed.id"
          :path="ed.path"
          :title="ed.title"
          :visible="ed.visible"
          @update:visible="v => { if (!v) closeEditor(ed.id) }"
        />
      </div>
    </q-page-container>
  </q-layout>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, onUnmounted } from 'vue'
import { useQuasar } from 'quasar'
import { useRouter } from 'vue-router'
import { useMenuStore } from '../stores/menu'
import { useDeviceStore } from '../stores/device'
import { checkAuth, isAdminUnset } from '../lib/auth'
import { getSession, type SessionState } from '../lib/webrtc-session'
import MenuBar from '../components/MenuBar.vue'
import SettingsPanel from '../components/SettingsPanel.vue'
import TerminalWindow from '../components/TerminalWindow.vue'
import LogWindow from '../components/LogWindow.vue'
import EditorWindow from '../components/EditorWindow.vue'
import { cliVisible, logVisible, videoStyle } from '../modules/advanced'
import { editors, closeEditor } from '../modules/editor'
import { startLogStream, installConsoleHooks } from '../stores/log'

const $q = useQuasar()
const router = useRouter()
const menuStore = useMenuStore()
const device = useDeviceStore()
const authChecked = ref(false)

const panelOpen = computed(() => menuStore.activePanel !== null)

const drawerWidth = computed(() =>
  Math.min(420, Math.max(280, Math.floor($q.screen.width * 0.38))),
)

/* Stack the settings panel below the video on small portrait screens
 * (typical phone). Otherwise use the side drawer. */
const stackPanel = computed(() =>
  $q.screen.lt.sm && $q.screen.height > $q.screen.width,
)

function onDrawerToggle(val: boolean) {
  if (!val) menuStore.closePanel()
}

/* ── Session state (BUSY / kicked) ── */
const session = getSession()
const sessionState = ref<SessionState>(session.state)
let sessionUnsub: (() => void) | null = null

const sessionBlocked = computed(() =>
  sessionState.value === 'busy' || sessionState.value === 'kicked')
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
  height: 100vh;
  overflow: hidden;
}
.main-page-container {
  height: 100%;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.usable-area {
  flex: 1;
  min-height: 0;
  position: relative;
  overflow: hidden;
}
.usable-area--stacked {
  display: flex;
  flex-direction: column;
}
.usable-area--stacked .video-area {
  /* Override videoStyle's absolute/100% inline styles. */
  position: relative !important;
  top: auto !important;
  left: auto !important;
  flex: 0 0 50%;
  width: 100% !important;
  height: 50% !important;
}
.usable-area--stacked .stacked-panel {
  flex: 1 1 50%;
  min-height: 0;
  overflow: hidden;
  border-top: 1px solid rgba(255, 255, 255, 0.12);
}
.video-area {
  overflow: hidden;
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
.panel-dismiss-overlay {
  position: absolute;
  inset: 0;
  z-index: 100;
  cursor: default;
}
.session-blocker {
  position: absolute;
  inset: 0;
  z-index: 10000;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
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
