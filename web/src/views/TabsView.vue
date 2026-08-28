<script setup>
import { inject, ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, followJob, stemMeta } from '../api'
import TabGrid from '../components/TabGrid.vue'

const props = defineProps({ id: { type: String, required: true } })
const song = inject('song')
const route = useRoute()
const router = useRouter()
const health = inject('health')

const stem = ref(route.query.stem || 'guitar')
const partName = ref(route.query.part || '')
const bars = ref(route.query.bars || '')
const subdiv = ref(4)
const zoom = ref(17)
const layout = ref(null)
const loading = ref(false)
const err = ref('')
const cleaning = ref(false)
const cleanInfo = ref(null)
const cleanNote = ref('')
const showAscii = ref(false)

const stems = computed(() => Object.keys(song.value?.note_stems || {}))
const parts = computed(() => song.value?.form?.parts || [])
const meta = computed(() => stemMeta(stem.value))

/** The window currently on screen, in the shape both endpoints expect. */
function windowParams() {
  const p = { stem: stem.value, subdiv: subdiv.value }
  if (partName.value) p.part = partName.value
  else if (bars.value) p.bars = bars.value
  return p
}

async function load() {
  loading.value = true
  err.value = ''
  cleanInfo.value = null
  try {
    layout.value = await api.tab(props.id, windowParams())
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
    .catch((e) => { cleaning.value = false; cleanNote.value = ''; err.value = e.message })
}

/** Keep the URL describing what is on screen, so a passage can be linked to. */
watch([stem, partName, bars], () => {
  router.replace({ query: {
    stem: stem.value,
    ...(partName.value ? { part: partName.value } : {}),
    ...(bars.value && !partName.value ? { bars: bars.value } : {}),
  } })
  load()
})
watch(subdiv, () => load())

function pickPart(p) {
  partName.value = partName.value === p.name ? '' : p.name
  bars.value = ''
}

onMounted(load)

const noteCount = computed(() => layout.value?.notes?.length || 0)
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
              <span class="dim thin">· {{ layout.kind === 'tab' ? layout.instrument + ' tab' : layout.clef + ' staff' }}</span>
            </h3>
            <div class="dim mono tiny">{{ layout.heading }} · {{ noteCount }} notes</div>
          </div>

          <div class="sacts">
            <span v-if="cleanInfo" class="chip chip-gold" :title="cleanInfo.changes">
              cleaned {{ cleanInfo.before }} → {{ cleanInfo.after }}
            </span>
            <button
              class="btn btn-sm"
              :disabled="cleaning || !health.gemini"
              :title="health.gemini ? 'Ask Gemini to merge jittered notes and drop noise' : 'Set GEMINI_API_KEY to use this'"
              @click="llmClean"
            >{{ cleaning ? 'Cleaning…' : 'Clean up' }}</button>
            <span v-if="cleanNote" class="dim tiny mono">{{ cleanNote }}</span>
            <button class="btn btn-sm btn-ghost" @click="showAscii = !showAscii">
              {{ showAscii ? 'Grid' : 'Plain text' }}
            </button>
            <RouterLink
              class="btn btn-sm"
              :to="{ name: 'play', params: { id }, query: { part: partName, stems: stem } }"
            >Play along</RouterLink>
          </div>
        </header>

        <pre v-if="showAscii" class="ascii mono">{{ layout.text }}</pre>
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
