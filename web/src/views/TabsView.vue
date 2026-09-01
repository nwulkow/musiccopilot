<script setup>
import { inject, ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, followJob, stemMeta, voiceNote } from '../api'
import TabGrid from '../components/TabGrid.vue'
import ScoreSheet from '../components/ScoreSheet.vue'

const props = defineProps({ id: { type: String, required: true } })
const song = inject('song')
const route = useRoute()
const router = useRouter()
const health = inject('health')

const stem = ref(route.query.stem || 'guitar')
const partName = ref(route.query.part || '')
const bars = ref(route.query.bars || '')
const subdiv = ref(4)
// One stem often holds a riff and a strummed chord at once. This picks which
// of them to read; the server does the splitting (`cli._voice`), so the
// browser and the terminal cannot disagree about what "melody" means.
const voice = ref(route.query.voice || 'all')
const zoom = ref(17)
const layout = ref(null)
const loading = ref(false)
const err = ref('')
const cleaning = ref(false)
const cleanInfo = ref(null)
const cleanNote = ref('')
const view = ref(route.query.view || '')   // '' = the natural one for this stem

const stems = computed(() => Object.keys(song.value?.note_stems || {}))
const parts = computed(() => song.value?.form?.parts || [])
const meta = computed(() => stemMeta(stem.value))

/** Tab where there is a fretboard, engraved notation where there is not. */
const shown = computed(() => view.value || (meta.value.fretted ? 'grid' : 'sheet'))

/**
 * The window currently on screen, in the shape both endpoints expect.
 *
 * "Whole song" says so explicitly, because `cli._window` reads an unbounded
 * request as the first twenty seconds - the right default for a terminal
 * printing a tab, and a passage that silently stops after twenty seconds here.
 */
function windowParams() {
  const p = { stem: stem.value, subdiv: subdiv.value }
  if (voice.value !== 'all') p.voice = voice.value
  if (partName.value) p.part = partName.value
  else if (bars.value) p.bars = bars.value
  else { p.start = 0; p.end = song.value?.analysis?.duration ?? 0 }
  return p
}

async function load() {
  loading.value = true
  err.value = ''
  cleanInfo.value = null
  try {
    const fetcher = shown.value === 'sheet' ? api.score : api.tab
    layout.value = await fetcher(props.id, windowParams())
  } catch (e) {
    err.value = e.detail?.error === 'no_notes'
      ? `No notes transcribed for ${stem.value}.`
      : e.message
    layout.value = null
  } finally { loading.value = false }
}

/**
 * Cleanup runs as a job rather than inline: a 75-note solo window measured
 * ~50s, which is past what a browser will reliably hold a request open for,
 * and a longer passage is worse. As a job it also has somewhere to report
 * progress instead of the button just sitting there.
 */
function llmClean() {
  cleaning.value = true
  cleanNote.value = 'asking Gemini…'
  api.cleanTab(props.id, windowParams())
    .then((job) => followJob(job.id, {
      onLine: (l) => { cleanNote.value = l.replace(/^[•\s]+/, '') },
      onEnd: (snap) => {
        cleaning.value = false
        cleanNote.value = ''
        if (snap.state === 'done') {
          layout.value = snap.result
          cleanInfo.value = snap.result.llm_clean
        } else err.value = snap.error || 'cleanup failed'
      },
      onError: () => { cleaning.value = false; cleanNote.value = ''; err.value = 'lost the cleanup stream' },
    }))
    .catch((e) => {
      cleaning.value = false
      cleanNote.value = ''
      // The server refuses an oversized window up front rather than running
      // the job, so this is the ordinary path when a whole-song tab is on
      // screen - it deserves the server's sentence, not "400".
      err.value = e.detail?.error === 'window_too_long' ? e.detail.detail : e.message
    })
}

/** Keep the URL describing what is on screen, so a passage can be linked to. */
watch([stem, partName, bars, view, voice], () => {
  router.replace({ query: {
    stem: stem.value,
    ...(partName.value ? { part: partName.value } : {}),
    ...(bars.value && !partName.value ? { bars: bars.value } : {}),
    ...(view.value ? { view: view.value } : {}),
    ...(voice.value !== 'all' ? { voice: voice.value } : {}),
  } })
  load()
})
watch(subdiv, () => load())
// The song is what the first load waits for - the whole-song window needs its
// duration - so this is the only place it is kicked off, not `onMounted` too.
watch(() => song.value?.analysis, (a) => { if (a) load() }, { immediate: true })

function pickPart(p) {
  partName.value = partName.value === p.name ? '' : p.name
  bars.value = ''
}


const noteCount = computed(() => layout.value?.notes?.length || 0)

/**
 * Whether cleanup applies to what is on screen.
 *
 * Cleanup is a snippet operation: the model is sent the window and writes it
 * back, so a whole-song request costs both ways and is roughly a hundred times
 * a solo. The limit itself lives in `musiccopilot.config` and arrives on the
 * layout (`clean_ok` / `clean_size`) - keeping a second copy of the numbers
 * here is how the browser and the CLI would come to disagree about what a
 * snippet is.
 */
const cleanOk = computed(() => layout.value?.clean_ok !== false)
const cleanWhy = computed(() => {
  if (!health.value.gemini) return 'Set GEMINI_API_KEY to use this'
  if (shown.value === 'sheet') return 'Cleanup applies to the tab, not the engraved score'
  const s = layout.value?.clean_size
  if (!cleanOk.value && s) {
    return `This passage is ${s.notes} notes over ${Math.round(s.seconds)}s. `
         + `Cleanup takes up to ${s.max_notes} notes / ${Math.round(s.max_seconds)}s `
         + '— pick a part or a bar range below.'
  }
  return 'Ask Gemini to merge jittered notes and drop noise'
})
const kindLabel = computed(() => {
  const l = layout.value
  if (!l) return ''
  if (l.kind === 'score') return `${l.clefs.join(' + ')} · ${l.key}`
  return l.kind === 'tab' ? `${l.instrument} tab` : `${l.clef} staff`
})
</script>

<template>
  <div class="tv">
    <!-- what to look at -->
    <div class="controls card">
      <div class="cgroup">
        <span class="eyebrow">Instrument</span>
        <div class="stemrow">
          <button
            v-for="s in stems" :key="s"
            class="btn btn-sm stembtn"
            :class="{ active: stem === s }"
            :style="{ '--c': stemMeta(s).color }"
            :title="voiceNote(song, s)"
            @click="stem = s"
          >
            {{ stemMeta(s).label }}
            <span class="dim tiny">{{ stemMeta(s).fretted ? 'tab' : 'staff' }}</span>
          </button>
        </div>
      </div>

      <div class="cgroup grow">
        <span class="eyebrow">Passage</span>
        <div class="partrow">
          <button
            class="btn btn-sm"
            :class="{ active: !partName && !bars }"
            @click="partName = ''; bars = ''"
          >Whole song</button>
          <button
            v-for="p in parts" :key="p.name"
            class="btn btn-sm"
            :class="{ active: partName === p.name }"
            @click="pickPart(p)"
          >
            {{ p.name }}
            <span v-if="p.lead" class="lead" :style="{ background: stemMeta(p.lead).color }" />
          </button>
        </div>
      </div>

      <div class="cgroup">
        <span class="eyebrow">Bars</span>
        <input
          v-model="bars" type="text" placeholder="17-24" class="barsin"
          @change="partName = ''"
        />
      </div>

      <div class="cgroup">
        <span class="eyebrow">Grid</span>
        <select v-model.number="subdiv">
          <option :value="1">quarters</option>
          <option :value="2">8ths</option>
          <option :value="4">16ths</option>
          <option :value="8">32nds</option>
        </select>
      </div>

      <div class="cgroup">
        <span class="eyebrow">Read</span>
        <select v-model="voice">
          <option value="all">everything</option>
          <option value="melody">the line</option>
          <option value="backing">under it</option>
        </select>
      </div>

      <div class="cgroup">
        <span class="eyebrow">Zoom</span>
        <input v-model.number="zoom" type="range" min="9" max="38" class="zoom" />
      </div>
    </div>

    <div v-if="err" class="err card">{{ err }}</div>
    <div v-else-if="loading" class="muted pad">Laying out the tab…</div>

    <template v-else-if="layout">
      <div class="sheet card">
        <header class="shead">
          <div>
            <h3 class="sh" :style="{ '--c': meta.color }">
              {{ meta.label }}
              <span class="dim thin">· {{ kindLabel }}</span>
            </h3>
            <div class="dim mono tiny">{{ layout.heading }} · {{ noteCount }} notes</div>
          </div>

          <div class="sacts">
            <span v-if="cleanInfo" class="chip chip-gold" :title="cleanInfo.changes">
              cleaned {{ cleanInfo.before }} → {{ cleanInfo.after }}
            </span>
            <button
              class="btn btn-sm"
              :disabled="cleaning || !health.gemini || shown === 'sheet' || !cleanOk"
              :title="cleanWhy"
              @click="llmClean"
            >{{ cleaning ? 'Cleaning…' : 'Clean up' }}</button>
            <span v-if="!cleanOk && health.gemini && shown !== 'sheet'" class="dim tiny">
              pick a part to clean
            </span>
            <span v-if="cleanNote" class="dim tiny mono">{{ cleanNote }}</span>
            <div class="seg">
              <button
                v-if="meta.fretted"
                class="btn btn-sm" :class="{ active: shown === 'grid' }"
                @click="view = 'grid'"
              >Tab</button>
              <button
                class="btn btn-sm" :class="{ active: shown === 'sheet' }"
                @click="view = 'sheet'"
              >Sheet</button>
              <button
                class="btn btn-sm" :class="{ active: shown === 'text' }"
                @click="view = 'text'"
              >Plain text</button>
            </div>
            <RouterLink
              class="btn btn-sm"
              :to="{ name: 'play', params: { id }, query: { part: partName, stems: stem } }"
            >Play along</RouterLink>
          </div>
        </header>

        <ScoreSheet
          v-if="shown === 'sheet'" :score="layout" :scale="Math.min(1.8, Math.max(0.65, zoom / 18))"
        />
        <pre v-else-if="shown === 'text'" class="ascii mono">{{ layout.text }}</pre>
        <TabGrid v-else :layout="layout" :col-width="zoom" />
      </div>

      <p v-if="layout.siblings?.length > 1" class="dim tiny sib">
        This part also occurs as: {{ layout.siblings.filter(s => s !== partName).join(', ') }}
      </p>
    </template>
  </div>
</template>

<style scoped>
.tv { display: flex; flex-direction: column; gap: 14px; }

.controls { padding: 13px 15px; display: flex; flex-wrap: wrap; gap: 18px; align-items: flex-start; }
.cgroup { display: flex; flex-direction: column; gap: 6px; }
.cgroup.grow { flex: 1; min-width: 240px; }

.stemrow, .partrow { display: flex; flex-wrap: wrap; gap: 5px; }
.stembtn.active { border-color: var(--c); color: var(--text); background: color-mix(in srgb, var(--c) 20%, var(--surface-2)); }
.tiny { font-size: 10px; }
.stembtn .tiny { opacity: 0.7; }

.lead { width: 5px; height: 5px; border-radius: 50%; display: inline-block; }
.barsin { width: 96px; }
.zoom { width: 108px; }

.sheet { padding: 14px 16px 10px; overflow: hidden; }
.shead { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 12px; flex-wrap: wrap; }
.sh { font-family: var(--font-display); font-size: 18px; color: var(--c); }
.thin { font-weight: 400; font-size: 12px; }
.sacts { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.seg { display: flex; gap: 3px; }

.ascii {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--text-2);
  overflow-x: auto;
  background: var(--bg-deep);
  padding: 12px;
  border-radius: var(--r-sm);
}

.err { padding: 13px 15px; color: #f0a8b4; border-color: var(--red-deep); }
.pad { padding: 22px; }
.sib { padding-left: 4px; }
</style>
