<script setup>
/**
 * "What key are we in?" - for jamming along with a band that has not told you.
 *
 * The estimate is the same Krumhansl-Kessler correlation the offline analysis
 * uses, run on a rolling window of the last 20 seconds, with the chord track
 * breaking the major/relative-minor tie. What the page adds is the part you
 * actually need on stage: the notes that are safe to play, laid out on a
 * fretboard.
 */
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { useLive } from '../composables/useLive'
import LevelMeter from '../components/LevelMeter.vue'

const live = useLive()
const devices = ref([])
const device = ref('')
const noDevices = ref('')
const tuning = ref('guitar')

const TUNINGS = {
  guitar: { labels: ['E', 'A', 'D', 'G', 'B', 'e'], open: [40, 45, 50, 55, 59, 64] },
  bass: { labels: ['E', 'A', 'D', 'G'], open: [28, 33, 38, 43] },
}
const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
const FRETS = 13

onMounted(async () => {
  try {
    const res = await api.devices()
    devices.value = res.devices || []
    if (res.error) noDevices.value = res.error
    const def = devices.value.find((d) => d.default)
    if (def) device.value = String(def.index)
  } catch (e) { noDevices.value = e.message }
})

function toggle() {
  if (live.connected.value) live.stop()
  // `instrument: other` widens the pitch window: the mic is hearing a whole
  // band here, not one instrument.
  else live.start({ mode: 'key', instrument: 'other', device: device.value, fps: 4 })
}

const f = computed(() => live.frame.value)
const key = computed(() => f.value?.key || '')
const scale = computed(() => f.value?.scale || [])
const scaleSet = computed(() => new Set(scale.value))
const root = computed(() => scale.value[0] || '')

const board = computed(() => {
  const t = TUNINGS[tuning.value]
  return t.open.map((open, s) => ({
    label: t.labels[s],
    frets: Array.from({ length: FRETS }, (_, fr) => {
      const name = NOTE_NAMES[(open + fr) % 12]
      return { fret: fr, name, inScale: scaleSet.value.has(name), isRoot: name === root.value }
    }),
  })).reverse()
})
</script>

<template>
  <div class="lk">
    <header class="head">
      <div>
        <h1 class="title">Live key</h1>
        <p class="muted sub">
          Let the band play. Scriptum works out the key so you can jam over it.
        </p>
      </div>
      <button class="btn" :class="live.connected.value ? 'btn-stop' : 'btn-primary'" @click="toggle">
        <span class="rec" :class="{ on: live.connected.value }" />
        {{ live.connected.value ? 'Stop listening' : 'Start listening' }}
      </button>
    </header>

    <div v-if="noDevices" class="warn card">
      No microphone available to the server: <span class="mono">{{ noDevices }}</span>
    </div>
    <div v-if="live.error.value" class="err card">{{ live.error.value }}</div>

    <div class="card setup">
      <label class="opt">
        <span class="eyebrow">Input</span>
        <select v-model="device" :disabled="live.connected.value">
          <option value="">system default</option>
          <option v-for="d in devices" :key="d.index" :value="String(d.index)">{{ d.name }}</option>
        </select>
      </label>
      <label class="opt">
        <span class="eyebrow">Your instrument</span>
        <select v-model="tuning">
          <option value="guitar">Guitar</option>
          <option value="bass">Bass</option>
        </select>
      </label>
      <div class="opt">
        <span class="eyebrow">Input level</span>
        <LevelMeter :level="f?.level || 0" />
      </div>
    </div>

    <!-- the answer -->
    <section class="keycard card">
      <div class="kmain">
        <span class="eyebrow">Key</span>
        <div v-if="key" class="kname">{{ key }}</div>
        <div v-else class="kidle">
          {{ live.connected.value ? 'listening…' : 'press start, then let them play' }}
        </div>
      </div>
      <div class="kside">
        <div class="stat">
          <span class="sv mono">{{ f?.chord || '—' }}</span>
          <span class="eyebrow">chord now</span>
        </div>
        <div class="stat">
          <span class="sv mono">{{ f?.tempo ? Math.round(f.tempo) : '—' }}</span>
          <span class="eyebrow">bpm</span>
        </div>
      </div>
    </section>

    <div v-if="scale.length" class="card scale">
      <span class="eyebrow">Notes that work</span>
      <div class="srow">
        <span v-for="n in scale" :key="n" class="sn mono" :class="{ root: n === root }">{{ n }}</span>
      </div>
    </div>

    <div v-if="f?.chords?.length" class="card chords">
      <span class="eyebrow">What they have been playing</span>
      <div class="crow">
        <span
          v-for="(c, i) in f.chords" :key="i"
          class="ch mono" :class="{ last: i === f.chords.length - 1 }"
        >{{ c.name }}</span>
      </div>
    </div>

    <!-- where to put your fingers -->
    <section class="card fb">
      <header class="fbhead">
        <span class="eyebrow">Where that sits on the neck</span>
        <span class="legend">
          <span class="lg root" /> root
          <span class="lg inscale" /> in key
        </span>
      </header>

      <div class="neck">
        <div class="fretnums mono">
          <span class="lbl" />
          <span v-for="n in FRETS" :key="n" class="fn">{{ n - 1 }}</span>
        </div>
        <div v-for="(s, si) in board" :key="si" class="string">
          <span class="lbl mono">{{ s.label }}</span>
          <span
            v-for="fr in s.frets" :key="fr.fret"
            class="fret"
            :class="{ on: fr.inScale, root: fr.isRoot, nut: fr.fret === 0 }"
          >{{ fr.inScale ? fr.name : '' }}</span>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.lk { padding: 24px 30px 60px; display: flex; flex-direction: column; gap: 13px; max-width: 1200px; }

.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.title { font-family: var(--font-display); font-size: 29px; }
.sub { margin: 3px 0 0; font-size: 13.5px; }
.tiny { font-size: 11.5px; }

.btn-stop { background: var(--surface-3); border-color: var(--red); color: var(--text); }
.rec { width: 8px; height: 8px; border-radius: 50%; background: var(--line-strong); }
.rec.on { background: var(--red-glow); animation: pulse 1.1s infinite; box-shadow: 0 0 8px var(--red-glow); }

.setup { padding: 12px 15px; display: flex; flex-wrap: wrap; gap: 17px; align-items: flex-end; }
.opt { display: flex; flex-direction: column; gap: 4px; min-width: 150px; }

.keycard {
  padding: 26px 28px;
  display: flex; align-items: center; justify-content: space-between;
  gap: 22px; flex-wrap: wrap;
  background: linear-gradient(105deg, rgba(168, 23, 47, 0.18), var(--surface) 62%);
  border-color: var(--red-deep);
}
.kmain { display: flex; flex-direction: column; gap: 3px; }
.kname {
  font-family: var(--font-display);
  font-size: 54px; font-weight: 600; line-height: 1.05;
  background: linear-gradient(100deg, #fff, #f0a8b4 50%, var(--red-bright));
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
.kidle { font-size: 20px; color: var(--text-4); padding: 9px 0; }
.kside { display: flex; gap: 28px; }
.stat { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.sv { font-size: 24px; font-weight: 600; color: var(--gold); }

.scale, .chords { padding: 12px 15px; }
.srow, .crow { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.sn {
  font-size: 14px; font-weight: 600; padding: 4px 12px; border-radius: var(--r-sm);
  background: var(--surface-2); border: 1px solid var(--gold-dim); color: var(--gold);
}
.sn.root { background: var(--gold); color: #1a1108; border-color: var(--gold); }

.ch {
  font-size: 13px; padding: 3px 9px; border-radius: var(--r-sm);
  background: var(--surface-2); border: 1px solid var(--line-soft); color: var(--text-2);
}
.ch.last { background: var(--red); color: #fff; border-color: var(--red-bright); box-shadow: var(--glow-red); }

.fb { padding: 14px 16px 16px; }
.fbhead { display: flex; align-items: center; justify-content: space-between; margin-bottom: 11px; flex-wrap: wrap; gap: 9px; }
.legend { display: flex; align-items: center; gap: 7px; font-size: 11px; color: var(--text-4); }
.lg { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.lg.root { background: var(--gold); }
.lg.inscale { background: var(--red); margin-left: 8px; }

.neck { overflow-x: auto; }
.fretnums, .string { display: flex; gap: 2px; margin-bottom: 2px; min-width: 620px; }
.lbl { width: 20px; flex: none; font-size: 10.5px; color: var(--text-4); text-align: right; padding-right: 5px; line-height: 27px; }
.fn { flex: 1; text-align: center; font-size: 9.5px; color: var(--text-4); }

.fret {
  flex: 1; height: 27px;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono); font-size: 11px;
  background: var(--surface-2);
  border-radius: 3px;
  color: transparent;
  border: 1px solid transparent;
  transition: background 0.2s, color 0.2s;
}
.fret.nut { background: var(--surface-3); border-left: 2px solid var(--line-strong); }
.fret.on {
  background: color-mix(in srgb, var(--red) 42%, var(--surface-2));
  color: #fff;
  border-color: var(--red-deep);
}
.fret.root {
  background: var(--gold); color: #1a1108; font-weight: 700;
  border-color: var(--gold);
  box-shadow: 0 0 10px rgba(217, 164, 65, 0.4);
}

.warn { padding: 11px 15px; border-color: var(--gold-dim); background: rgba(217, 164, 65, 0.09); color: var(--gold); }
.err { padding: 11px 15px; color: #f0a8b4; border-color: var(--red-deep); }
</style>
