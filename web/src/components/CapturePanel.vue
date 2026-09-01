<script setup>
/**
 * Recording an input device into the library.
 *
 * The device belongs to the *server* - the same rule as the live panes
 * (CLAUDE.md, "The mic is the server's"). What that buys here is streamed
 * music: with a loopback driver (BlackHole on macOS) as the input, a song you
 * can only stream becomes a wav the pipeline can read, because the machine
 * running Scriptum is the machine playing it.
 *
 * The session lives on the server too, so this panel is a *view* of a
 * recording rather than the recording itself. Closing the tab mid-take does
 * not lose it: the panel asks on mount and re-attaches to whatever is running.
 *
 * The meter is polled at 4Hz. A level is a gauge, not a transcript - the
 * opposite of the job log, which is why it does not use the job machinery.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, mmss } from '../api'

const emit = defineEmits(['captured', 'error'])

const open = ref(false)
const devices = ref([])
const device = ref(null)
const name = ref('')
const status = ref(null)     // the server's view while a take is running
const last = ref(null)       // what the take became, once it stops
const err = ref('')
const busy = ref(false)
let timer = null

const recording = computed(() => !!status.value?.active)

/** Loopback drivers are the whole point here, so they sort first and say so. */
const LOOPBACK = /blackhole|loopback|soundflower|vb-audio|voicemeeter|virtual/i
const isLoopback = (d) => LOOPBACK.test(d.name)
const sorted = computed(() =>
  [...devices.value].sort((a, b) => isLoopback(b) - isLoopback(a)))

/**
 * The meter, as a percentage of a -60..0 dB scale.
 *
 * Digital silence reads -140 dB, which on a linear scale would be a bar that
 * never moves off zero for anything quiet. -60 is the floor worth showing.
 */
const level = computed(() => {
  const db = status.value?.peak_db ?? -140
  return Math.max(0, Math.min(100, ((db + 60) / 60) * 100))
})
const quiet = computed(() => (status.value?.quiet_seconds ?? 0) > 3)

async function loadDevices() {
  try {
    const r = await api.devices()
    devices.value = r.devices || []
    if (device.value === null && devices.value.length) {
      const pick = sorted.value.find(isLoopback)
        || devices.value.find((d) => d.default) || devices.value[0]
      device.value = pick.index
    }
    if (r.error) err.value = r.error
  } catch (e) { err.value = e.message }
}

function poll() {
  clearInterval(timer)
  timer = setInterval(async () => {
    try {
      status.value = await api.captureStatus()
      if (!status.value.active) clearInterval(timer)
    } catch { /* a dropped poll is not worth stopping the take over */ }
  }, 250)
}

async function show() {
  open.value = true
  await loadDevices()
}

async function start() {
  busy.value = true
  err.value = ''
  last.value = null
  try {
    status.value = await api.captureStart({ name: name.value, device: device.value })
    poll()
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

async function stop(discard = false) {
  busy.value = true
  try {
    const out = await api.captureStop({ discard })
    clearInterval(timer)
    status.value = null
    if (out.written) {
      last.value = out
      emit('captured', out)
    }
  } catch (e) { err.value = e.message; emit('error', e.message) } finally { busy.value = false }
}

// A take running on the server outlives this component, so find out about it
// rather than showing an idle panel over a live recording.
onMounted(async () => {
  try {
    const s = await api.captureStatus()
    if (s.active) { status.value = s; open.value = true; poll(); await loadDevices() }
  } catch { /* server not up yet; the button still works */ }
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div class="capture">
    <button class="btn" :class="{ live: recording }" @click="open ? (open = false) : show()">
      <span class="dot" :class="{ on: recording }" />
      {{ recording ? 'Recording…' : 'Capture audio' }}
    </button>

    <div v-if="open" class="panel card">
      <div class="eyebrow">Record what this machine is playing</div>

      <div v-if="err" class="err">{{ err }}</div>

      <template v-if="!recording">
        <p class="muted small">
          Pick a loopback input (BlackHole) and Scriptum records the computer's
          own output — which is how a song you can only stream becomes a file it
          can analyse. Point it at a microphone instead and it records the room.
        </p>

        <label class="field">
          <span class="lab">Song name</span>
          <input
            v-model="name" class="txt" spellcheck="false"
            placeholder="Livin on a Prayer" @keyup.enter="start"
          />
        </label>

        <label class="field">
          <span class="lab">Input</span>
          <select v-model="device" class="sel">
            <option v-for="d in sorted" :key="d.index" :value="d.index">
              {{ d.name }}{{ isLoopback(d) ? '  — loopback' : '' }}
              ({{ d.channels }}ch, {{ Math.round(d.samplerate / 1000) }} kHz)
            </option>
          </select>
        </label>

        <p v-if="!devices.length" class="warn">
          No input devices. On Linux this is usually a missing
          <code class="mono">libportaudio2</code>; on macOS the app that started
          Scriptum needs the Microphone permission.
        </p>

        <div v-if="last" class="done">
          Captured <strong>{{ last.filename }}</strong> —
          {{ mmss(last.seconds) }}, peak {{ last.peak_db }} dB
          <span v-if="last.dropouts" class="bad">· {{ last.dropouts }} dropout(s)</span>
        </div>

        <footer class="foot">
          <span class="muted small">Start it, then press play in Music.</span>
          <button
            class="btn btn-primary btn-sm" :disabled="busy || !devices.length"
            @click="start"
          >Start recording</button>
        </footer>
      </template>

      <template v-else>
        <div class="meterrow">
          <span class="clock mono">{{ mmss(status.seconds) }}</span>
          <div class="meter"><div class="fill" :style="{ width: level + '%' }" /></div>
          <span class="db mono">{{ status.peak_db }} dB</span>
        </div>
        <div class="dim tiny">
          {{ status.device }} · {{ status.channels === 2 ? 'stereo' : 'mono' }}
          · {{ Math.round(status.samplerate / 1000) }} kHz
          <span v-if="status.dropouts" class="bad">· {{ status.dropouts }} dropout(s)</span>
        </div>

        <!-- Silence has one cause here, and four minutes of it is worth
             interrupting for rather than discovering after the take. -->
        <div v-if="quiet" class="warn">
          <strong>Hearing nothing for {{ Math.round(status.quiet_seconds) }}s.</strong>
          Check that the song is actually playing, and that macOS output
          (<span class="mono">Systemeinstellungen → Ton → Ausgabe</span>) is set to
          <strong>BlackHole 2ch</strong>, or to a <strong>Multiausgangsgerät</strong>
          containing it. In Music, the speaker menu must say <em>Computer</em> —
          AirPlay never reaches the loopback device.
        </div>
        <div v-else-if="status.full" class="warn">
          The buffer is full — stop now and keep what you have.
        </div>

        <footer class="foot">
          <button class="btn btn-sm" :disabled="busy" @click="stop(true)">Discard</button>
          <button class="btn btn-primary btn-sm" :disabled="busy" @click="stop(false)">
            Stop and keep
          </button>
        </footer>
      </template>
    </div>
  </div>
</template>

<style scoped>
.capture { position: relative; display: inline-block; }
.btn.live { border-color: var(--red-bright); color: var(--text); }
.dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--line-strong); margin-right: 6px; vertical-align: 1px;
}
.dot.on { background: var(--red-bright); box-shadow: 0 0 7px var(--red-bright); }

.panel {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 30;
  width: min(520px, 90vw); padding: 14px 16px 15px;
  box-shadow: 0 18px 50px rgba(0, 0, 0, 0.5);
}
.eyebrow {
  font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--text-4); margin-bottom: 8px;
}
.small { font-size: 11.5px; }
.tiny { font-size: 11px; margin-top: 7px; }

.field { display: flex; align-items: center; gap: 9px; margin-top: 9px; }
.lab { flex: none; width: 82px; font-size: 12px; color: var(--text-3); }
.txt, .sel {
  flex: 1; min-width: 0; font-size: 12.5px; padding: 5px 8px; border-radius: 5px;
  background: var(--bg-deep); border: 1px solid var(--line-soft); color: var(--text);
}

.meterrow { display: flex; align-items: center; gap: 10px; }
.clock { font-size: 15px; flex: none; }
.db { font-size: 11.5px; color: var(--text-3); flex: none; width: 62px; text-align: right; }
.meter {
  flex: 1; height: 8px; border-radius: 4px; overflow: hidden;
  background: var(--bg-deep); border: 1px solid var(--line-soft);
}
.fill {
  height: 100%; transition: width 0.12s linear;
  background: linear-gradient(90deg, var(--ok), var(--gold) 78%, var(--red-bright));
}

.done {
  margin-top: 10px; padding: 8px 11px; border-radius: 6px; font-size: 12px;
  border: 1px solid var(--gold-dim); color: var(--text-2);
  background: rgba(217, 164, 65, 0.09);
}
.bad { color: var(--err); }

.foot {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; margin-top: 13px;
}
.err, .warn {
  padding: 9px 12px; border-radius: 6px; font-size: 12px; margin-top: 10px;
  border: 1px solid var(--red-deep); color: #f0a8b4; background: rgba(168, 23, 47, 0.12);
}
.warn {
  border-color: var(--gold-dim); color: var(--text-2);
  background: rgba(217, 164, 65, 0.09);
}
.warn strong { color: var(--gold); }
</style>
