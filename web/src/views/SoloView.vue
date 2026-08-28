<script setup>
/** Ask Gemini for a solo over a passage, then read and hear what it wrote. */
import { inject, ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { api, followJob, mmss, stemMeta } from '../api'
import TabGrid from '../components/TabGrid.vue'
import { useTransport } from '../composables/useTransport'

const props = defineProps({ id: { type: String, required: true } })
const song = inject('song')
const health = inject('health')
const route = useRoute()
const transport = useTransport()

const prompt = ref('')
const partName = ref(route.query.part || '')
const instrument = ref('guitar')
const over = ref('backing')
const temperature = ref(1.0)
const bedGain = ref(0.55)

const running = ref(false)
const lines = ref([])
const result = ref(null)
const err = ref('')
const zoom = ref(18)

const parts = computed(() => song.value?.form?.parts || [])
const IDEAS = [
  'slow and bluesy, lots of bends, build to a scream',
  'fast legato runs, dorian, tight to the changes',
  'melodic and singable — four-bar phrases, plenty of space',
  'aggressive, syncopated, pentatonic with a flat five',
]

async function generate() {
  if (!prompt.value.trim()) return
  running.value = true
  lines.value = []
  result.value = null
  err.value = ''
  try {
    const job = await api.solo(props.id, {
      prompt: prompt.value,
      part: partName.value || undefined,
      instrument: instrument.value,
      over: over.value,
      temperature: temperature.value,
      bed_gain: bedGain.value,
    })
    followJob(job.id, {
      onLine: (l) => lines.value.push(l),
      onEnd: (snap) => {
        running.value = false
        if (snap.state === 'done') {
          result.value = snap.result
          transport.tempo.value = song.value?.analysis?.tempo || 120
          transport.load(snap.result.audio)
        } else err.value = snap.error || 'generation failed'
      },
      onError: () => { running.value = false; err.value = 'lost the progress stream' },
    })
  } catch (e) { running.value = false; err.value = e.message }
}

onMounted(() => {
  if (!partName.value && parts.value.length) {
    const solo = parts.value.find((p) => /solo/i.test(p.role))
    if (solo) partName.value = solo.name
  }
})
</script>

<template>
  <div class="sv">
    <div v-if="!health.gemini" class="warn card">
      No Gemini key found. Export <code class="mono">GEMINI_API_KEY</code> and restart the
      server to use this.
    </div>

    <section class="card ask">
      <h3 class="h">Write a solo</h3>
      <p class="muted sub">
        Gemini gets the key, tempo and chord changes of the passage you pick, then writes
        over them. You hear it against the real backing track.
      </p>

      <textarea
        v-model="prompt" rows="2" class="prompt"
        placeholder="How should it sound? e.g. slow and bluesy, lots of bends…"
        @keydown.meta.enter="generate" @keydown.ctrl.enter="generate"
      />
      <div class="ideas">
        <button v-for="i in IDEAS" :key="i" class="idea" @click="prompt = i">{{ i }}</button>
      </div>

      <div class="opts">
        <label class="opt">
          <span class="eyebrow">Over which part</span>
          <select v-model="partName">
            <option value="">the detected solo section</option>
            <option v-for="p in parts" :key="p.name" :value="p.name">{{ p.name }}</option>
          </select>
        </label>
        <label class="opt">
          <span class="eyebrow">Instrument</span>
          <select v-model="instrument">
            <option value="guitar">Guitar</option>
            <option value="bass">Bass</option>
            <option value="piano">Piano</option>
          </select>
        </label>
        <label class="opt">
          <span class="eyebrow">Play it over</span>
          <select v-model="over">
            <option value="backing">the real backing track</option>
            <option value="chords">synthesised chords</option>
            <option value="none">nothing</option>
          </select>
        </label>
        <label class="opt">
          <span class="eyebrow">Adventurousness {{ temperature.toFixed(1) }}</span>
          <input v-model.number="temperature" type="range" min="0.2" max="1.8" step="0.1" />
        </label>
      </div>

      <button class="btn btn-primary go" :disabled="running || !prompt.trim() || !health.gemini" @click="generate">
        {{ running ? 'Writing…' : 'Write it' }}
      </button>
    </section>

    <div v-if="running || lines.length" class="card prog">
      <div v-for="(l, i) in lines" :key="i" class="pl mono">{{ l }}</div>
      <div v-if="running" class="pl mono dim">…</div>
    </div>

    <div v-if="err" class="err card">{{ err }}</div>

    <section v-if="result" class="card out">
      <header class="ohead">
        <div>
          <h3 class="ot">{{ result.title }}</h3>
          <div class="dim mono tiny">
            {{ result.scale }} · {{ mmss(result.start) }}–{{ mmss(result.end) }} ·
            {{ result.notes.length }} notes
          </div>
        </div>
        <div class="oacts">
          <button class="playbtn" @click="transport.toggle()">
            {{ transport.playing.value ? '❚❚' : '▶' }}
          </button>
          <a class="btn btn-sm" :href="result.audio" download>wav</a>
          <a class="btn btn-sm" :href="result.midi" download>midi</a>
        </div>
      </header>

      <p class="expl">{{ result.explanation }}</p>

      <div class="scrub">
        <input
          type="range" min="0" :max="transport.duration.value || 1" step="0.01"
          :value="transport.time.value"
          @input="transport.seek(+$event.target.value)"
        />
        <span class="mono dim tiny">{{ mmss(transport.time.value) }}</span>
      </div>

      <TabGrid
        v-if="result.layout"
        :layout="result.layout"
        :cursor-time="result.start + transport.time.value"
        :col-width="zoom"
        follow
      />
    </section>
  </div>
</template>

<style scoped>
.sv { display: flex; flex-direction: column; gap: 13px; max-width: 1180px; }

.warn { padding: 11px 15px; border-color: var(--gold-dim); background: rgba(217, 164, 65, 0.09); color: var(--gold); }
.warn code { background: var(--surface-3); padding: 1px 5px; border-radius: 3px; }

.ask { padding: 19px 21px; }
.h { font-family: var(--font-display); font-size: 20px; }
.sub { font-size: 13px; margin: 4px 0 13px; max-width: 620px; }

.prompt { resize: vertical; font-family: var(--font-ui); }
.ideas { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.idea {
  font-size: 11.5px; padding: 3px 9px; border-radius: 999px;
  border: 1px solid var(--line); background: var(--surface-2);
  color: var(--text-3); cursor: pointer; transition: all 0.13s;
}
.idea:hover { border-color: var(--red); color: var(--text); }

.opts { display: flex; flex-wrap: wrap; gap: 15px; margin: 16px 0 15px; }
.opt { display: flex; flex-direction: column; gap: 4px; min-width: 150px; }
.opt select, .opt input { width: 100%; }

.go { min-width: 130px; }

.prog { padding: 11px 15px; background: var(--bg-deep); max-height: 150px; overflow-y: auto; }
.pl { font-size: 11.5px; color: var(--text-2); }

.out { padding: 17px 19px 10px; }
.ohead { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
.ot { font-family: var(--font-display); font-size: 19px; color: var(--red-glow); }
.tiny { font-size: 11px; }
.oacts { display: flex; align-items: center; gap: 7px; }

.playbtn {
  width: 34px; height: 34px; border-radius: 50%;
  border: 1px solid #7d1128;
  background: linear-gradient(180deg, var(--red-bright), var(--red));
  color: #fff; cursor: pointer; font-size: 11px;
}
.playbtn:hover { box-shadow: var(--glow-red); }

.expl { font-size: 13.5px; color: var(--text-2); margin: 11px 0 13px; max-width: 720px; }
.scrub { display: flex; align-items: center; gap: 10px; margin-bottom: 11px; }
.scrub input { flex: 1; }

.err { padding: 12px 15px; color: #f0a8b4; border-color: var(--red-deep); }
</style>
