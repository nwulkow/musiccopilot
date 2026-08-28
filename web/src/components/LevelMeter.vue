<script setup>
/** Input level. Mostly it answers "is the mic actually hearing anything",
 *  which is the first thing you check when the display stays empty. */
defineProps({ level: { type: Number, default: 0 }, segments: { type: Number, default: 22 } })
</script>

<template>
  <div class="meter" :title="`input level ${(level * 100).toFixed(0)}%`">
    <span
      v-for="i in segments" :key="i"
      class="seg"
      :class="{
        on: level * segments >= i,
        hot: i > segments * 0.82,
        warm: i > segments * 0.6 && i <= segments * 0.82,
      }"
    />
  </div>
</template>

<style scoped>
.meter { display: flex; gap: 2px; align-items: flex-end; height: 17px; }
.seg {
  width: 3px; height: 100%;
  background: var(--surface-3);
  border-radius: 1px;
  transition: background 0.06s;
}
.seg.on { background: var(--ok); }
.seg.on.warm { background: var(--gold); }
.seg.on.hot { background: var(--err); }
</style>
