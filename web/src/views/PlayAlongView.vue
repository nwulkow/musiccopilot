<script setup>
/**
 * Play along with several instruments at once.
 *
 * Every selected stem gets its own tab (or staff), and all of them scroll
 * under one cursor because they share a single `useTransport` clock - two
 * <audio> elements started together would drift, and a bass tab a beat away
 * from the guitar tab is worse than no bass tab.
 */
import { inject, ref, computed, watch, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { api, stemMeta, mmss } from '../api'
import TabGrid from '../components/TabGrid.vue'
import ScoreSheet from '../components/ScoreSheet.vue'
import { useTransport } from '../composables/useTransport'

const props = defineProps({ id: { type: String, required: true } })
const song = inject('song')
const route = useRoute()

const transport = useTransport()
const partName = ref(route.query.part || '')
const picked = ref((route.query.stems || 'guitar').split(',').filter(Boolean))
const views = ref({})            // stem -> 'tab' | 'sheet' | 'notes'
const layouts = ref({})          // stem -> tab layout or score, per its view
const loading = ref(false)
const minusMine = ref([])        // stems dropped from the bed, so you play them
const zoom = ref(16)
const detail = ref(4)      // grid steps per beat: how fine the rhythm is read
const err = ref('')

const stems = computed(() => Object.keys(song.value?.note_stems || {}))
const parts = computed(() => song.value?.form?.parts || [])
const analysis = computed(() => song.value?.analysis)

const part = computed(() => parts.value.find((p) => p.name === partName.value) || null)
const region = computed(() =>
  part.value ? { start: part.value.start, end: part.value.end } : null)

/** Sheet music is scaled off the same slider that sets the tab's column width,
 *  so one "zoom" control means one thing on screen whatever is being read. */
const sheetScale = computed(() => Math.min(1.8, Math.max(0.65, zoom.value / 17)))

/** A stem with no fretboard has no frets to show; it reads as sheet music. */
const viewOf = (s) => views.value[s] || (stemMeta(s).fretted ? 'tab' : 'sheet')

/**
 * The passage every sheet is laid out for.
 *
 * With no part chosen this is the *whole song*, spelled out as an explicit
 * start and end. It has to be: `cli._window` defaults an unbounded request to
 * the first twenty seconds, which is the right default for a terminal that
 * would otherwise print a four-minute tab and the wrong one here - it is why
 * the play-along used to stop dead twenty seconds in, with the cursor running
 * off the end of a grid that had nothing left to show.
 */
function windowParams(stem) {
  const p = { stem, subdiv: detail.value }
  if (partName.value) p.part = partName.value
  else { p.start = 0; p.end = analysis.value?.duration ?? 0 }
  return p
}

/** Fetch one stem's sheet. Which endpoint depends on how it is being read:
 *  engraved notation is a different thing from a grid, not a skin on one. */
async function loadStem(s) {
  const fetcher = viewOf(s) === 'sheet' ? api.score : api.tab
  try { return await fetcher(props.id, windowParams(s)) } catch { return null }
}

async function loadLayouts() {
  loading.value = true
  err.value = ''
  try {
    const got = await Promise.all(picked.value.map(loadStem))
    layouts.value = Object.fromEntries(picked.value.map((s, i) => [s, got[i]]))
  } catch (e) { err.value = e.message } finally { loading.value = false }
}

/** Switching one stem's view only re-fetches that stem. */
async function setView(s, v) {
  if (viewOf(s) === v) return
  const before = viewOf(s)
  views.value = { ...views.value, [s]: v }
  if ((before === 'sheet') !== (v === 'sheet')) {
    layouts.value = { ...layouts.value, [s]: await loadStem(s) }
  }
}

/** Load the bed: the mix, or the mix minus whatever you are playing. */
function loadAudio(autoplay = false) {
  const url = minusMine.value.length
    ? api.media.backing(props.id, minusMine.value.join(','))
    : api.media.mix(props.id)
  transport.load(url, { region: region.value, autoplay })
}

function toggleStem(s) {
  picked.value = picked.value.includes(s)
    ? picked.value.filter((x) => x !== s)
    : [...picked.value, s]
}

function toggleMinus(s) {
  minusMine.value = minusMine.value.includes(s)
    ? minusMine.value.filter((x) => x !== s)
    : [...minusMine.value, s]
}

watch(picked, loadLayouts, { deep: true })
watch(detail, loadLayouts)
watch(partName, () => { loadLayouts(); loadAudio() })
watch(minusMine, () => loadAudio(), { deep: true })
// The song is what the first load waits for: the whole-song window needs its
// duration, and that arrives with the song rather than with the route. This is
// the *only* place the first load is kicked off - loading again from
// `onMounted` as well would land a second copy of every layout after the user
// had already scrolled or seeked, and snap all of it back to the start.
watch(analysis, (a) => {
  if (!a) return
  transport.tempo.value = a.tempo
  loadLayouts()
}, { immediate: true })

onMounted(loadAudio)

const pos = computed(() => transport.time.value)
const barNow = computed(() => {
  const bt = song.value?.form?.bar_times
  if (!bt) return null
  let i = 0
  while (i < bt.length && bt[i] <= pos.value) i++
  return i
})
</script>

<template>
  <div class="pa">
    <!-- transport -->
    <div class="card bar">
      <button class="playbtn" :class="{ on: transport.playing.value }" @click="transport.toggle()">
        {{ transport.playing.value ? '❚❚' : '▶' }}
      </button>
      <button class="btn btn-icon btn-ghost" title="Back to the start" @click="transport.stop()">■</button>

      <div class="clock mono">
        <span class="now">{{ mmss(pos) }}</span>
        <span class="dim"> / {{ mmss(region ? region.end : transport.duration.value) }}</span>
        <span v-if="barNow" class="dim bar-n"> · bar {{ barNow }}</span>
      </div>

      <div class="scrub">
        <input
          type="range" :min="region ? region.start : 0"
          :max="region ? region.end : (transport.duration.value || 1)"
          :value="pos" step="0.01"
          @input="transport.seek(+$event.target.value)"
        />
      </div>

      <label class="ctl">
        <span class="eyebrow">Speed</span>
        <select v-model.number="transport.rate.value">
          <option :value="0.5">50%</option>
          <option :value="0.65">65%</option>
          <option :value="0.75">75%</option>
          <option :value="0.9">90%</option>
          <option :value="1">100%</option>
          <option :value="1.25">125%</option>
        </select>
      </label>

      <label class="ctl">
        <span class="eyebrow">Count-in</span>
        <select v-model.number="transport.countIn.value">
          <option :value="0">none</option>
          <option :value="4">4</option>
          <option :value="8">8</option>
        </select>
      </label>

      <label class="ctl chk">
        <input type="checkbox" v-model="transport.loop.value" />
        <span>Loop</span>
      </label>

      <div v-if="transport.countingIn.value" class="countin mono">{{ transport.countingIn.value }}</div>
    </div>

    <!-- what to play -->
    <div class="card picker">
      <div class="pgroup">
        <span class="eyebrow">Passage</span>
        <div class="row">
          <button class="btn btn-sm" :class="{ active: !partName }" @click="partName = ''">Whole song</button>
          <button
            v-for="p in parts" :key="p.name"
            class="btn btn-sm" :class="{ active: partName === p.name }"
            @click="partName = p.name"
          >{{ p.name }}</button>
        </div>
      </div>

      <div class="pgroup">
        <span class="eyebrow">Show these instruments</span>
        <div class="row">
          <button
            v-for="s in stems" :key="s"
            class="btn btn-sm stembtn" :class="{ active: picked.includes(s) }"
            :style="{ '--c': stemMeta(s).color }"
            @click="toggleStem(s)"
          >{{ stemMeta(s).label }}</button>
        </div>
      </div>

      <div class="pgroup">
        <span class="eyebrow">Drop from the mix (you play it)</span>
        <div class="row">
          <button
            v-for="s in song?.stems || []" :key="s"
            class="btn btn-sm" :class="{ active: minusMine.includes(s) }"
            @click="toggleMinus(s)"
          >{{ stemMeta(s).label }}</button>
        </div>
      </div>

      <div class="pgroup">
        <span class="eyebrow">Rhythm</span>
        <select v-model.number="detail" class="detail">
          <option :value="2">8ths</option>
          <option :value="4">16ths</option>
          <option :value="8">32nds</option>
        </select>
      </div>

      <div class="pgroup">
        <span class="eyebrow">Zoom</span>
        <input v-model.number="zoom" type="range" min="9" max="34" class="zoom" />
      </div>
    </div>

    <div v-if="transport.error.value" class="err card">{{ transport.error.value }}</div>
    <div v-if="loading" class="muted pad">Laying out…</div>

    <!-- one sheet per instrument, all on the same clock -->
    <div class="sheets">
      <section
        v-for="s in picked" :key="s"
        class="sheet card"
        :style="{ '--c': stemMeta(s).color }"
      >
        <header class="shead">
          <h3 class="sh">{{ stemMeta(s).label }}</h3>
          <div class="sright">
            <span v-if="minusMine.includes(s)" class="chip chip-red">muted — your part</span>
            <div class="seg">
              <button
                v-if="stemMeta(s).fretted"
                class="btn btn-sm" :class="{ active: viewOf(s) === 'tab' }"
                @click="setView(s, 'tab')"
              >Tab</button>
              <button
                class="btn btn-sm" :class="{ active: viewOf(s) === 'sheet' }"
                @click="setView(s, 'sheet')"
              >Sheet</button>
              <button
                class="btn btn-sm" :class="{ active: viewOf(s) === 'notes' }"
                @click="setView(s, 'notes')"
              >Notes</button>
            </div>
          </div>
        </header>

        <ScoreSheet
          v-if="layouts[s] && viewOf(s) === 'sheet'"
          :score="layouts[s]"
          :cursor-time="pos"
          :scale="sheetScale"
          follow
          @seek="transport.seek($event)"
        />

        <TabGrid
          v-else-if="layouts[s] && viewOf(s) !== 'notes'"
          :layout="layouts[s]"
          :cursor-time="pos"
          :col-width="zoom"
          follow
          @seek="transport.seek($event)"
        />

        <!-- the note-name ruler: for stems where frets would be a lie, and
             as an option for the ones where they would not -->
        <div v-else-if="layouts[s]" class="ruler">
          <span
            v-for="(n, i) in layouts[s].notes" :key="i"
            class="rn mono"
            :class="{ on: n.start <= pos && pos < n.end }"
          >{{ n.name }}</span>
        </div>

        <div v-else class="dim pad">No notes for this stem in this passage.</div>
      </section>
    </div>

    <div v-if="!picked.length" class="card pad muted">
      Pick an instrument above to see what it plays.
    </div>
  </div>
</template>

<style scoped>
.pa { display: flex; flex-direction: column; gap: 13px; }

.bar { display: flex; align-items: center; gap: 13px; padding: 11px 15px; flex-wrap: wrap; position: sticky; top: 0; z-index: 20; backdrop-filter: blur(9px); background: color-mix(in srgb, var(--surface) 92%, transparent); }

.playbtn {
  width: 40px; height: 40px; flex: none;
  border-radius: 50%;
  border: 1px solid #7d1128;
  background: linear-gradient(180deg, var(--red-bright), var(--red));
  color: #fff; font-size: 13px; cursor: pointer;
  transition: box-shadow 0.15s, transform 0.08s;
}
.playbtn:hover { box-shadow: var(--glow-red); }
.playbtn:active { transform: translateY(1px); }
.playbtn.on { animation: pulse 2s infinite; }

.clock { font-size: 13px; min-width: 140px; }
.now { color: var(--red-glow); font-weight: 600; }
.bar-n { font-size: 11.5px; }

.scrub { flex: 1; min-width: 180px; }

.ctl { display: flex; flex-direction: column; gap: 2px; }
.ctl select { width: auto; padding: 4px 8px; font-size: 12.5px; }
.chk { flex-direction: row; align-items: center; gap: 6px; font-size: 12.5px; color: var(--text-3); cursor: pointer; }
.chk input { width: auto; }

.countin {
  font-size: 20px; color: var(--red-glow); font-weight: 700;
  min-width: 26px; text-align: center; animation: pulse 0.4s infinite;
}

.picker { padding: 12px 15px; display: flex; flex-wrap: wrap; gap: 18px; }
.pgroup { display: flex; flex-direction: column; gap: 6px; }
.row { display: flex; flex-wrap: wrap; gap: 5px; }
.stembtn.active { border-color: var(--c); background: color-mix(in srgb, var(--c) 20%, var(--surface-2)); color: var(--text); }
.zoom { width: 110px; }
.detail { width: auto; padding: 4px 8px; font-size: 12.5px; }

.sheets { display: flex; flex-direction: column; gap: 11px; }
.sheet { padding: 12px 15px 8px; border-left: 3px solid var(--c); }
.shead { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 9px; }
.sh { font-family: var(--font-display); font-size: 16px; color: var(--c); }
.sright { display: flex; align-items: center; gap: 7px; }
.seg { display: flex; gap: 3px; }

.ruler { display: flex; flex-wrap: wrap; gap: 4px; padding: 6px 0 10px; }
.rn {
  font-size: 11.5px; padding: 2px 6px; border-radius: 3px;
  background: var(--surface-2); color: var(--text-3);
  border: 1px solid var(--line-soft);
}
.rn.on { background: var(--red); color: #fff; border-color: var(--red-bright); box-shadow: var(--glow-red); }

.err { padding: 12px 15px; color: #f0a8b4; border-color: var(--red-deep); }
.pad { padding: 20px; }
</style>
