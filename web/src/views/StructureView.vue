<script setup>
import { inject, ref, computed, onBeforeUnmount } from 'vue'
import { api, mmss, stemMeta } from '../api'
import ChordDiagram from '../components/ChordDiagram.vue'

const props = defineProps({ id: { type: String, required: true } })
const song = inject('song')

const selected = ref(null)
const playingPart = ref(null)
const shapes = ref([])
let audio = null

const form = computed(() => song.value?.form)
const parts = computed(() => form.value?.parts || [])
const duration = computed(() => song.value?.analysis?.duration || 1)

/** The colour a part is drawn in: role first, so repeats of the same role
 *  read as the same block across the whole timeline. */
const ROLE_COLORS = {
  Intro: '#7d5a8c', Verse: '#b5484f', 'Pre-Chorus': '#c2703c',
  Chorus: '#d32742', Bridge: '#4f7a8c', Outro: '#6b5a7d',
  Instrumental: '#c98a3e',
}
function colorOf(p) {
  if (p.lead) return stemMeta(p.lead).color
  for (const [k, v] of Object.entries(ROLE_COLORS)) if (p.role.startsWith(k)) return v
  return '#8d7fa8'
}

function playSnippet(p) {
  if (!p.snippet) return
  if (audio) { audio.pause(); audio = null }
  if (playingPart.value === p.name) { playingPart.value = null; return }
  audio = new Audio(api.media.snippet(props.id, p.snippet))
  audio.play()
  playingPart.value = p.name
  audio.onended = () => { playingPart.value = null }
}

async function loadShapes() {
  if (shapes.value.length) return
  try { shapes.value = (await api.chords(props.id)).chords } catch { /* diagrams are optional */ }
}
loadShapes()

/** Chord fingerings for whichever part is open. */
const partShapes = computed(() => {
  if (!selected.value) return []
  const want = new Set(selected.value.loop.length ? selected.value.loop : selected.value.chords)
  return shapes.value.filter((s) => want.has(s.name))
})

const lyricsFor = (p) =>
  (song.value?.lyrics || []).filter((l) => l.end > p.start && l.start < p.end)

onBeforeUnmount(() => { if (audio) audio.pause() })
</script>

<template>
  <div v-if="form">
    <!-- the whole song, to scale -->
    <section class="card timeline">
      <div class="tlhead">
        <span class="eyebrow">Arrangement</span>
        <span class="dim mono">{{ form.outline.replace(/->/g, '→') }}</span>
      </div>
      <div class="track">
        <button
          v-for="p in parts"
          :key="p.name"
          class="block"
          :class="{ on: selected?.name === p.name }"
          :style="{
            width: ((p.end - p.start) / duration * 100) + '%',
            '--c': colorOf(p),
          }"
          :title="`${p.name} · bars ${p.bar}–${p.bar + p.bars - 1} · ${mmss(p.start)}`"
          @click="selected = selected?.name === p.name ? null : p"
        >
          <span class="blabel">{{ p.name }}</span>
        </button>
      </div>
      <div class="ruler">
        <span v-for="i in 6" :key="i" class="tick mono dim">{{ mmss((i - 1) / 5 * duration) }}</span>
      </div>
    </section>

    <!-- one card per part -->
    <div class="parts">
      <article
        v-for="p in parts"
        :key="p.name"
        class="part card"
        :class="{ open: selected?.name === p.name }"
        :style="{ '--c': colorOf(p) }"
      >
        <header class="phead" @click="selected = selected?.name === p.name ? null : p">
          <button
            class="play"
            :class="{ playing: playingPart === p.name }"
            :disabled="!p.snippet"
            :title="p.snippet ? 'Play this part' : 'No snippet cut yet'"
            @click.stop="playSnippet(p)"
          >{{ playingPart === p.name ? '■' : '▶' }}</button>

          <div class="pident">
            <h3 class="pname">{{ p.name }}</h3>
            <div class="pmeta dim mono">
              bars {{ p.bar }}–{{ p.bar + p.bars - 1 }} · {{ mmss(p.start) }}–{{ mmss(p.end) }} · {{ p.bars }} bars
            </div>
          </div>

          <div class="ptags">
            <span v-if="p.lead" class="chip chip-red">{{ stemMeta(p.lead).label }} lead</span>
            <span class="chip">{{ p.kind }}</span>
            <span v-if="p.key" class="chip chip-gold">{{ p.key }}</span>
            <span v-if="p.transpose" class="chip chip-red">{{ p.transpose > 0 ? '+' : '' }}{{ p.transpose }} st</span>
            <span v-if="p.varies" class="chip">varies</span>
          </div>
        </header>

        <div class="loop mono">{{ p.loop_text }}</div>

        <div v-if="selected?.name === p.name" class="detail">
          <div v-if="p.chords.length" class="drow">
            <span class="eyebrow dl">Bar by bar</span>
            <div class="bars mono">
              <span v-for="(c, i) in p.chords" :key="i" class="barc">
                <b class="dim">{{ p.bar + i }}</b>{{ c }}
              </span>
            </div>
          </div>

          <div v-if="partShapes.length" class="drow">
            <span class="eyebrow dl">Fingerings</span>
            <div class="shapes">
              <ChordDiagram v-for="s in partShapes" :key="s.name" :chord="s" />
            </div>
          </div>

          <div v-if="lyricsFor(p).length" class="drow">
            <span class="eyebrow dl">Words</span>
            <div class="words">
              <p v-for="(l, i) in lyricsFor(p)" :key="i" class="lyric">
                <span class="lt mono dim">{{ mmss(l.start) }}</span>{{ l.text }}
              </p>
            </div>
          </div>

          <div class="drow acts">
            <RouterLink
              class="btn btn-sm"
              :to="{ name: 'tabs', params: { id }, query: { part: p.name, stem: p.lead || 'guitar' } }"
            >Open the tab</RouterLink>
            <RouterLink
              class="btn btn-sm"
              :to="{ name: 'play', params: { id }, query: { part: p.name } }"
            >Play along</RouterLink>
            <RouterLink
              class="btn btn-sm"
              :to="{ name: 'solo', params: { id }, query: { part: p.name } }"
            >Write a solo over it</RouterLink>
          </div>
        </div>
      </article>
    </div>
  </div>

  <div v-else class="card pad muted">No form detected — re-analyse to build one.</div>
</template>

<style scoped>
.timeline { padding: 15px 17px; margin-bottom: 18px; }
.tlhead { display: flex; align-items: baseline; gap: 12px; margin-bottom: 11px; flex-wrap: wrap; }
.tlhead .dim { font-size: 11.5px; }

.track { display: flex; gap: 2px; height: 42px; }
.block {
  border: 0;
  border-radius: 3px;
  background: color-mix(in srgb, var(--c) 34%, var(--surface-2));
  border-top: 2px solid var(--c);
  color: var(--text-2);
  cursor: pointer;
  overflow: hidden;
  padding: 0 5px;
  min-width: 0;
  transition: background 0.14s, color 0.14s;
}
.block:hover, .block.on { background: color-mix(in srgb, var(--c) 62%, var(--surface-2)); color: #fff; }
.blabel { font-size: 10.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; }

.ruler { display: flex; justify-content: space-between; margin-top: 6px; }
.tick { font-size: 10px; }

.parts { display: flex; flex-direction: column; gap: 9px; }
.part { border-left: 3px solid var(--c); overflow: hidden; }
.part.open { border-color: var(--c); background: var(--surface-2); }

.phead { display: flex; align-items: center; gap: 12px; padding: 12px 15px; cursor: pointer; }

.play {
  width: 31px; height: 31px; flex: none;
  border-radius: 50%;
  border: 1px solid var(--line);
  background: var(--surface-2);
  color: var(--text-2);
  cursor: pointer;
  font-size: 11px;
  transition: background 0.14s, color 0.14s, border-color 0.14s;
}
.play:hover:not(:disabled) { background: var(--c); border-color: var(--c); color: #fff; }
.play.playing { background: var(--red); border-color: var(--red-bright); color: #fff; animation: pulse 1.4s infinite; }
.play:disabled { opacity: 0.32; cursor: not-allowed; }

.pident { flex: 1; min-width: 0; }
.pname { font-family: var(--font-display); font-size: 17px; }
.pmeta { font-size: 11px; }
.ptags { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; }

.loop {
  padding: 0 15px 12px 58px;
  font-size: 12.5px;
  color: var(--gold);
  overflow-x: auto;
  white-space: nowrap;
}

.detail { border-top: 1px solid var(--line-soft); padding: 14px 15px; background: var(--bg-deep); }
.drow { margin-bottom: 15px; }
.drow:last-child { margin-bottom: 0; }
.dl { display: block; margin-bottom: 7px; }

.bars { display: flex; flex-wrap: wrap; gap: 5px; }
.barc {
  background: var(--surface-2);
  border: 1px solid var(--line-soft);
  border-radius: 3px;
  padding: 2px 7px;
  font-size: 12px;
  color: var(--gold);
}
.barc b { font-weight: 400; font-size: 9.5px; margin-right: 5px; }

.shapes { display: flex; flex-wrap: wrap; gap: 9px; }

.words { display: flex; flex-direction: column; gap: 2px; }
.lyric { margin: 0; font-size: 13.5px; color: var(--text-2); }
.lt { font-size: 10.5px; margin-right: 9px; }

.acts { display: flex; gap: 7px; flex-wrap: wrap; }
.pad { padding: 22px; }
</style>
