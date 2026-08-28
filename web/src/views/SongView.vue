<script setup>
import { ref, onMounted, provide, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import { api, followJob, mmss } from '../api'

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

async function analyze() {
  try { watchJob(await api.analyze(props.id, { llm: true })) } catch (e) { err.value = e.message }
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
            <span v-for="s in song.stems" :key="s" class="chip stem">{{ s }}</span>
          </div>
        </div>
        <button class="btn btn-sm" :disabled="busy" @click="analyze">
          {{ busy ? 'Analysing…' : 'Re-analyse' }}
        </button>
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
        <p class="muted">
          Scriptum needs one slow pass to separate the stems and transcribe them.
          Everything after that is instant.
        </p>
        <button class="btn btn-primary" :disabled="busy" @click="analyze">
          {{ busy ? 'Analysing…' : 'Analyse this song' }}
        </button>
      </div>

      <template v-else>
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

.tabs {
  display: flex; gap: 2px;
  border-bottom: 1px solid var(--line-soft);
  margin: 20px 0 20px;
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
