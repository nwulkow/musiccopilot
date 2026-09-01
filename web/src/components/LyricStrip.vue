<script setup>
/**
 * The words, under the vocal line, on the play-along's clock.
 *
 * A vocal tab is a row of note names, which tells a singer the melody and
 * nothing about what they are singing. The lyrics are already transcribed and
 * already timed (Whisper segments, cached in `lyrics.json`), so the only thing
 * missing was putting them on the same clock as everything else on the page -
 * this reads `cursorTime` from the shared `useTransport` like `TabGrid` and
 * `ScoreSheet` do, so the words cannot drift away from the notes above them.
 *
 * The current line stays lit until the next one starts rather than going out
 * when it ends. Whisper's segments have gaps between them - a breath, an
 * instrumental bar - and a highlight that blanks in those gaps reads as having
 * lost your place at exactly the moment you are looking down to find it.
 *
 * Timing is per line, not per word: Whisper gives one start and end for a
 * whole segment, so there is nothing finer to be honest about. Clicking a line
 * seeks to it, which is how you loop the phrase you keep getting wrong.
 */
import { computed, ref, watch } from 'vue'
import { mmss } from '../api'

const props = defineProps({
  lines: { type: Array, required: true },
  cursorTime: { type: Number, default: 0 },
  follow: { type: Boolean, default: false },
})
const emit = defineEmits(['seek'])

const box = ref(null)
const rows = ref([])

/** The line being sung, or the last one sung if we are between two. */
const now = computed(() => {
  let idx = -1
  for (let i = 0; i < props.lines.length; i++) {
    if (props.lines[i].start <= props.cursorTime) idx = i
    else break
  }
  return idx
})

/** ...and whether it is actually sounding, which the dot shows and the
 *  highlight deliberately does not. */
const sounding = computed(() => {
  const l = props.lines[now.value]
  return !!l && props.cursorTime < l.end
})

// Same rule as TabGrid: jump for a seek, glide for playback. A smooth scroll
// re-issued every frame never lands, so this only fires when the line changes.
watch(now, (i) => {
  if (!props.follow || i < 0 || !box.value) return
  const el = rows.value[i]
  if (!el) return
  const target = Math.max(0, el.offsetTop - (box.value.clientHeight - el.clientHeight) / 2)
  box.value.scrollTo({
    top: target,
    behavior: Math.abs(target - box.value.scrollTop) > 220 ? 'auto' : 'smooth',
  })
})
</script>

<template>
  <div ref="box" class="strip">
    <p
      v-for="(l, i) in lines" :key="i"
      :ref="(el) => (rows[i] = el)"
      class="line"
      :class="{ now: i === now, live: i === now && sounding, past: i < now }"
      @click="emit('seek', l.start)"
    >
      <span class="t mono">{{ mmss(l.start) }}</span>
      <span class="txt">{{ l.text }}</span>
    </p>
  </div>
</template>

<style scoped>
.strip {
  max-height: 132px;
  overflow-y: auto;
  padding: 2px 0 6px;
  margin-bottom: 9px;
  border-bottom: 1px solid var(--line-soft);
  scrollbar-width: thin;
}

.line {
  display: flex; gap: 11px; align-items: baseline;
  margin: 0; padding: 3px 8px;
  border-radius: var(--r-sm);
  cursor: pointer;
  font-size: 14.5px;
  color: var(--text-3);
  transition: background 0.13s, color 0.13s;
}
.line:hover { background: var(--surface-2); color: var(--text); }
.line.past { color: var(--text-4); }
.line.now { background: color-mix(in srgb, var(--c, var(--red)) 22%, transparent); color: #fff; }
.line.live .txt::after {
  content: '';
  display: inline-block;
  width: 6px; height: 6px; margin-left: 7px;
  border-radius: 50%;
  background: var(--red-glow);
  animation: pulse 1.1s infinite;
}

.t { font-size: 10.5px; color: var(--text-4); flex: none; }
.line.now .t { color: rgba(255, 255, 255, 0.62); }
.txt { font-family: var(--font-display); }
</style>
