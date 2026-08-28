<script setup>
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api, followJob, bytes } from '../api'

const router = useRouter()
const songs = ref([])
const loading = ref(true)
const err = ref('')
const dragging = ref(false)
const uploading = ref(null)      // { name, progress }
const fileInput = ref(null)

// jobs keyed by song id -> { id, lines[], state }
const jobs = ref({})
const stoppers = []

async function refresh() {
  try {
    songs.value = await api.library()
    err.value = ''
  } catch (e) { err.value = e.message } finally { loading.value = false }
}

/** Re-attach to anything still analysing, so a reload does not lose progress. */
async function attach() {
  try {
    for (const j of await api.jobs()) {
      if (j.kind === 'analyze') watchJob(j.song, j)
    }
  } catch { /* no jobs endpoint yet is not an error worth showing */ }
}

function watchJob(songId, job) {
  jobs.value = { ...jobs.value, [songId]: { ...job } }
  stoppers.push(followJob(job.id, {
    onLine: (line) => {
      const j = jobs.value[songId]
      if (j) jobs.value = { ...jobs.value, [songId]: { ...j, lines: [...j.lines, line] } }
    },
    onEnd: (snap) => {
      jobs.value = { ...jobs.value, [songId]: { ...snap } }
      refresh()
      if (snap.state === 'done') {
        setTimeout(() => {
          const { [songId]: _drop, ...rest } = jobs.value
          jobs.value = rest
        }, 2500)
      }
    },
    onError: () => refresh(),
  }))
}

async function analyze(song, opts = {}) {
  try {
    const job = await api.analyze(song.id, { llm: true, ...opts })
    watchJob(song.id, job)
  } catch (e) { err.value = e.message }
}

async function upload(file) {
  if (!file) return
  uploading.value = { name: file.name, progress: 0 }
  try {
    const entry = await api.upload(file, (p) => { uploading.value.progress = p })
    await refresh()
    analyze(entry)
  } catch (e) { err.value = e.message } finally { uploading.value = null }
}

function onDrop(e) {
  dragging.value = false
  const file = e.dataTransfer?.files?.[0]
  if (file) upload(file)
}

async function remove(song) {
  if (!confirm(`Delete "${song.title}" and its analysis?`)) return
  try { await api.remove(song.id); await refresh() } catch (e) { err.value = e.message }
}

const STAGES = [
  ['stems', 'stems'], ['analysis', 'chords'], ['notes', 'notes'],
  ['lyrics', 'lyrics'], ['form', 'form'], ['snippets', 'snippets'],
]

onMounted(() => { refresh(); attach() })
onBeforeUnmount(() => stoppers.forEach((s) => s()))

const empty = computed(() => !loading.value && !songs.value.length)
</script>

<template>
  <div class="wrap">
    <header class="head">
      <div>
        <h1 class="title">Library</h1>
        <p class="muted sub">Drop a song in, and Scriptum works out what it is made of.</p>
      </div>
      <button class="btn btn-primary" @click="fileInput.click()">
        <span>＋</span> Add song
      </button>
      <input
        ref="fileInput" type="file" hidden
        accept=".mp3,.wav,.flac,.m4a,.aac,.ogg,.opus,.aiff,.aif"
        @change="upload($event.target.files[0]); $event.target.value = ''"
      />
    </header>

    <div v-if="err" class="err card">{{ err }}</div>

    <div
      class="drop"
      :class="{ over: dragging }"
      @dragover.prevent="dragging = true"
      @dragleave="dragging = false"
      @drop.prevent="onDrop"
    >
      <template v-if="uploading">
        <div class="uploading">
          <div class="upname mono">{{ uploading.name }}</div>
          <div class="bar"><div class="fill" :style="{ width: uploading.progress * 100 + '%' }" /></div>
        </div>
      </template>
      <template v-else>
        <div class="dropinner">
          <span class="dropicon">♫</span>
          <span>Drop an mp3, wav or flac here</span>
        </div>
      </template>
    </div>

    <div v-if="loading" class="muted pad">Reading the library…</div>

    <div v-else-if="empty" class="empty card">
      <h3>Nothing here yet</h3>
      <p class="muted">
        Add a song and Scriptum will separate the stems, find the chords, the form,
        the lyrics and the notes for every instrument.
      </p>
    </div>

    <div v-else class="grid">
      <article v-for="s in songs" :key="s.id" class="song card">
        <div class="songmain" @click="router.push(`/song/${s.id}`)">
          <div class="songhead">
            <h3 class="songtitle">{{ s.title }}</h3>
            <span v-if="s.analyzed" class="chip chip-gold">analysed</span>
            <span v-else class="chip">raw</span>
          </div>
          <div class="meta dim mono">{{ s.filename }} · {{ bytes(s.size) }}</div>

          <div class="stages">
            <span
              v-for="[key, label] in STAGES"
              :key="key"
              class="stage"
              :class="{ done: s.stages[key] }"
              :title="label"
            >{{ label }}</span>
          </div>
        </div>

        <div v-if="jobs[s.id]" class="job">
          <div class="jobline">
            <span v-if="jobs[s.id].state === 'running'" class="spinner spin" />
            <span v-else-if="jobs[s.id].state === 'error'" class="bad">✕</span>
            <span v-else class="good">✓</span>
            <span class="jobtext mono">
              {{ jobs[s.id].lines[jobs[s.id].lines.length - 1] || 'starting…' }}
            </span>
          </div>
          <div v-if="jobs[s.id].error" class="joberr">{{ jobs[s.id].error }}</div>
        </div>

        <footer class="songfoot">
          <button
            class="btn btn-sm"
            :disabled="jobs[s.id]?.state === 'running'"
            @click.stop="analyze(s)"
          >
            {{ s.analyzed ? 'Re-analyse' : 'Analyse' }}
          </button>
          <RouterLink v-if="s.analyzed" :to="`/song/${s.id}`" class="btn btn-sm btn-primary">Open</RouterLink>
          <button class="btn btn-sm btn-ghost del" @click.stop="remove(s)">Delete</button>
        </footer>
      </article>
    </div>
  </div>
</template>

<style scoped>
.wrap { padding: 26px 30px 60px; max-width: 1180px; }

.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.title { font-family: var(--font-display); font-size: 30px; }
.sub { margin: 3px 0 0; font-size: 13.5px; }

.err {
  padding: 10px 14px; margin-bottom: 14px;
  border-color: var(--red-deep); color: #f0a8b4;
  background: rgba(168, 23, 47, 0.12);
}

.drop {
  border: 1px dashed var(--line-strong);
  border-radius: var(--r);
  padding: 20px;
  text-align: center;
  color: var(--text-3);
  margin-bottom: 22px;
  transition: border-color 0.15s, background 0.15s, color 0.15s;
}
.drop.over { border-color: var(--red-bright); background: rgba(168, 23, 47, 0.09); color: var(--text); }
.dropinner { display: flex; align-items: center; justify-content: center; gap: 10px; font-size: 13.5px; }
.dropicon { font-size: 17px; color: var(--red-bright); }

.uploading { display: flex; flex-direction: column; gap: 8px; }
.upname { font-size: 12.5px; color: var(--text-2); }
.bar { height: 4px; background: var(--surface-3); border-radius: 2px; overflow: hidden; }
.fill { height: 100%; background: linear-gradient(90deg, var(--red), var(--red-glow)); transition: width 0.15s; }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 14px; }

.song { display: flex; flex-direction: column; overflow: hidden; transition: border-color 0.15s, transform 0.12s; }
.song:hover { border-color: var(--line-strong); transform: translateY(-1px); }

.songmain { padding: 15px 16px 12px; cursor: pointer; flex: 1; }
.songhead { display: flex; align-items: center; gap: 8px; margin-bottom: 3px; }
.songtitle {
  font-family: var(--font-display);
  font-size: 18px;
  text-transform: capitalize;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.meta { font-size: 11.5px; }

.stages { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 11px; }
.stage {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 3px;
  background: var(--surface-2);
  color: var(--text-4);
  border: 1px solid var(--line-soft);
}
.stage.done { color: var(--gold); border-color: var(--gold-dim); background: rgba(217, 164, 65, 0.09); }

.job { padding: 9px 16px; background: var(--bg-deep); border-top: 1px solid var(--line-soft); }
.jobline { display: flex; align-items: center; gap: 8px; }
.jobtext { font-size: 11.5px; color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.joberr { font-size: 11.5px; color: var(--err); margin-top: 4px; }
.spinner {
  width: 11px; height: 11px; flex: none;
  border: 2px solid var(--line-strong);
  border-top-color: var(--red-bright);
  border-radius: 50%;
}
.good { color: var(--ok); } .bad { color: var(--err); }

.songfoot { display: flex; gap: 7px; padding: 11px 16px; border-top: 1px solid var(--line-soft); }
.del { margin-left: auto; }
.del:hover { color: var(--err) !important; }

.empty { padding: 34px; text-align: center; }
.empty h3 { font-family: var(--font-display); font-size: 19px; margin-bottom: 6px; }
.pad { padding: 20px 0; }
</style>
