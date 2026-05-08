<template>
  <div class="index-page">
    <div class="index-video-wrap">
      <RtcPlayer v-if="playing" class="full-width full-height" />
      <div v-else-if="showBusy" class="busy-wrap flex flex-center column">
        <div class="busy-label">BUSY</div>
        <div class="busy-hint">Another session is streaming</div>
        <q-btn flat dense color="white" label="Take over" class="q-mt-md" @click="takeOver" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import RtcPlayer from '../components/RtcPlayer.vue'
import { useDeviceStore } from '../stores/device'

const device = useDeviceStore()
const myId = 'c' + String(Math.random()).slice(2, 10)

const playing = ref(false)
const showBusy = ref(false)
let heartbeat: ReturnType<typeof setInterval> | null = null
let busyCheck: ReturnType<typeof setInterval> | null = null

const STALE_S = 5

function epoch(): number { return Math.floor(Date.now() / 1000) }
function isMine(): boolean { return String(device.get('stream.owner')) === myId }

function otherIsStreaming(): boolean {
  if (isMine()) return false
  const v = Number(device.get('stream.busy') || 0)
  return v > 0 && (epoch() - v) < STALE_S
}

function claim() {
  stopAll()
  device.set('stream.owner', myId)
  device.set('stream.busy', epoch())
  playing.value = true
  showBusy.value = false
  heartbeat = setInterval(() => {
    device.set('stream.busy', epoch())
    if (!isMine()) enterBusy()
  }, 2000)
}

function enterBusy() {
  stopAll()
  playing.value = false
  showBusy.value = true
  busyCheck = setInterval(() => {
    if (!otherIsStreaming()) claim()
  }, 1000)
}

function tryConnect() {
  if (!device.settings.s) return
  if (otherIsStreaming()) enterBusy()
  else claim()
}

function takeOver() { claim() }

function stopAll() {
  if (heartbeat) { clearInterval(heartbeat); heartbeat = null }
  if (busyCheck) { clearInterval(busyCheck); busyCheck = null }
}

/* Storage DC connected + config loaded → try connect. Disconnect → reset. */
watch(
  [() => device.connected, () => device.settings?.s],
  ([conn, s]) => {
    if (!conn) { stopAll(); playing.value = false; showBusy.value = false; return }
    if (s && !playing.value && !showBusy.value) tryConnect()
  },
  { immediate: true },
)

onUnmounted(() => {
  stopAll()
  if (playing.value) { device.set('stream.busy', 0); device.set('stream.owner', '') }
})

window.addEventListener('beforeunload', () => {
  if (playing.value) { device.set('stream.busy', 0); device.set('stream.owner', '') }
})
</script>

<style scoped>
.index-page {
  min-height: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.index-video-wrap {
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  position: relative;
  overflow: hidden;
}
.busy-wrap {
  width: 100%;
  height: 100%;
  background: #121212;
}
.busy-label {
  font-size: 2.5rem;
  font-weight: 700;
  color: rgba(255, 80, 80, 0.85);
  letter-spacing: 0.15em;
}
.busy-hint {
  color: rgba(255, 255, 255, 0.5);
  font-size: 0.9rem;
  margin-top: 4px;
}
</style>
