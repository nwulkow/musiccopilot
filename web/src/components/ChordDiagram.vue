<script setup>
/** A fingering, drawn from the fret numbers `tabs.chord_frets` worked out.
 *  `null` in a slot means that string is not played. */
import { computed } from 'vue'

const props = defineProps({ chord: { type: Object, required: true } })

const frets = computed(() => props.chord.frets || [])
const played = computed(() => frets.value.filter((f) => f != null && f > 0))

/** Which fret the little window starts at: open shapes start at the nut,
 *  barre shapes start at their lowest fretted note. */
const base = computed(() => {
  if (!played.value.length) return 1
  const lo = Math.min(...played.value)
  const hi = Math.max(...played.value)
  return hi <= 4 ? 1 : lo
})
const SPAN = 4
</script>

<template>
  <figure class="cd" v-if="frets.length">
    <figcaption class="nm">{{ chord.name }}</figcaption>
    <div class="grid">
      <!-- one column per string, low E on the left as you look at the neck -->
      <div v-for="(f, i) in frets" :key="i" class="str">
        <span class="top mono">{{ f == null ? '×' : (f === 0 ? '○' : '') }}</span>
        <span
          v-for="n in SPAN" :key="n"
          class="slot"
          :class="{ dot: f != null && f > 0 && f - base + 1 === n }"
        />
      </div>
    </div>
    <div v-if="base > 1" class="base mono dim">{{ base }}fr</div>
  </figure>
</template>

<style scoped>
.cd {
  margin: 0;
  padding: 7px 9px 6px;
  background: var(--surface-2);
  border: 1px solid var(--line-soft);
  border-radius: var(--r-sm);
  text-align: center;
}
.nm { font-size: 12px; font-weight: 600; color: var(--gold); margin-bottom: 5px; }
.grid { display: flex; gap: 4px; justify-content: center; }
.str { display: flex; flex-direction: column; align-items: center; }
.top { font-size: 8px; height: 10px; line-height: 10px; color: var(--text-4); }
.slot {
  width: 9px; height: 9px;
  border-top: 1px solid var(--line);
  border-left: 1px solid var(--line-soft);
  position: relative;
}
.str:last-child .slot { border-right: 1px solid var(--line-soft); }
.slot.dot::after {
  content: '';
  position: absolute;
  left: 50%; top: 50%;
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--red-bright);
  transform: translate(-50%, -30%);
}
.base { font-size: 8.5px; margin-top: 2px; }
</style>
