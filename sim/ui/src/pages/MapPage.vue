<template>
  <q-page class="map-page">
    <q-tabs v-model="tab" dense no-caps align="left" class="map-tabs"
            active-color="white" indicator-color="primary">
      <q-tab name="map" label="Map" />
    </q-tabs>

    <!-- The capture listener runs before the map's own, so a right-click that
         landed on the info card rather than on the canvas leaves no position
         behind: the menu still opens, but with nowhere to put a node. -->
    <div class="map-body" @contextmenu.capture="pendingPos = null">
      <SimMap ref="map" @select="onSelect" @context="onContext" />

      <div v-if="!sim.loaded" class="map-empty">
        <div class="map-empty-title">No scenario loaded</div>
        <div class="map-empty-text">
          Scenario ▸ New… makes one, Load… opens one that is already on disk.
          Right-click the map to put a station down.
        </div>
      </div>

      <div v-if="selected" class="map-card">
        <NodeCard :name="selected" @close="selected = null" @console="openConsole" />
      </div>

      <!-- Anchored to where the pointer was: the map is a canvas, so there is
           no element under the click for a menu to hang off. The canvas's own
           handler has already turned that point into a position by the time
           this opens, since the event reaches it first on the way up. -->
      <q-menu context-menu touch-position>
        <q-list dense style="min-width: 150px">
          <q-item clickable v-close-popup :disable="!sim.loaded || !pendingPos"
                  @click="askNode">
            <q-item-section>New node here</q-item-section>
          </q-item>
          <q-item clickable v-close-popup @click="map?.fitToNodes()">
            <q-item-section>Fit to nodes</q-item-section>
          </q-item>
        </q-list>
      </q-menu>
    </div>

    <ConsoleWindow
      v-for="name in consoles"
      :key="name"
      :name="name"
      :visible="true"
      @update:visible="v => !v && closeConsole(name)"
    />
  </q-page>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useQuasar } from 'quasar'
import SimMap from '../components/SimMap.vue'
import NodeCard from '../components/NodeCard.vue'
import ConsoleWindow from '../components/ConsoleWindow.vue'
import { useSim } from '../stores/sim'

const sim = useSim()
const quasar = useQuasar()
const tab = ref('map')
const map = ref<InstanceType<typeof SimMap>>()
const selected = ref<string | null>(null)
const consoles = ref<string[]>([])

/** Where the last right-click landed on the map, in degrees — what "here"
 *  means in "New node here". Null when the click was on something else, which
 *  is why it has to be a ref: the menu item's disabled state reads it. */
const pendingPos = ref<[number, number] | null>(null)

function onSelect(name: string) {
  selected.value = selected.value === name ? null : name
}

function onContext(pos: [number, number]) {
  pendingPos.value = pos
}

function askNode() {
  quasar.dialog({
    title: 'New node',
    message: 'A name: lower-case letters, digits and hyphens. It is the '
           + "station's hostname and the label on the map.",
    prompt: { model: '', type: 'text' },
    cancel: true,
  }).onOk((name: string) => {
    const pos = pendingPos.value
    if (name.trim() && pos) sim.addNode(name.trim(), pos)
  })
}

function openConsole(name: string) {
  if (!consoles.value.includes(name)) consoles.value.push(name)
}

function closeConsole(name: string) {
  consoles.value = consoles.value.filter(n => n !== name)
}
</script>

<style scoped>
.map-page { display: flex; flex-direction: column; height: 100%; }
.map-tabs { background: #171b21; border-bottom: 1px solid #262c35; flex: none; min-height: 30px; }
.map-body { position: relative; flex: 1 1 auto; min-height: 0; }
.map-card { position: absolute; top: 12px; right: 12px; }
.map-empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  pointer-events: none;
  text-align: center;
}
.map-empty-title { font-size: 15px; color: #9ca3af; }
.map-empty-text { font-size: 12px; color: #6b7280; max-width: 360px; line-height: 1.5; }
</style>
