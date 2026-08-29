<script setup>
import { ref, onMounted, provide, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import { api, followJob, mmss } from '../api'
import { chosenTranscriber, settings, transcriberLabel, useTranscribers } from '../composables/useSettings'
import TrackPanel from '../components/TrackPanel.vue'

const props = defineProps({ id: { type: String, required: true } })
const route = useRoute()

const song = ref(null)
const loading = ref(true)
const err = ref('')
const job = ref(null)

// Every child view reads the same loaded song rather than fetching it again -
// the payload carries the whole analysis and is not small.
provide('song', song)
provide('reloadSong', load)

async function load() {
  try {
    song.value = await api.song(props.id)
    err.value = ''
    if (song.value.job && song.value.job.state === 'running') watchJob(song.value.job)
  } catch (e) {
    err.value = e.detail?.error === 'not_analyzed' ? 'not_analyzed' : e.message
  } finally { loading.value = false }
}

function watchJob(j) {
  job.value = { ...j }
  followJob(j.id, {
    onLine: (line) => { job.value = { ...job.value, lines: [...job.value.lines, line] } },
    onEnd: (snap) => { job.value = snap; load(); if (snap.state === 'done') setTimeout(() => { job.value = null }, 2000) },
  })
}

useTranscribers()

async function analyze() {
  try {
    watchJob(await api.analyze(props.id,
                               { llm: settings.llmNotes, backend: chosenTranscriber() }))
  } catch (e) { err.value = e.message }
}

// Re-reading the notes is the cheap half of the pipeline - the stems, chords
// and form stay as they are - so switching engine is its own action rather
// than a reason to sit through a whole re-analysis.
async function retranscribe() {
  try {
    watchJob(await api.transcribe(props.id, { backend: chosenTranscriber() }))
  } catch (e) { err.value = e.message }
}

watch(() => props.id, () => { loading.value = true; load() })
onMounted(load)

const TABS = [
  { name: 'structure', label: 'Structure' },
  { name: 'tabs', label: 'Tabs & Notes' },
  { name: 'play', label: 'Play along' },
  { name: 'lyrics', label: 'Lyrics' },
  { name: 'chart', label: 'Chart' },
  { name: 'solo', label: 'Solo' },
]

const a = computed(() => song.value?.analysis)
const busy = computed(() => job.value?.state === 'running')

// Which engine the cached notes came from, and which one the settings pane
// now asks for. They differ after a settings change (and on any song analysed
// before the engine was a choice), which is exactly when the offer to
// re-transcribe is worth making.
const wanted = computed(() => chosenTranscriber())
const engines = computed(() => {
  const by = song.value?.note_backends || {}
  const out = new Map()
  for (const [stem, name] of Object.entries(by)) {
    if (!out.has(name)) out.set(name, [])
    out.get(name).push(stem)
  }
  return [...out].map(([name, stems]) => ({ name, stems, label: transcriberLabel(name) }))
})
const stale = computed(() =>
  wanted.value && engines.value.some((e) => e.name !== wanted.value))

// On an imported multitrack a stem is one of the band's own tracks, and which
// one is not always guessable from the name - `guitar-2` could be either
// guitarist. `sources.json` remembers, so the chip can say.
function trackFor(stem) {
  const t = (song.value?.sources?.tracks || []).find((t) => t.stem === stem)
  return t ? `${t.name} (${t.file})` : ''
}

// ...and remembering is also what makes it correctable. A name that says the
// wrong instrument is not always visible at import time - a vocal track read
// as a guitar looks like an ordinary mapping until the Lyrics tab comes back
// empty - so the same choice is offered here, over the finished analysis.
function reassigned(j) {
  watchJob(j)
  err.value = ''
}
</script>

<template>
  <div class="wrap">
    <div v-if="loading" class="muted pad">Loading…</div>

    <template v-else-if="song">
      <header class="head">
        <div class="ident">
          <h1 class="title">{{ song.title }}</h1>
          <div v-if="a" class="facts">
            <span class="chip chip-gold">{{ a.key }}</span>
            <span class="chip">{{ Math.round(a.tempo) }} bpm</span>
            <span class="chip">{{ a.beats_per_bar }}/4</span>
            <span class="chip">{{ mmss(a.duration) }}</span>
            <span v-if="song.form" class="chip">{{ song.form.parts.length }} parts</span>
            <span v-for="s in song.stems" :key="s" class="chip stem"
                  :title="trackFor(s)">{{ s }}</span>
            <span v-if="song.sources" class="chip chip-gold">
              imported · {{ song.sources.kind === 'garageband' ? 'GarageBand' : 'stems' }}
            </span>
          </div>
        </div>
        <div class="headact">
          <TrackPanel
            v-if="song.sources" :id="id" :sources="song.sources" :busy="busy"
            @started="reassigned" @error="err = $event"
          />
          <button class="btn btn-sm" :disabled="busy" @click="analyze">
            {{ busy ? 'Analysing…' : 'Re-analyse' }}
          </button>
        </div>
      </header>

      <div v-if="job" class="job card">
        <div class="jobline">
          <span v-if="busy" class="spinner spin" />
          <span v-else-if="job.state === 'error'" class="bad">✕</span>
          <span v-else class="good">✓</span>
          <span class="mono jobtext">{{ job.lines[job.lines.length - 1] || 'starting…' }}</span>
        </div>
        <div v-if="job.error" class="joberr mono">{{ job.error }}</div>
      </div>

      <div v-if="!song.analyzed" class="notyet card">
        <h3>Not analysed yet</h3>
        <p v-if="song.sources" class="muted">
          The stems came in from your DAW, so there is nothing to separate -
          just chords, form and notes to read off them. Everything after that
          is instant.
        </p>
        <p v-else class="muted">
          Scriptum needs one slow pass to separate the stems and transcribe them.
          Everything after that is instant.
        </p>
        <button class="btn btn-primary" :disabled="busy" @click="analyze">
          {{ busy ? 'Analysing…' : 'Analyse this song' }}
        </button>
      </div>

      <template v-else>
        <div class="engines" v-if="engines.length">
          <span class="eyebrow">Notes read with</span>
          <span v-for="e in engines" :key="e.name" class="chip"
                :class="{ 'chip-gold': e.name === wanted }">
            {{ e.label }}
            <span class="dim">· {{ e.stems.join(', ') }}</span>
          </span>
          <button
            v-if="stale" class="btn btn-sm" :disabled="busy" @click="retranscribe">
            {{ busy ? 'Working…' : `Re-transcribe with ${transcriberLabel(wanted)}` }}
          </button>
          <RouterLink to="/settings" class="setlink">Change engine</RouterLink>
        </div>

        <nav class="tabs">
          <RouterLink
            v-for="t in TABS" :key="t.name"
            :to="{ name: t.name, params: { id } }"
            class="tab"
            :class="{ on: route.name === t.name }"
          >{{ t.label }}</RouterLink>
        </nav>
        <RouterView />
      </template>
    </template>

    <div v-else class="err card">{{ err }}</div>
  </div>
</template>

<style scoped>
.wrap { padding: 22px 30px 60px; max-width: 1500px; }
.pad { padding: 22px 0; }

.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.title { font-family: var(--font-display); font-size: 29px; text-transform: capitalize; }
.facts { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.headact { display: flex; align-items: center; gap: 7px; flex: none; }
.stem { text-transform: capitalize; }

.job { padding: 9px 13px; margin: 13px 0; background: var(--bg-deep); }
.jobline { display: flex; align-items: center; gap: 9px; }
.jobtext { font-size: 12px; color: var(--text-2); }
.joberr { font-size: 11.5px; color: var(--err); margin-top: 5px; }
.spinner {
  width: 11px; height: 11px; flex: none;
  border: 2px solid var(--line-strong); border-top-color: var(--red-bright); border-radius: 50%;
}
.good { color: var(--ok); } .bad { color: var(--err); }

.notyet { padding: 30px; text-align: center; margin-top: 22px; }
.notyet h3 { font-family: var(--font-display); font-size: 20px; margin-bottom: 7px; }
.notyet p { max-width: 460px; margin: 0 auto 16px; }

.engines {
  display: flex; align-items: center; flex-wrap: wrap; gap: 7px;
  margin-top: 18px;
}
.engines .eyebrow { padding-right: 2px; }
.engines .dim { color: var(--text-4); }
.setlink { font-size: 12px; color: var(--text-3); }

.tabs {
  display: flex; gap: 2px;
  border-bottom: 1px solid var(--line-soft);
  margin: 14px 0 20px;
  overflow-x: auto;
}
.tab {
  padding: 9px 15px;
  color: var(--text-3);
  text-decoration: none;
  font-size: 13.5px;
  border-bottom: 2px solid transparent;
  white-space: nowrap;
  transition: color 0.14s, border-color 0.14s;
}
.tab:hover { color: var(--text); text-decoration: none; }
.tab.on { color: var(--text); border-bottom-color: var(--red-bright); font-weight: 600; }

.err { padding: 14px; color: #f0a8b4; border-color: var(--red-deep); }
</style>
