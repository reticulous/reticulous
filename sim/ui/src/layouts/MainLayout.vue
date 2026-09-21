<template>
  <q-layout view="hHh lpR fFf" class="sim-layout">
    <q-header class="sim-header">
      <q-toolbar class="sim-toolbar">
        <!-- A scenario is the design: the map and the setup lines, no state.
             Loading one is a factory-fresh network. -->
        <q-btn flat dense no-caps label="Scenario" class="sim-menu-btn">
          <q-menu auto-close>
            <q-list dense style="min-width: 180px">
              <q-item clickable @click="askNew"><q-item-section>New…</q-item-section></q-item>
              <q-item clickable :disable="!sim.scenarios.length" @click="loading = true">
                <q-item-section>Load…</q-item-section>
              </q-item>
              <q-separator />
              <q-item clickable :disable="!sim.loaded" @click="sim.saveScenario()">
                <q-item-section>Save</q-item-section>
              </q-item>
              <q-item clickable :disable="!sim.loaded" @click="askSaveAs">
                <q-item-section>Save As…</q-item-section>
              </q-item>
              <q-separator />
              <q-item clickable :disable="!sim.loaded" @click="openSettings">
                <q-item-section>Settings…</q-item-section>
              </q-item>
            </q-list>
          </q-menu>
        </q-btn>

        <!-- A snapshot is the design plus everything that has happened to it:
             identities, keys, paths, message history. -->
        <q-btn flat dense no-caps label="Snapshot" class="sim-menu-btn">
          <q-menu auto-close>
            <q-list dense style="min-width: 180px">
              <q-item clickable :disable="!sim.snapshots.length" @click="restoring = true">
                <q-item-section>Load…</q-item-section>
              </q-item>
              <q-item clickable :disable="!sim.loaded" @click="askSnapshot">
                <q-item-section>Save As…</q-item-section>
              </q-item>
            </q-list>
          </q-menu>
        </q-btn>

        <q-btn flat dense no-caps label="Simulation" class="sim-menu-btn">
          <q-menu auto-close>
            <q-list dense style="min-width: 200px">
              <q-item clickable :disable="!sim.loaded" @click="sim.startAll()">
                <q-item-section>Start all</q-item-section>
              </q-item>
              <q-item clickable :disable="!sim.loaded" @click="sim.stopAll()">
                <q-item-section>Stop all</q-item-section>
              </q-item>
              <q-separator />
              <q-item clickable :disable="!sim.loaded" @click="sim.resetAll()">
                <q-item-section>Reset all</q-item-section>
                <q-item-section side><span class="sim-hint">restart</span></q-item-section>
              </q-item>
              <q-item clickable :disable="!sim.loaded" @click="askFactoryResetAll">
                <q-item-section>Factory reset all</q-item-section>
                <q-item-section side><span class="sim-hint">wipe state</span></q-item-section>
              </q-item>
              <q-separator />
              <q-item clickable :disable="!sim.loaded" @click="commanding = true">
                <q-item-section>Run command…</q-item-section>
              </q-item>
            </q-list>
          </q-menu>
        </q-btn>

        <q-toolbar-title class="sim-title">
          <span v-if="sim.scenario">{{ sim.scenario.name }}<span v-if="sim.dirty" class="sim-dirty"> •</span></span>
          <span v-else class="sim-empty">no scenario</span>
        </q-toolbar-title>

        <span class="sim-count">{{ sim.running }}/{{ sim.nodeList.length }} up</span>
        <span class="sim-link" :class="{ 'sim-link-off': !sim.connected }">
          {{ sim.connected ? 'connected' : 'reconnecting…' }}
        </span>
      </q-toolbar>
    </q-header>

    <q-page-container class="sim-page-container">
      <router-view />
    </q-page-container>

    <!-- Load a scenario: the design only, so the network comes up factory fresh. -->
    <q-dialog v-model="loading">
      <q-card style="min-width: 340px">
        <q-card-section class="text-subtitle2">Load scenario</q-card-section>
        <q-card-section class="text-caption text-grey-6 q-pt-none">
          The design only — every station comes up with no state and runs its
          setup lines.
        </q-card-section>
        <q-list dense bordered separator>
          <q-item v-for="name in sim.scenarios" :key="name" clickable v-close-popup
                  @click="guard(() => sim.loadScenario(name))">
            <q-item-section>{{ name }}</q-item-section>
            <q-item-section side v-if="name === sim.scenario?.name">
              <span class="text-caption text-grey-6">loaded</span>
            </q-item-section>
          </q-item>
        </q-list>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" v-close-popup />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <!-- Load a snapshot: the design and the state it had when it was taken. -->
    <q-dialog v-model="restoring">
      <q-card style="min-width: 340px">
        <q-card-section class="text-subtitle2">Load snapshot</q-card-section>
        <q-card-section class="text-caption text-grey-6 q-pt-none">
          The network as it was: identities, keys, paths and message history.
        </q-card-section>
        <q-list dense bordered separator>
          <q-item v-for="name in sim.snapshots" :key="name" clickable v-close-popup
                  @click="guard(() => sim.loadSnapshot(name))">
            <q-item-section>{{ name }}</q-item-section>
          </q-item>
        </q-list>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" v-close-popup />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <!-- Run command: one CLI line on every running station. -->
    <q-dialog v-model="commanding">
      <q-card style="min-width: 560px">
        <q-card-section class="text-subtitle2">Run command on all nodes</q-card-section>
        <q-card-section>
          <div class="row q-col-gutter-sm">
            <q-input class="col" v-model="commandLine" outlined dense autofocus
                     label="CLI line" :disable="waiting"
                     input-style="font-family: ui-monospace, monospace"
                     @keyup.enter="runCommand" />
            <q-input class="col-auto" style="width: 120px" v-model.number="commandSpread"
                     type="number" min="0" outlined dense :disable="waiting"
                     label="spread (s)" />
          </div>
          <div class="text-caption text-grey-6 q-mt-sm">
            <code>{name}</code>, <code>{id}</code> and <code>{addr}</code> are
            filled in per station, so <code>lxmf create {name}</code> does the
            right thing everywhere.
          </div>
          <div class="text-caption text-grey-6 q-mt-xs">
            <b>spread</b> is how many seconds to scatter the stations over.
            Leave it at 0 to ask them all at once; give it 30 or 60 for
            anything that puts something on the air, such as
            <code>lora 0 a</code> — two dozen stations announcing in the same
            instant is a collision storm rather than an announcement.
          </div>
        </q-card-section>
        <q-card-section v-if="sim.command" class="sim-results">
          <div v-for="(text, node) in sim.command.results" :key="node" class="sim-result">
            <span class="sim-result-node">{{ node }}</span>
            <pre>{{ text || '—' }}</pre>
          </div>
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Close" v-close-popup />
          <q-btn flat no-caps color="primary" label="Run" :loading="waiting"
                 :disable="!commandLine.trim()" @click="runCommand" />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <!-- Scenario settings: the air, and the lines every station is given. -->
    <q-dialog v-model="settings">
      <q-card style="min-width: 460px">
        <q-card-section class="text-subtitle2">Scenario settings</q-card-section>
        <q-card-section class="row q-col-gutter-sm">
          <q-input class="col" v-model.number="physics.exponent" type="number" step="0.1"
                   outlined dense label="Path-loss exponent" />
          <q-input class="col" v-model.number="physics.noise_figure_db" type="number"
                   outlined dense label="Noise figure (dB)" />
          <q-input class="col" v-model.number="physics.capture_db" type="number"
                   outlined dense label="Capture margin (dB)" />
        </q-card-section>
        <q-card-section>
          <div class="text-caption text-grey-6 q-mb-sm">
            Setup lines — CLI commands every station is given, in order.
          </div>
          <q-input v-model="setupDraft" type="textarea" outlined dense autogrow
                   input-style="font-family: ui-monospace, monospace" />
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" v-close-popup />
          <!-- Two buttons, because they are two different acts: the lines are
               the scenario's, and sending them is something done to a running
               network. One button labelled "Apply" that only did the first is
               how you end up reading settings the stations never got. -->
          <q-btn flat no-caps label="Save" @click="saveSettings(false)" />
          <q-btn flat no-caps color="primary" label="Save &amp; apply"
                 @click="saveSettings(true)" />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </q-layout>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useSim } from '../stores/sim'

const sim = useSim()
const quasar = useQuasar()
const loading = ref(false)
const restoring = ref(false)
const commanding = ref(false)
const settings = ref(false)
const commandLine = ref('')
const commandSpread = ref(0)
const waiting = ref(false)
const setupDraft = ref('')
const physics = reactive({ exponent: 2.7, noise_figure_db: 6, capture_db: 6 })

sim.connect()

/* Load, New and Reload all discard whatever has not been saved, so each of
 * them asks first — and only when there is in fact something to lose. */
function guard(go: () => void) {
  if (!sim.dirty) { go(); return }
  quasar.dialog({
    title: 'Unsaved changes',
    message: `${sim.scenario?.name} has changes that have not been saved. Discard them?`,
    cancel: true,
    persistent: true,
  }).onOk(go)
}

function askName(title: string, then: (name: string) => void) {
  quasar.dialog({
    title,
    message: 'Lower-case letters, digits and hyphens.',
    prompt: { model: '', type: 'text' },
    cancel: true,
  }).onOk((name: string) => { if (name.trim()) then(name.trim()) })
}

function askNew() { guard(() => askName('New scenario', n => sim.newScenario(n))) }
function askSaveAs() { askName('Save scenario as', n => sim.saveScenarioAs(n)) }
function askSnapshot() { askName('Save snapshot as', n => sim.saveSnapshotAs(n)) }

function askFactoryResetAll() {
  quasar.dialog({
    title: 'Factory reset all',
    message: 'Wipe every station\'s state and start them again from the setup '
           + 'lines. Identities, keys, paths and message history go. The map '
           + 'and the lines are untouched.',
    cancel: true,
    persistent: true,
  }).onOk(() => sim.factoryResetAll())
}

function runCommand() {
  const line = commandLine.value.trim()
  if (!line || waiting.value) return
  waiting.value = true
  sim.runCommand(line, Number(commandSpread.value) || 0)
}

// The replies arrive together, as one message, so the wait ends when they do.
watch(() => sim.command, () => { waiting.value = false })

function openSettings() {
  if (!sim.scenario) return
  Object.assign(physics, sim.scenario.physics)
  setupDraft.value = sim.scenario.setup.join('\n')
  settings.value = true
}

function saveSettings(apply: boolean) {
  sim.setPhysics({ ...physics })
  sim.setSetup(setupDraft.value.split('\n').filter(l => l.trim()))
  // The lines are settings, so re-sending them is harmless; what it costs is
  // a round of CLI traffic per running station, which is why it is asked for.
  if (apply) sim.applySetup()
  settings.value = false
}

// simd reports what it could not do; the page says so and moves on.
watch(() => sim.errors.length, () => {
  const text = sim.errors.pop()
  if (text) quasar.notify({ type: 'negative', message: text, timeout: 6000 })
})
</script>

<style scoped>
.sim-header { background: #171b21; box-shadow: none; border-bottom: 1px solid #262c35; }
.sim-toolbar { min-height: 38px; padding-left: 4px; gap: 2px; }
.sim-menu-btn { font-size: 13px; }
.sim-title { font-size: 14px; font-weight: 500; padding-left: 12px; }
.sim-dirty { color: #f59e0b; }
.sim-empty { color: #6b7280; font-weight: 400; }
.sim-count { font: 11px ui-monospace, monospace; color: #6b7280; padding-right: 12px; }
.sim-link { font: 11px ui-monospace, monospace; color: #22c55e; padding-right: 8px; }
.sim-link-off { color: #f59e0b; }
.sim-page-container { height: 100vh; }
.sim-hint { font-size: 11px; color: #6b7280; }
.sim-results { max-height: 46vh; overflow-y: auto; border-top: 1px solid #2b313b; }
.sim-result { display: flex; gap: 10px; padding: 3px 0; }
.sim-result-node {
  flex: none;
  width: 84px;
  font: 12px ui-monospace, monospace;
  color: #7dd3fc;
}
.sim-result pre {
  margin: 0;
  font: 12px ui-monospace, monospace;
  color: #c7ccd4;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
