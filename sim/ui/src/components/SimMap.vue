<template>
  <div ref="wrap" class="simmap">
    <canvas
      ref="canvas"
      @pointerdown="onDown"
      @pointermove="onMove"
      @pointerup="onUp"
      @pointercancel="onUp"
      @wheel.prevent="onWheel"
      @contextmenu.prevent="onContext"
    />
    <div class="simmap-scale">{{ scaleLabel }}</div>
  </div>
</template>

<script setup lang="ts">
/* The map, drawn rather than laid out in the DOM: a grid, a few hundred pulses
 * a minute and a drag at 60 Hz are all far cheaper on a canvas, and none of
 * them wants to be an element.
 *
 * The view is equirectangular around the scenario's origin, exactly the
 * projection the ether uses, so a pixel on the map and a metre in the medium
 * agree by construction rather than by two implementations staying in step. */
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useSim, type Node } from '../stores/sim'

const EARTH_RADIUS_M = 6371008.8
const NODE_R = 6                 // the dot, in px
const HIT_R = 16                 // how close a pointer must be to grab one
const FLASH_MS = 400
const GRID_MIN_PX = 60
const GRID_MAX_PX = 150
const DRAG_SEND_HZ = 6           // how often a drag tells simd, so links fade live

const sim = useSim()
const wrap = ref<HTMLDivElement>()
const canvas = ref<HTMLCanvasElement>()

const emit = defineEmits<{
  select: [name: string]
  context: [pos: [number, number]]
}>()

/* ── the view ──
 * centre in metres from the scenario origin, and metres per pixel. */
const view = ref({ cx: 0, cy: 0, mpp: 40 })
const hovered = ref<string | null>(null)

let ctx: CanvasRenderingContext2D | null = null
let frame = 0
let redrawWanted = true
let size = { w: 0, h: 0, dpr: 1 }

/* ── projection ── */
function origin(): [number, number] {
  return sim.scenario?.origin ?? [0, 0]
}

function toMetres(lat: number, lon: number): [number, number] {
  const [lat0, lon0] = origin()
  const x = ((lon - lon0) * Math.PI / 180) * Math.cos(lat0 * Math.PI / 180) * EARTH_RADIUS_M
  const y = ((lat - lat0) * Math.PI / 180) * EARTH_RADIUS_M
  return [x, y]
}

function toDegrees(x: number, y: number): [number, number] {
  const [lat0, lon0] = origin()
  const lat = lat0 + (y / EARTH_RADIUS_M) * 180 / Math.PI
  const lon = lon0 + (x / (EARTH_RADIUS_M * Math.cos(lat0 * Math.PI / 180))) * 180 / Math.PI
  return [lat, lon]
}

function toScreen(lat: number, lon: number): [number, number] {
  const [x, y] = toMetres(lat, lon)
  return [size.w / 2 + (x - view.value.cx) / view.value.mpp,
          size.h / 2 - (y - view.value.cy) / view.value.mpp]
}

function fromScreen(sx: number, sy: number): [number, number] {
  const x = view.value.cx + (sx - size.w / 2) * view.value.mpp
  const y = view.value.cy - (sy - size.h / 2) * view.value.mpp
  return toDegrees(x, y)
}

/* ── the view is remembered per scenario, so a reload comes back where it was ── */
function viewKey() {
  return `sim.view.${sim.scenario?.name ?? '-'}`
}

function saveView() {
  try { localStorage.setItem(viewKey(), JSON.stringify(view.value)) } catch { /* private window */ }
}

function restoreView() {
  try {
    const held = localStorage.getItem(viewKey())
    if (held) { view.value = JSON.parse(held); redrawWanted = true; return }
  } catch { /* private window */ }
  fitToNodes()
}

/** Put every node on screen with a margin, which is what a freshly loaded
 *  scenario wants and what an empty one falls back to. */
function fitToNodes() {
  const nodes = sim.nodeList
  if (!nodes.length) { view.value = { cx: 0, cy: 0, mpp: 40 }; redrawWanted = true; return }
  const points = nodes.map(n => toMetres(n.pos[0], n.pos[1]))
  const xs = points.map(p => p[0]), ys = points.map(p => p[1])
  const minX = Math.min(...xs), maxX = Math.max(...xs)
  const minY = Math.min(...ys), maxY = Math.max(...ys)
  const spanX = Math.max(maxX - minX, 200), spanY = Math.max(maxY - minY, 200)
  view.value = {
    cx: (minX + maxX) / 2,
    cy: (minY + maxY) / 2,
    mpp: Math.max(spanX / Math.max(size.w - 120, 200),
                  spanY / Math.max(size.h - 120, 200)),
  }
  redrawWanted = true
}

defineExpose({ fitToNodes })

/* ── the grid: 1, 2 or 5 times a power of ten, whichever lands in the band ── */
function gridStep(): number {
  const target = (GRID_MIN_PX + GRID_MAX_PX) / 2 * view.value.mpp
  const decade = Math.pow(10, Math.floor(Math.log10(target)))
  for (const multiple of [1, 2, 5, 10]) {
    const step = multiple * decade
    if (step / view.value.mpp >= GRID_MIN_PX) return step
  }
  return 10 * decade
}

const scaleLabel = computed(() => {
  const step = gridStep()
  return step >= 1000 ? `${(step / 1000).toFixed(step >= 10000 ? 0 : 1)} km grid`
                      : `${Math.round(step)} m grid`
})

function metresLabel(v: number) {
  const a = Math.abs(v)
  if (a >= 1000) return `${(v / 1000).toFixed(a >= 10000 ? 0 : 1)}k`
  return `${Math.round(v)}`
}

/* ── drawing ── */
const STATUS_COLOUR: Record<string, string> = {
  stopped: '#6b7280',
  starting: '#f59e0b',
  setup: '#f59e0b',
  restarting: '#ef4444',
  up: '#e5e7eb',
}

function draw() {
  if (!ctx) return
  const { w, h } = size
  ctx.clearRect(0, 0, w, h)
  ctx.fillStyle = '#121417'
  ctx.fillRect(0, 0, w, h)

  drawGrid()
  drawObstructions()
  if (hovered.value) drawHeard(hovered.value)
  drawPulses()
  drawNodes()
}

function drawGrid() {
  if (!ctx) return
  const { w, h } = size
  const step = gridStep()
  const left = view.value.cx - (w / 2) * view.value.mpp
  const right = view.value.cx + (w / 2) * view.value.mpp
  const bottom = view.value.cy - (h / 2) * view.value.mpp
  const top = view.value.cy + (h / 2) * view.value.mpp

  ctx.lineWidth = 1
  ctx.font = '11px ui-monospace, monospace'
  ctx.textBaseline = 'top'

  for (let x = Math.ceil(left / step) * step; x <= right; x += step) {
    const sx = Math.round(w / 2 + (x - view.value.cx) / view.value.mpp) + 0.5
    // The axis through the origin is the scenario's own latitude and
    // longitude, so it is drawn brighter than the metre grid over it.
    ctx.strokeStyle = Math.abs(x) < step / 2 ? '#3b4351' : '#1e232b'
    ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, h); ctx.stroke()
    ctx.fillStyle = '#4b5563'
    ctx.fillText(metresLabel(x), sx + 3, 3)
  }
  for (let y = Math.ceil(bottom / step) * step; y <= top; y += step) {
    const sy = Math.round(h / 2 - (y - view.value.cy) / view.value.mpp) + 0.5
    ctx.strokeStyle = Math.abs(y) < step / 2 ? '#3b4351' : '#1e232b'
    ctx.beginPath(); ctx.moveTo(0, sy); ctx.lineTo(w, sy); ctx.stroke()
    ctx.fillStyle = '#4b5563'
    ctx.fillText(metresLabel(y), 3, sy + 3)
  }

  const [lat0, lon0] = origin()
  ctx.fillStyle = '#4b5563'
  ctx.fillText(`origin ${lat0.toFixed(4)}, ${lon0.toFixed(4)}`, 3, h - 16)
}

function drawObstructions() {
  if (!ctx) return
  for (const wall of sim.scenario?.obstructions ?? []) {
    const a = sim.nodes[wall.between[0]], b = sim.nodes[wall.between[1]]
    if (!a || !b) continue
    const [ax, ay] = toScreen(a.pos[0], a.pos[1])
    const [bx, by] = toScreen(b.pos[0], b.pos[1])
    ctx.strokeStyle = '#7f1d1d'
    ctx.lineWidth = 1.5
    ctx.setLineDash([5, 4])
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke()
    ctx.setLineDash([])
    ctx.fillStyle = '#b91c1c'
    ctx.font = '11px ui-monospace, monospace'
    ctx.fillText(`${wall.db} dB`, (ax + bx) / 2 + 4, (ay + by) / 2 - 12)
  }
}

/** Who the hovered node is heard by, and at what level. */
function drawHeard(name: string) {
  if (!ctx) return
  const from = sim.nodes[name]
  const heard = sim.levels[name]
  if (!from || !heard) return
  const [fx, fy] = toScreen(from.pos[0], from.pos[1])
  ctx.font = '11px ui-monospace, monospace'
  for (const [other, level] of Object.entries(heard)) {
    const node = sim.nodes[other]
    if (!node) continue
    const [ox, oy] = toScreen(node.pos[0], node.pos[1])
    // The stronger the link, the brighter the line: -60 dBm is loud here and
    // -150 is the edge of earshot.
    const strength = Math.min(1, Math.max(0.12, (level + 150) / 90))
    ctx.strokeStyle = `rgba(56, 189, 248, ${strength})`
    ctx.lineWidth = 1
    ctx.beginPath(); ctx.moveTo(fx, fy); ctx.lineTo(ox, oy); ctx.stroke()
    ctx.fillStyle = 'rgba(125, 211, 252, 0.85)'
    ctx.fillText(`${level.toFixed(0)}`, (fx + ox) / 2 + 3, (fy + oy) / 2 + 3)
  }
}

function drawPulses() {
  if (!ctx) return
  const now = performance.now()
  for (const pulse of sim.pulses) {
    const node = sim.nodes[pulse.name]
    if (!node) continue
    const age = (now - pulse.start) / pulse.duration
    if (age < 0 || age > 1) continue
    const [sx, sy] = toScreen(node.pos[0], node.pos[1])
    // The ring is the frame occupying the air, not its range: it grows for as
    // long as the transmission lasts and fades as it ends.
    ctx.strokeStyle = `rgba(250, 204, 21, ${0.55 * (1 - age)})`
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.arc(sx, sy, NODE_R + age * 70, 0, Math.PI * 2)
    ctx.stroke()
  }
}

function drawNodes() {
  if (!ctx) return
  const now = performance.now()
  const flashOf = new Map<string, { verdict: string; age: number }>()
  for (const flash of sim.flashes) {
    const age = (now - flash.start) / FLASH_MS
    if (age >= 0 && age <= 1) flashOf.set(flash.name, { verdict: flash.verdict, age })
  }

  ctx.font = '12px ui-sans-serif, system-ui, sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'top'

  for (const node of sim.nodeList) {
    const [sx, sy] = toScreen(node.pos[0], node.pos[1])
    const flash = flashOf.get(node.name)
    if (flash) {
      ctx.fillStyle = flash.verdict === 'clean'
        ? `rgba(34, 197, 94, ${0.6 * (1 - flash.age)})`
        : `rgba(239, 68, 68, ${0.6 * (1 - flash.age)})`
      ctx.beginPath(); ctx.arc(sx, sy, NODE_R + 9, 0, Math.PI * 2); ctx.fill()
    }
    if (node.transport) {
      // A transport node carries other nodes' traffic; the second ring is the
      // whole of that difference and is read at a glance across a big map.
      ctx.strokeStyle = '#38bdf8'
      ctx.lineWidth = 1.5
      ctx.beginPath(); ctx.arc(sx, sy, NODE_R + 4, 0, Math.PI * 2); ctx.stroke()
    }
    ctx.fillStyle = STATUS_COLOUR[node.status] ?? '#6b7280'
    ctx.beginPath(); ctx.arc(sx, sy, NODE_R, 0, Math.PI * 2); ctx.fill()
    if (hovered.value === node.name || dragging?.name === node.name) {
      ctx.strokeStyle = '#ffffff'
      ctx.lineWidth = 2
      ctx.beginPath(); ctx.arc(sx, sy, NODE_R + 2, 0, Math.PI * 2); ctx.stroke()
    }
    ctx.fillStyle = node.status === 'up' ? '#d1d5db' : '#8b93a1'
    ctx.fillText(node.name, sx, sy + NODE_R + 4)
  }
  ctx.textAlign = 'left'
}

/* ── the frame loop ──
 * Draw when something moved, and every frame while anything is animating. */
function tick() {
  const now = performance.now()
  sim.expire(now)
  if (redrawWanted || sim.pulses.length || sim.flashes.length) {
    redrawWanted = false
    draw()
  }
  frame = requestAnimationFrame(tick)
}

/* ── pointer ── */
interface Drag { name?: string; lastSend: number; from: { x: number; y: number }
                 start: { cx: number; cy: number }; moved: boolean }
let dragging: Drag | null = null

function nodeAt(sx: number, sy: number): Node | null {
  let best: Node | null = null
  let bestDistance = HIT_R
  for (const node of sim.nodeList) {
    const [nx, ny] = toScreen(node.pos[0], node.pos[1])
    const distance = Math.hypot(nx - sx, ny - sy)
    if (distance <= bestDistance) { best = node; bestDistance = distance }
  }
  return best
}

function pointer(event: PointerEvent) {
  const box = canvas.value!.getBoundingClientRect()
  return { x: event.clientX - box.left, y: event.clientY - box.top }
}

function onDown(event: PointerEvent) {
  if (event.button !== 0) return
  const at = pointer(event)
  const node = nodeAt(at.x, at.y)
  canvas.value?.setPointerCapture(event.pointerId)
  dragging = {
    name: node?.name,
    lastSend: 0,
    from: at,
    start: { cx: view.value.cx, cy: view.value.cy },
    moved: false,
  }
}

function onMove(event: PointerEvent) {
  const at = pointer(event)
  if (!dragging) {
    const node = nodeAt(at.x, at.y)
    const name = node?.name ?? null
    if (name !== hovered.value) {
      hovered.value = name
      if (name) sim.askLevels(name)     // the card and the lines want the levels
      redrawWanted = true
    }
    return
  }
  const dx = at.x - dragging.from.x
  const dy = at.y - dragging.from.y
  if (Math.abs(dx) > 2 || Math.abs(dy) > 2) dragging.moved = true
  if (dragging.name) {
    const pos = fromScreen(at.x, at.y)
    const now = performance.now()
    // The ether is told a few times a second while the drag runs, so a person
    // can watch a link fade as they pull a station away; the scenario is only
    // written when the drag settles.
    const settle = false
    if (now - dragging.lastSend > 1000 / DRAG_SEND_HZ) {
      dragging.lastSend = now
      sim.moveNode(dragging.name, pos, settle)
      sim.askLevels(dragging.name)
    } else {
      const node = sim.nodes[dragging.name]
      if (node) node.pos = pos
    }
  } else {
    view.value.cx = dragging.start.cx - dx * view.value.mpp
    view.value.cy = dragging.start.cy + dy * view.value.mpp
  }
  redrawWanted = true
}

function onUp(event: PointerEvent) {
  if (!dragging) return
  const at = pointer(event)
  const held = dragging
  dragging = null
  canvas.value?.releasePointerCapture(event.pointerId)
  if (held.name) {
    if (held.moved) {
      sim.moveNode(held.name, fromScreen(at.x, at.y), true)
    } else {
      emit('select', held.name)
    }
  } else if (held.moved) {
    saveView()
  }
  redrawWanted = true
}

function onWheel(event: WheelEvent) {
  const box = canvas.value!.getBoundingClientRect()
  const at = { x: event.clientX - box.left, y: event.clientY - box.top }
  // Zoom about the cursor: the point under it stays under it.
  const before = fromScreen(at.x, at.y)
  const factor = Math.exp(event.deltaY * 0.0015)
  view.value.mpp = Math.min(5000, Math.max(0.05, view.value.mpp * factor))
  const [x, y] = toMetres(before[0], before[1])
  view.value.cx = x - (at.x - size.w / 2) * view.value.mpp
  view.value.cy = y + (at.y - size.h / 2) * view.value.mpp
  saveView()
  redrawWanted = true
}

function onContext(event: MouseEvent) {
  const box = canvas.value!.getBoundingClientRect()
  const at = { x: event.clientX - box.left, y: event.clientY - box.top }
  emit('context', fromScreen(at.x, at.y))
}

/* ── sizing ── */
function resize() {
  const element = wrap.value
  const surface = canvas.value
  if (!element || !surface) return
  const dpr = window.devicePixelRatio || 1
  size = { w: element.clientWidth, h: element.clientHeight, dpr }
  surface.width = Math.round(size.w * dpr)
  surface.height = Math.round(size.h * dpr)
  surface.style.width = `${size.w}px`
  surface.style.height = `${size.h}px`
  ctx = surface.getContext('2d')
  ctx?.setTransform(dpr, 0, 0, dpr, 0, 0)
  redrawWanted = true
}

let observer: ResizeObserver | null = null

onMounted(async () => {
  await nextTick()
  resize()
  observer = new ResizeObserver(resize)
  if (wrap.value) observer.observe(wrap.value)
  restoreView()
  frame = requestAnimationFrame(tick)
})

onUnmounted(() => {
  cancelAnimationFrame(frame)
  observer?.disconnect()
  saveView()
})

// A different scenario is a different map; come back to where that one was.
watch(() => sim.scenario?.name, () => { restoreView() })
watch(() => sim.nodeList.length, () => { redrawWanted = true })
// A levels answer arrives well after the hover that asked for it, and on a
// quiet network nothing else is animating to carry it onto the canvas.
watch(() => sim.levels, () => { redrawWanted = true }, { deep: true })
watch(() => sim.scenario?.obstructions, () => { redrawWanted = true }, { deep: true })
</script>

<style scoped>
.simmap {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: #121417;
}
.simmap canvas {
  display: block;
  cursor: crosshair;
  touch-action: none;
}
.simmap-scale {
  position: absolute;
  right: 10px;
  bottom: 8px;
  font: 11px ui-monospace, monospace;
  color: #6b7280;
  pointer-events: none;
}
</style>
