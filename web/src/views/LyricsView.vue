<script setup>
import { inject, ref, computed, onBeforeUnmount } from 'vue'
import { api, mmss } from '../api'

const props = defineProps({ id: { type: String, required: true } })
const song = inject('song')

const playing = ref(false)
const pos = ref(0)
let audio = null
let raf = 0

const lines = computed(() => song.value?.lyrics || [])
const parts = computed(() => song.value?.form?.parts || [])

/** Lyrics grouped under the part they are sung in, so the page reads like a
 *  lyric sheet with section headings rather than a flat list of timings. */
const grouped = computed(() => {
  if (!parts.value.length) return [{ part: null, lines: lines.value }]
  return parts.value
    .map((p) => ({ part: p, lines: lines.value.filter((l) => l.end > p.start && l.start < p.end) }))
    .filter((g) => g.lines.length)
})

function tick() {
  if (audio) pos.value = audio.currentTime
  if (audio && !audio.paused) raf = requestAnimationFrame(tick)
}

function playFrom(t) {
  if (!audio) {
    audio = new Audio(api.media.mix(props.id))
    audio.onplay = () => { playing.value = true; tick() }
    audio.onpause = () => { playing.value = false }
  }
  audio.currentTime = t
  audio.play()
}

function toggle() {
  if (!audio) return playFrom(0)
  audio.paused ? audio.play() : audio.pause()
}

const isNow = (l) => pos.value >= l.start && pos.value < l.end

onBeforeUnmount(() => { cancelAnimationFrame(raf); if (audio) audio.pause() })
</script>

<template>
  <div v-if="lines.length" class="lv">
    <div class="card bar">
      <button class="btn btn-sm btn-primary" @click="toggle">
        {{ playing ? '❚❚ Pause' : '▶ Play' }}
      </button>
      <span class="mono dim">{{ mmss(pos) }}</span>
      <span class="dim tiny">Click any line to hear it.</span>
    </div>

    <section v-for="(g, i) in grouped" :key="i" class="block card">
      <h3 v-if="g.part" class="ph">
        {{ g.part.name }}
        <span class="dim mono tiny">bars {{ g.part.bar }}–{{ g.part.bar + g.part.bars - 1 }}</span>
      </h3>
      <p
        v-for="(l, j) in g.lines" :key="j"
        class="line" :class="{ now: isNow(l) }"
        @click="playFrom(l.start)"
      >
        <span class="t mono dim">{{ mmss(l.start) }}</span>
        <span class="txt">{{ l.text }}</span>
      </p>
    </section>
  </div>

  <div v-else class="card pad muted">
    No lyrics — either this song has none, or the vocal stem was not transcribed.
  </div>
</template>

<style scoped>
.lv { display: flex; flex-direction: column; gap: 11px; max-width: 780px; }
.bar { display: flex; align-items: center; gap: 12px; padding: 10px 14px; position: sticky; top: 0; z-index: 10; backdrop-filter: blur(8px); background: color-mix(in srgb, var(--surface) 92%, transparent); }
.tiny { font-size: 11px; }

.block { padding: 15px 18px; }
.ph {
  font-family: var(--font-display); font-size: 15px;
  color: var(--gold); margin-bottom: 9px;
  display: flex; align-items: baseline; gap: 9px;
}
.ph .tiny { font-size: 10.5px; }

.line {
  display: flex; gap: 13px; margin: 0;
  padding: 3px 7px; border-radius: var(--r-sm);
  cursor: pointer; font-size: 15px; color: var(--text-2);
  transition: background 0.13s, color 0.13s;
}
.line:hover { background: var(--surface-2); color: var(--text); }
.line.now { background: rgba(168, 23, 47, 0.2); color: #fff; }
.t { font-size: 10.5px; padding-top: 4px; flex: none; }
.txt { font-family: var(--font-display); }
.pad { padding: 24px; }
</style>
