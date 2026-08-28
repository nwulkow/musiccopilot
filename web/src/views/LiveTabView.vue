<script setup>
/**
 * "What is he playing?" - point the mic at whoever is jamming and read the
 * tab as it comes out.
 *
 * The whole analysis is the same one `musiccopilot record` runs; this view
 * only draws the frames. Committed notes lag the present by design (a note
 * is only settled once a later block cannot change it), so the big readout
 * is the *now-playing* pitch, which is read straight off the pitch contour
 * and does not wait for a note to settle.
 */
import { ref, computed, onMounted } from 'vue'
import { api, stemMeta } from '../api'
import { useLive } from '../composables/useLive'
import TabGrid from '../components/TabGrid.vue'
import LevelMeter from '../components/LevelMeter.vue'

const live = useLive()
const devices = ref([])
const device = ref('')
const instrument = ref('bass')
const zoom = ref(19)
const noDevices = ref('')
const takeName = ref('')

const INSTRUMENTS = ['guitar', 'bass', 'piano', 'vocals', 'other']

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
  else live.start({ mode: 'tab', instrument: instrument.value, device: device.value, fps: 8 })
}

const f = computed(() => live.frame.value)
const layout = computed(() => f.value?.layout || null)
const notes = computed(() => f.value?.notes || [])
const recent = computed(() => notes.value.slice(-14).reverse())
const meta = computed(() => stemMeta(instrument.value))
</script>

<template>
  <div class="lt">
    <header class="head">
      <div>
        <h1 class="title">Live tab</h1>
        <p class="muted sub">
          Point the mic at the room. Scriptum listens and writes down what it hears.
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
        <span class="eyebrow">Instrument</span>
        <select v-model="instrument" :disabled="live.connected.value">
          <option v-for="i in INSTRUMENTS" :key="i" :value="i">{{ stemMeta(i).label }}</option>
        </select>
      </label>
      <label class="opt">
        <span class="eyebrow">Input</span>
        <select v-model="device" :disabled="live.connected.value">
          <option value="">system default</option>
          <option v-for="d in devices" :key="d.index" :value="String(d.index)">{{ d.name }}</option>
        </select>
      </label>
      <label class="opt">
        <span class="eyebrow">Zoom</span>
        <input v-model.number="zoom" type="range" min="10" max="36" />
      </label>
      <div class="opt meterbox">
        <span class="eyebrow">Input level</span>
        <LevelMeter :level="f?.level || 0" />
      </div>
    </div>

    <!-- the big readout: what is sounding right now -->
    <section class="now card" :style="{ '--c': meta.color }">
      <div class="nowmain">
        <div class="pitch mono" :class="{ idle: !f?.pitch_name }">
          {{ f?.pitch_name || (live.connected.value ? 'listening' : 'ready') }}
        </div>
        <div class="nowmeta">
          <span class="eyebrow">Sounding now</span>
          <div class="dim mono tiny">
            {{ f?.pitch ? f.pitch.toFixed(1) + ' MIDI' : 'nothing detected' }}
          </div>
        </div>
      </div>
      <div class="nowside">
        <div class="stat">
          <span class="sv mono">{{ f?.tempo ? Math.round(f.tempo) : '—' }}</span>
          <span class="eyebrow">bpm</span>
        </div>
        <div class="stat">
          <span class="sv mono">{{ notes.length }}</span>
          <span class="eyebrow">notes</span>
        </div>
        <div class="stat">
          <span class="sv mono">{{ f?.t ? f.t.toFixed(0) + 's' : '—' }}</span>
          <span class="eyebrow">recorded</span>
        </div>
      </div>
    </section>

    <div v-if="f?.chords?.length" class="chords card">
      <span class="eyebrow">Chords heard</span>
      <div class="crow">
        <span
          v-for="(c, i) in f.chords" :key="i"
          class="ch mono" :class="{ last: i === f.chords.length - 1 }"
        >{{ c.name }}</span>
      </div>
    </div>

    <section class="card sheet" :style="{ '--c': meta.color }">
      <header class="shead">
        <h3 class="sh">{{ meta.label }} — last few seconds</h3>
        <span v-if="!meta.fretted" class="chip">staff · no fretboard for this instrument</span>
      </header>
      <TabGrid v-if="layout" :layout="layout" :col-width="zoom" :show-bars="false" />
      <div v-else class="empty dim">
        {{ live.connected.value ? 'Listening…' : 'Press start, then play something.' }}
      </div>
    </section>

    <section v-if="recent.length" class="card recent">
      <span class="eyebrow">Note history</span>
      <div class="rrow">
        <span v-for="(n, i) in recent" :key="i" class="rn mono" :class="`tech-${n.technique}`">
          {{ n.name }}<sub v-if="n.technique !== 'normal'">{{ n.technique[0] }}</sub>
        </span>
      </div>
    </section>

    <div v-if="live.connected.value" class="card savebar">
      <input v-model="takeName" type="text" placeholder="name this take (optional)" class="tn" />
      <button class="btn btn-sm" @click="live.save(takeName)">Save the take</button>
      <span v-if="live.saved.value?.saved" class="chip chip-gold">
        saved {{ live.saved.value.notes }} notes → {{ live.saved.value.dir }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.lt { padding: 24px 30px 60px; display: flex; flex-direction: column; gap: 13px; max-width: 1400px; }

.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.title { font-family: var(--font-display); font-size: 29px; }
.sub { margin: 3px 0 0; font-size: 13.5px; }

.btn-stop { background: var(--surface-3); border-color: var(--red); color: var(--text); }
.rec { width: 8px; height: 8px; border-radius: 50%; background: var(--line-strong); }
.rec.on { background: var(--red-glow); animation: pulse 1.1s infinite; box-shadow: 0 0 8px var(--red-glow); }

.setup { padding: 12px 15px; display: flex; flex-wrap: wrap; gap: 17px; align-items: flex-end; }
.opt { display: flex; flex-direction: column; gap: 4px; min-width: 140px; }
.meterbox { min-width: 110px; }

.now {
  padding: 18px 22px;
  display: flex; align-items: center; justify-content: space-between;
  gap: 20px; flex-wrap: wrap;
  border-left: 3px solid var(--c);
  background: linear-gradient(100deg, color-mix(in srgb, var(--c) 12%, var(--surface)), var(--surface));
}
.nowmain { display: flex; align-items: center; gap: 18px; }
.pitch {
  font-size: 52px; font-weight: 700; line-height: 1;
  color: var(--c);
  min-width: 130px;
  text-shadow: 0 0 26px color-mix(in srgb, var(--c) 45%, transparent);
}
/* Nothing sounding is a normal state, not a missing value - say so in words
   rather than leaving a giant dash that reads as a broken glyph. */
.pitch.idle {
  font-size: 21px; font-weight: 500;
  color: var(--text-4); text-shadow: none;
  font-family: var(--font-ui);
}
.nowmeta { display: flex; flex-direction: column; gap: 3px; }
.tiny { font-size: 11px; }

.nowside { display: flex; gap: 24px; }
.stat { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.sv { font-size: 21px; font-weight: 600; color: var(--text); }

.chords { padding: 11px 15px; }
.crow { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.ch {
  font-size: 13px; padding: 3px 9px; border-radius: var(--r-sm);
  background: var(--surface-2); border: 1px solid var(--line-soft); color: var(--gold);
}
.ch.last { background: var(--red); color: #fff; border-color: var(--red-bright); box-shadow: var(--glow-red); }

.sheet { padding: 13px 16px 9px; border-left: 3px solid var(--c); }
.shead { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 9px; }
.sh { font-family: var(--font-display); font-size: 16px; color: var(--c); }
.empty { padding: 34px; text-align: center; font-size: 13.5px; }

.recent { padding: 11px 15px; }
.rrow { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.rn {
  font-size: 12px; padding: 2px 7px; border-radius: 3px;
  background: var(--surface-2); border: 1px solid var(--line-soft); color: var(--text-2);
}
.rn sub { font-size: 8px; opacity: 0.75; }
.tech-bend { color: var(--ember); border-color: var(--red-deep); }
.tech-vibrato { color: #eab04a; }
.tech-slide { color: #9fc6e8; }
.tech-hammer, .tech-pull { color: #8fd0a8; }

.savebar { padding: 10px 15px; display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.tn { max-width: 240px; }

.warn { padding: 11px 15px; border-color: var(--gold-dim); background: rgba(217, 164, 65, 0.09); color: var(--gold); }
.err { padding: 11px 15px; color: #f0a8b4; border-color: var(--red-deep); }
</style>
