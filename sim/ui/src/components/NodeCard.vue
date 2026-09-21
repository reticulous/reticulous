<template>
  <q-card v-if="node" class="node-card" flat bordered>
    <div class="node-card-head">
      <span class="node-card-name">{{ node.name }}</span>
      <span class="node-card-id">#{{ node.id }}</span>
      <q-space />
      <q-btn flat dense round size="sm" aria-label="Close" @click="$emit('close')">
        <span class="node-card-x">×</span>
      </q-btn>
    </div>

    <q-separator />

    <div class="node-card-rows">
      <div><span>status</span><b :class="'st-' + node.status">{{ node.status }}</b></div>
      <div><span>position</span><b>{{ node.pos[0].toFixed(5) }}, {{ node.pos[1].toFixed(5) }}</b></div>
      <div><span>transport</span><b>{{ node.transport === null ? '—' : node.transport ? 'on' : 'off' }}</b></div>
      <div><span>radio</span><b>{{ radio }}</b></div>
      <div v-if="heard.length"><span>hears</span><b class="node-card-heard">
        <span v-for="[name, level] in heard" :key="name">{{ name }} {{ level.toFixed(0) }}&thinsp;dBm</span>
      </b></div>
    </div>

    <q-separator />

    <div class="node-card-actions">
      <q-btn flat dense no-caps size="sm" label="Web UI" @click="openStation" />
      <q-btn flat dense no-caps size="sm" label="Console" @click="$emit('console', node.name)" />
      <q-btn flat dense no-caps size="sm" label="Reset" @click="sim.resetNode(node.name)">
        <q-tooltip>Presses reset — the process restarts, its state is untouched</q-tooltip>
      </q-btn>
      <q-btn flat dense no-caps size="sm" label="Factory reset" @click="confirmFactoryReset">
        <q-tooltip>Wipes its state and starts it again from the setup lines</q-tooltip>
      </q-btn>
      <q-btn flat dense no-caps size="sm" label="Setup" @click="editing = true" />
      <q-btn flat dense no-caps size="sm" color="negative" label="Remove" @click="confirmRemove" />
    </div>

    <q-dialog v-model="editing">
      <q-card style="min-width: 420px">
        <q-card-section class="text-subtitle2">
          Setup lines for {{ node.name }}
        </q-card-section>
        <q-card-section>
          <div class="text-caption text-grey-6 q-mb-sm">
            Sent after the scenario's own lines, on a first boot and on Apply setup.
          </div>
          <q-input v-model="draft" type="textarea" outlined dense autogrow
                   input-style="font-family: ui-monospace, monospace" />
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" v-close-popup />
          <q-btn flat no-caps label="Save" color="primary" @click="saveSetup" />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </q-card>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useSim } from '../stores/sim'

const props = defineProps<{ name: string | null }>()
defineEmits<{ close: []; console: [name: string] }>()

const sim = useSim()
const quasar = useQuasar()
const editing = ref(false)
const draft = ref('')

const node = computed(() => (props.name ? sim.nodes[props.name] ?? null : null))

const radio = computed(() => {
  const n = node.value
  if (!n || !n.freq) return '—'
  return `${(n.freq / 1e6).toFixed(3)} MHz  SF${n.sf}  ${(n.bw ?? 0) / 1000} kHz  ${n.mode ?? ''}`
})

const heard = computed<[string, number][]>(() => {
  const levels = props.name ? sim.levels[props.name] : undefined
  return levels ? Object.entries(levels).sort((a, b) => b[1] - a[1]) : []
})

watch(() => props.name, (name) => {
  if (name) sim.askLevels(name)
  draft.value = (sim.nodes[name ?? '']?.setup ?? []).join('\n')
}, { immediate: true })

function openStation() {
  if (node.value) window.open(sim.stationUrl(node.value.name), '_blank')
}

function saveSetup() {
  if (!node.value) return
  sim.setNodeSetup(node.value.name, draft.value.split('\n').filter(l => l.trim()))
  editing.value = false
}

function confirmFactoryReset() {
  const name = node.value?.name
  if (!name) return
  quasar.dialog({
    title: 'Factory reset',
    message: `Wipe ${name}'s state and start it again from the setup lines. `
           + 'Its identity, keys, paths and message history go.',
    cancel: true,
    persistent: true,
  }).onOk(() => sim.factoryResetNode(name))
}

function confirmRemove() {
  const name = node.value?.name
  if (!name) return
  quasar.dialog({
    title: 'Remove node',
    message: `Stop ${name} and delete its state? This cannot be undone.`,
    cancel: true,
    persistent: true,
  }).onOk(() => sim.removeNode(name))
}
</script>

<style scoped>
.node-card {
  width: 330px;
  background: #1b1f26;
  border-color: #2b313b;
  /* Labels, not prose: a click on the card is aimed at a button, and a
     half-selected reading is only ever in the way. */
  user-select: none;
  -webkit-user-select: none;
}
.node-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 6px 6px 12px;
}
.node-card-name { font-weight: 600; }
.node-card-id { color: #6b7280; font: 11px ui-monospace, monospace; }
.node-card-x { font-size: 18px; line-height: 1; color: #9ca3af; }
.node-card-rows { padding: 8px 12px; font-size: 12px; }
.node-card-rows > div { display: flex; gap: 10px; padding: 2px 0; }
.node-card-rows span { color: #6b7280; width: 74px; flex: none; }
.node-card-rows b { font-weight: 500; font-family: ui-monospace, monospace; }
.node-card-heard { display: flex; flex-direction: column; min-width: 0; }
.node-card-heard span { white-space: nowrap; }
.node-card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  padding: 4px 6px;
}
.st-up { color: #e5e7eb; }
.st-starting, .st-setup { color: #f59e0b; }
.st-stopped { color: #6b7280; }
.st-restarting { color: #ef4444; }
</style>
