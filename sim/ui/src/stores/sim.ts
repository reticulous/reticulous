import { defineStore } from 'pinia'

/* The one store. It owns the websocket to simd and everything that arrives on
 * it; every component reads it, and every action a person takes is one method
 * here that sends one message. Reconnecting replays the snapshot, so the page
 * has no state simd cannot restate. */

export type Status = 'stopped' | 'starting' | 'setup' | 'up' | 'restarting'

export interface Node {
  name: string
  id: number
  pos: [number, number]      // latitude, longitude
  gain_db: number
  setup: string[]
  status: Status
  transport: boolean | null
  /** The carrier the station last said it was on — the map shows it on hover. */
  freq?: number
  sf?: number
  bw?: number
  mode?: string
}

export interface Scenario {
  name: string
  dirty: boolean
  origin: [number, number]
  physics: { exponent: number; noise_figure_db: number; capture_db: number }
  setup: string[]
  obstructions: { between: [string, string]; db: number }[]
}

/** A transmission in the air, on the browser's clock: the ether's microseconds
 *  are its own and mean nothing here, but a frame's duration does. */
export interface Pulse {
  eid: number
  name: string
  start: number              // performance.now() when the tx arrived
  duration: number           // ms the frame occupies the air
}

/** One reception, drawn as a flash at the receiver in the verdict's colour. */
export interface Flash {
  name: string
  from: string
  verdict: 'clean' | 'crc'
  level: number
  start: number
}

const FLASH_MS = 400
const MAX_PULSES = 400       // a busy network, bounded

export const useSim = defineStore('sim', {
  state: () => ({
    connected: false,
    scenario: null as Scenario | null,
    scenarios: [] as string[],
    snapshots: [] as string[],
    /** The last `Run command` and what each station said back. */
    command: null as { line: string; results: Record<string, string> } | null,
    nodes: {} as Record<string, Node>,
    pulses: [] as Pulse[],
    flashes: [] as Flash[],
    /** What each node is heard at by its neighbours, from a `levels` request. */
    levels: {} as Record<string, Record<string, number>>,
    /** The port the browser reached simd on, for the station links. */
    port: '9011',
    errors: [] as string[],
    socket: null as WebSocket | null,
  }),

  getters: {
    nodeList: (s): Node[] => Object.values(s.nodes).sort((a, b) => a.id - b.id),
    loaded: (s): boolean => s.scenario !== null,
    dirty: (s): boolean => s.scenario?.dirty ?? false,
    running: (s): number =>
      Object.values(s.nodes).filter(n => n.status === 'up').length,
  },

  actions: {
    connect() {
      if (this.socket) return
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      const socket = new WebSocket(`${proto}//${location.host}/ws`)
      this.socket = socket
      socket.onopen = () => { this.connected = true }
      socket.onclose = () => {
        this.connected = false
        this.socket = null
        // simd is the process; if it went away there is nothing to talk to
        // until it is back, so keep trying rather than leaving a dead page.
        setTimeout(() => this.connect(), 1000)
      }
      socket.onmessage = (event) => this.receive(JSON.parse(event.data))
    },

    receive(msg: Record<string, unknown>) {
      switch (msg.type) {
        case 'snapshot': {
          this.scenario = (msg.scenario as Scenario | null) ?? null
          this.scenarios = (msg.scenarios as string[]) ?? []
          this.snapshots = (msg.snapshots as string[]) ?? []
          this.port = String(msg.port ?? '9011')
          this.nodes = {}
          for (const node of (msg.nodes as Node[]) ?? []) this.nodes[node.name] = node
          this.pulses = []
          this.flashes = []
          break
        }
        case 'node': {
          const node = msg as unknown as Node
          // Keep the radio fields a `radio` message put here: a status change
          // says nothing about the carrier and must not blank it.
          this.nodes[node.name] = { ...this.nodes[node.name], ...node }
          break
        }
        case 'node_gone':
          delete this.nodes[msg.name as string]
          break
        case 'scenario': {
          this.scenario = { ...(this.scenario ?? {}), ...msg } as Scenario
          if (msg.scenarios) this.scenarios = msg.scenarios as string[]
          if (msg.snapshots) this.snapshots = msg.snapshots as string[]
          break
        }
        case 'command_result':
          this.command = {
            line: msg.line as string,
            results: msg.results as Record<string, string>,
          }
          break
        case 'radio': {
          const node = this.nodes[msg.name as string]
          if (node) Object.assign(node, {
            mode: msg.mode, freq: msg.freq, sf: msg.sf, bw: msg.bw,
          })
          break
        }
        case 'tx': {
          const span = (msg.t_end as number) - (msg.t_start as number)
          this.pulses.push({
            eid: msg.eid as number,
            name: msg.name as string,
            start: performance.now(),
            duration: Math.max(60, span / 1000),
          })
          if (this.pulses.length > MAX_PULSES) this.pulses.splice(0, this.pulses.length - MAX_PULSES)
          break
        }
        case 'rx':
          this.flashes.push({
            name: msg.name as string,
            from: msg.from as string,
            verdict: msg.verdict as 'clean' | 'crc',
            level: msg.level as number,
            start: performance.now(),
          })
          break
        case 'levels':
          this.levels[msg.name as string] = msg.heard as Record<string, number>
          break
        case 'error':
          this.errors.push(msg.text as string)
          break
      }
    },

    /** Drop everything that has finished animating. Called from the map's frame. */
    expire(now: number) {
      this.pulses = this.pulses.filter(p => now - p.start < p.duration)
      this.flashes = this.flashes.filter(f => now - f.start < FLASH_MS)
    },

    send(msg: Record<string, unknown>) {
      if (this.socket?.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify(msg))
      }
    },

    /* ── the network ── */
    addNode(name: string, pos: [number, number]) {
      this.send({ type: 'node_add', name, pos })
    },
    moveNode(name: string, pos: [number, number], settle = true) {
      const node = this.nodes[name]
      if (node) node.pos = pos          // the drag is local until it settles
      this.send({ type: 'node_move', name, pos, settle })
    },
    removeNode(name: string) { this.send({ type: 'node_remove', name }) },
    /** Press reset: the process goes and comes back, state untouched. */
    resetNode(name: string) { this.send({ type: 'node_reset', name }) },
    /** Wipe its state and start it again from the setup lines. */
    factoryResetNode(name: string) { this.send({ type: 'node_factory_reset', name }) },
    setNodeSetup(name: string, lines: string[]) {
      this.send({ type: 'node_setup', name, lines })
    },
    askLevels(name: string) { this.send({ type: 'levels', name }) },
    setObstruction(a: string, b: string, db: number) {
      this.send({ type: 'obstruction', between: [a, b], db })
    },

    /* ── the run ── */
    startAll() { this.send({ type: 'start_all' }) },
    stopAll() { this.send({ type: 'stop_all' }) },
    resetAll() { this.send({ type: 'reset_all' }) },
    factoryResetAll() { this.send({ type: 'factory_reset_all' }) },
    /** One CLI line on every running station, `{name}` and friends expanded.
     *  `stagger` spreads the stations over that many seconds — 0 fires them
     *  together, which is wrong for anything that transmits. */
    runCommand(line: string, stagger = 0) {
      this.command = null
      this.send({ type: 'command', line, stagger })
    },
    setPhysics(values: Record<string, number>) {
      this.send({ type: 'physics', ...values })
    },
    setSetup(lines: string[]) { this.send({ type: 'setup', lines }) },

    /* ── scenarios: the design ── */
    newScenario(name: string) { this.send({ type: 'scenario_new', name }) },
    loadScenario(name: string) { this.send({ type: 'scenario_load', name }) },
    saveScenario() { this.send({ type: 'scenario_save' }) },
    saveScenarioAs(name: string) { this.send({ type: 'scenario_save_as', name }) },

    /* ── snapshots: the design and everything that has happened to it ── */
    loadSnapshot(name: string) { this.send({ type: 'snapshot_load', name }) },
    saveSnapshotAs(name: string) { this.send({ type: 'snapshot_save_as', name }) },

    /** The station's own web UI, through the proxy on this same port. */
    stationUrl(name: string) {
      return `${location.protocol}//${name}.sim.localhost:${this.port}/`
    },
  },
})
