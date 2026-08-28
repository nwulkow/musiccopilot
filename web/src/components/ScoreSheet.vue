<script setup>
/**
 * Engraved notation: real note heads, stems, beams, rests and ties.
 *
 * The counterpart to `TabGrid.vue` for stems with no fretboard. It draws the
 * score `musiccopilot.score` built - and only draws it. Every musical
 * decision was already made in Python: which hand a note is on, what value it
 * is written as, how it is spelled against the key, where the rests fall. By
 * the time a note reaches this file it already *is* a dotted eighth; nothing
 * here may decide that it is.
 *
 * What is decided here is engraving, which is VexFlow's job: glyph shapes,
 * accidental placement, beam angles, how wide a bar has to be to fit its
 * notes. That last one is why measures are packed into systems in two passes
 * - ask each measure how narrow it can be, fill a line with as many as fit,
 * then hand the slack back out in proportion. A line of four bars where one
 * holds a run of sixteenths and the others hold a whole note should not give
 * them a quarter of the width each.
 *
 * The play-along cursor rides a time -> (x, y) map collected from the notes
 * VexFlow actually placed (`getAbsoluteX`), so it points at the head you are
 * meant to be playing rather than at a guess about where the head went.
 */
import { ref, shallowRef, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import {
  Accidental, Barline, Beam, Dot, Formatter, Fraction, Renderer, Stave,
  StaveConnector, StaveNote, StaveTie, Voice,
} from 'vexflow'

const props = defineProps({
  score: { type: Object, required: true },
  cursorTime: { type: Number, default: null },   // absolute seconds, or null
  scale: { type: Number, default: 1 },
  follow: { type: Boolean, default: false },     // keep the cursor in view
  showChords: { type: Boolean, default: true },
})
const emit = defineEmits(['seek'])

const box = ref(null)          // the scroll box
const paper = ref(null)        // what VexFlow renders into
const width = ref(0)
const systems = shallowRef([]) // { y, height, points: [{ t, x }] } per system
const svgH = ref(0)
const failed = ref('')

// --- engraving constants (VexFlow units, before `scale`) --------------------
const PAD_X = 12               // paper margin either side
const TOP = 20                 // paper margin above the first system
const LINE = 10                // VexFlow's spacing between two staff lines
const STAVE_H = 4 * LINE       // the five printed lines themselves
const CHORD_ROW = 18           // vertical room one chord symbol needs
const SYSTEM_GAP = 30          // white between one system and the next
const CLEAR = 14               // white kept outside the outermost note head
const MAX_LEDGER = 84          // ... but a stray high note may not push forever
const MIN_MEASURE = 90         // a bar never gets narrower than this
const REST_KEY = { treble: 'b/4', bass: 'd/3' }

// Which staff step a pitch sits on, counted from a clef's bottom line, one
// step per letter name. This is geometry, not spelling - it only answers "how
// far off the five lines is this head", which is what decides how much white
// a system needs. What the note is *called* was settled in Python.
const LETTER_STEP = [0, 0, 1, 1, 2, 3, 3, 4, 4, 5, 5, 6]
const CLEF_BOTTOM = { treble: 64, bass: 43 }        // E4, G2
const TOP_STEP = 8                                   // the staff's top line

// VexFlow reserves a band above a stave's top line for measure numbers and
// stave text: `new Stave(x, y, w)` puts the top of *that band* at y, not the
// top line. Everything in this file is positioned by the line the reader
// sees, so measure the band once and subtract it when placing a stave.
const STAVE_HEAD = new Stave(0, 0, 10).getYForLine(0)

function staffStep(pitch, clef) {
  const b = CLEF_BOTTOM[clef] ?? CLEF_BOTTOM.treble
  return (Math.floor(pitch / 12) - Math.floor(b / 12)) * 7
    + LETTER_STEP[pitch % 12] - LETTER_STEP[b % 12]
}

const clefs = computed(() => props.score?.clefs || ['treble'])
const measures = computed(() => props.score?.measures || [])

/**
 * How tall one system has to be, from the notes that are actually in it.
 *
 * Fixed spacing does not survive a piano part: the constants that look right
 * for a melody sitting inside the treble staff let a bass line four ledger
 * lines down collide with the system underneath it. So each stave is measured
 * for how far its heads reach past the five lines, and the white goes where
 * those notes need it.
 *
 * Measured per system rather than once for the whole passage, because one
 * note up at the top of the page would otherwise buy that much headroom for
 * every line in the piece - and a chord symbol floating eighty pixels above
 * its own bar reads as belonging to the system above it.
 */
function systemGeom(inSystem) {
  const reach = clefs.value.map((clef, si) => {
    let lo = 0
    let hi = TOP_STEP
    for (const m of inSystem) {
      for (const ev of m.voices[si] || []) {
        for (const p of ev.pitches) {
          const st = staffStep(p, clef)
          if (st < lo) lo = st
          if (st > hi) hi = st
        }
      }
    }
    return {
      ascent: Math.min(MAX_LEDGER, (hi - TOP_STEP) * (LINE / 2)) + CLEAR,
      descent: Math.min(MAX_LEDGER, -lo * (LINE / 2)) + CLEAR,
    }
  })

  // A chord symbol belongs to the bar under it, so it sits just clear of the
  // highest thing in that bar rather than at a fixed height above the staff -
  // a fixed height leaves it stranded halfway to the system above whenever
  // the music reaches up, reading as that system's chord instead of its own.
  const tops = []
  let y = reach[0].ascent + CHORD_ROW
  reach.forEach((r, i) => {
    tops.push(y)
    y += STAVE_H + r.descent + (reach[i + 1]?.ascent || 0)
  })
  return {
    tops,
    chordY: CHORD_ROW - 3,
    height: tops[tops.length - 1] + STAVE_H + reach[reach.length - 1].descent,
  }
}

/** A written event as a VexFlow note. Rests are notes with no keys. */
function makeNote(ev, clef) {
  const rest = !ev.keys.length
  const note = new StaveNote({
    keys: rest ? [REST_KEY[clef]] : ev.keys,
    duration: ev.duration + (rest ? 'r' : ''),
    dots: ev.dots,
    clef,
    auto_stem: true,
  })
  for (let i = 0; i < ev.dots; i++) Dot.buildAndAttach([note], { all: true })
  return note
}

/**
 * Everything for one measure on one stave: the notes, the voice they sit in,
 * and the beams (generated before formatting, because generating them is what
 * settles the stem directions the formatter then works around).
 */
function buildVoice(events, clef) {
  const notes = events.map((ev) => makeNote(ev, clef))
  const voice = new Voice({
    num_beats: props.score.beats_per_bar, beat_value: 4,
  }).setMode(Voice.Mode.SOFT)
  voice.addTickables(notes)
  const beams = Beam.generateBeams(notes, {
    groups: [new Fraction(1, 4)],       // beam within the beat, as a reader expects
    beam_rests: false,
  })
  return { notes, voice, beams }
}

/**
 * Width the clef and key signature eat at the head of every system, and the
 * extra the time signature takes on the first one. Reserved before packing
 * rather than added after: a system that packs to the full width and *then*
 * grows a clef is a system that overflows the page.
 */
const headWidth = computed(() => {
  const sig = Math.abs(props.score?.sig || 0)
  return 34 + sig * 9 + (sig ? 10 : 0)
})

/**
 * Pack measures into systems: measure how narrow each bar can be, fill a line
 * with as many as fit, then share the leftover width out in proportion so a
 * busy bar stays wider than an empty one.
 */
function packSystems(mins, avail) {
  const out = []
  let line = []
  let used = 0
  for (let i = 0; i < mins.length; i++) {
    const w = mins[i]
    if (line.length && used + w > avail) {
      out.push(line)
      line = []
      used = 0
    }
    line.push(i)
    used += w
  }
  if (line.length) out.push(line)

  return out.map((idx) => {
    const total = idx.reduce((s, i) => s + mins[i], 0)
    // The last system keeps its natural width rather than being stretched to
    // the margin: a final line of one bar spread across the page reads as a
    // mistake, which is why engravers do not do it either.
    const stretch = total < avail * 0.6 && idx === out[out.length - 1] ? 1 : avail / total
    return idx.map((i) => ({ i, w: mins[i] * stretch }))
  })
}

/** Draw the whole score, and record where every moment landed. */
function draw() {
  const host = paper.value
  if (!host) return
  failed.value = ''
  host.innerHTML = ''
  // An empty passage has no systems, so it has nowhere to put a cursor
  // either - leaving the old map in place would park it over blank paper.
  if (!props.score || !measures.value.length || !width.value) {
    systems.value = []
    svgH.value = 0
    return
  }

  const s = props.scale
  const avail = width.value / s - PAD_X * 2
  const nStaves = clefs.value.length
  const time = props.score.time
  const key = props.score.key

  // --- pass 1: how narrow may each measure be? -----------------------------
  // Voices are built once and reused: `preCalculateMinTotalWidth` is meant to
  // be followed by `format` on the same formatter, and rebuilding the notes
  // in between would throw away the stem directions the beams just settled.
  const built = []
  const mins = []
  try {
    for (let m = 0; m < measures.value.length; m++) {
      const meas = measures.value[m]
      const parts = clefs.value.map((clef, si) =>
        buildVoice(meas.voices[si] || [], clef))
      // Which accidentals actually get printed is a convention about the bar
      // (the key signature, and what has already been altered in it), not a
      // fact about the note - so it is VexFlow's call, per stave, since an
      // accidental on one hand does not carry to the other.
      parts.forEach((p) => Accidental.applyAccidentals([p.voice], props.score.key))
      const fmt = new Formatter()
      parts.forEach((p) => fmt.joinVoices([p.voice]))
      const bare = fmt.preCalculateMinTotalWidth(parts.map((p) => p.voice))
      built.push({ meas, parts, fmt })
      mins.push(Math.max(MIN_MEASURE, bare + 26))
    }
  } catch (e) {
    failed.value = String(e.message || e)
    return
  }

  // Every system's first measure carries the clef and key signature; the
  // first system also carries the time signature.
  const head = headWidth.value
  const lines = packSystems(mins, avail - head - 28)
  lines.forEach((line, li) => { line[0].w += head + (li === 0 ? 28 : 0) })

  // Vertical layout runs after packing, because a system's height depends on
  // which measures landed in it.
  const geoms = lines.map((line) => systemGeom(line.map(({ i }) => built[i].meas)))
  const tops = []
  let cursorY = TOP
  geoms.forEach((g) => { tops.push(cursorY); cursorY += g.height + SYSTEM_GAP })
  const totalH = cursorY - SYSTEM_GAP + TOP

  const renderer = new Renderer(host, Renderer.Backends.SVG)
  renderer.resize(width.value, totalH * s)
  const ctx = renderer.getContext()
  ctx.scale(s, s)
  ctx.setFont('Georgia, serif', 12)

  const found = []
  try {
    lines.forEach((line, li) => {
      const y = tops[li]
      const geom = geoms[li]
      let x = PAD_X
      const points = []
      const staveOf = []

      line.forEach(({ i, w }, mi) => {
        const { meas, parts, fmt } = built[i]
        const first = mi === 0
        const staves = clefs.value.map((clef, si) => {
          const stave = new Stave(x, y + geom.tops[si] - STAVE_HEAD, w)
          if (first) {
            stave.addClef(clef).addKeySignature(key)
            if (li === 0) stave.addTimeSignature(time)
          }
          if (first && si === 0) stave.setMeasure(meas.number)
          if (i === measures.value.length - 1) stave.setEndBarType(Barline.type.END)
          return stave
        })

        // Both hands must start their notes at the same x, or a chord split
        // across the staves prints as two events a few pixels apart. The key
        // signature is a different width in each clef, so they do not agree
        // on their own.
        const startX = Math.max(...staves.map((st) => st.getNoteStartX()))
        staves.forEach((st) => st.setNoteStartX(startX))

        parts.forEach((p, si) => p.voice.setStave(staves[si]))
        const justify = staves[0].getNoteEndX() - startX - 12
        fmt.format(parts.map((p) => p.voice), Math.max(24, justify))

        staves.forEach((stave) => stave.setContext(ctx).draw())
        parts.forEach((p, si) => {
          p.voice.draw(ctx, staves[si])
          p.beams.forEach((b) => b.setContext(ctx).draw())
        })

        // Ties, once the notes have x positions. A tie into the next system
        // is dropped rather than drawn across the page break.
        parts.forEach((p, si) => {
          p.notes.forEach((note, ni) => {
            const ev = (meas.voices[si] || [])[ni]
            if (!ev?.tie || !p.notes[ni + 1]) return
            const idx = note.keys.map((_, k) => k)
            new StaveTie({
              first_note: note, last_note: p.notes[ni + 1],
              first_indices: idx, last_indices: idx,
            }).setContext(ctx).draw()
          })
        })

        if (nStaves > 1) staveOf.push(staves)

        // Time -> x, sampled at every event of the top voice plus the bar's
        // own edges, so the cursor moves smoothly between heads instead of
        // hopping from one to the next.
        points.push({ t: meas.start, x: staves[0].getNoteStartX() })
        parts[0].notes.forEach((note, ni) => {
          const ev = (meas.voices[0] || [])[ni]
          if (ev) points.push({ t: ev.start, x: note.getAbsoluteX() })
        })
        points.push({ t: meas.end, x: x + w })

        if (props.showChords && meas.chord) {
          ctx.save()
          ctx.setFont('Georgia, serif', 13, 'bold')
          ctx.fillText(meas.chord, startX - 2, y + geom.chordY)
          ctx.restore()
        }
        x += w
      })

      // The brace and the line down the left edge that make two staves one
      // instrument rather than two.
      if (nStaves > 1 && staveOf.length) {
        const [top, bottom] = [staveOf[0][0], staveOf[0][nStaves - 1]]
        new StaveConnector(top, bottom).setType(StaveConnector.type.BRACE)
          .setContext(ctx).draw()
        new StaveConnector(top, bottom).setType(StaveConnector.type.SINGLE_LEFT)
          .setContext(ctx).draw()
      }

      points.sort((a, b) => a.t - b.t)
      found.push({
        y: y + geom.tops[0] - 14,
        height: geom.height - geom.tops[0] + 28,
        t0: points[0]?.t ?? 0,
        t1: points[points.length - 1]?.t ?? 0,
        points,
      })
    })
  } catch (e) {
    failed.value = String(e.message || e)
    return
  }

  systems.value = found
  svgH.value = totalH * s
}

/** Where a moment sits on the page, in screen pixels. */
const cursor = computed(() => {
  const t = props.cursorTime
  const sys = systems.value
  if (t == null || !sys.length) return null
  let hit = sys.find((sy) => t >= sy.t0 && t < sy.t1)
  if (!hit) {
    if (t < sys[0].t0 || t > sys[sys.length - 1].t1) return null
    hit = sys[sys.length - 1]
  }
  const p = hit.points
  let i = 0
  while (i < p.length - 2 && p[i + 1].t <= t) i++
  const span = p[i + 1].t - p[i].t
  const f = span > 0 ? Math.min(1, Math.max(0, (t - p[i].t) / span)) : 0
  return {
    x: (p[i].x + (p[i + 1].x - p[i].x) * f) * props.scale,
    y: hit.y * props.scale,
    h: hit.height * props.scale,
  }
})

/** Click the page to jump there - the same map, read the other way round. */
function onClick(e) {
  const host = paper.value
  if (!host || !systems.value.length) return
  const r = host.getBoundingClientRect()
  const x = (e.clientX - r.left) / props.scale
  const y = (e.clientY - r.top) / props.scale
  const hit = systems.value.find((sy) => y >= sy.y && y <= sy.y + sy.height)
    || systems.value[0]
  const p = hit.points
  let i = 0
  while (i < p.length - 2 && p[i + 1].x <= x) i++
  const span = p[i + 1].x - p[i].x
  const f = span > 0 ? Math.min(1, Math.max(0, (x - p[i].x) / span)) : 0
  emit('seek', p[i].t + (p[i + 1].t - p[i].t) * f)
}

// Keep the system being played in view. A seek lands far from where the eye
// was, so it jumps; ordinary playback walks one system at a time, so it
// glides - and either way the scroll is only issued when the cursor has left
// the comfortable middle of the box, not on every frame.
watch(cursor, (c) => {
  if (!props.follow || !c || !box.value) return
  const el = box.value
  const top = el.scrollTop
  const h = el.clientHeight
  if (c.y >= top + h * 0.1 && c.y + c.h <= top + h * 0.9) return
  const target = Math.max(0, c.y - h * 0.3)
  el.scrollTo({ top: target, behavior: Math.abs(target - top) > h * 1.5 ? 'auto' : 'smooth' })
})

let ro = null
onMounted(() => {
  ro = new ResizeObserver(([entry]) => {
    const w = Math.round(entry.contentRect.width)
    if (w && w !== width.value) { width.value = w; nextTick(draw) }
  })
  ro.observe(box.value)
  width.value = box.value.clientWidth
  nextTick(draw)
})
onBeforeUnmount(() => ro && ro.disconnect())

watch(() => [props.score, props.scale], () => nextTick(draw))
</script>

<template>
  <div ref="box" class="sheetbox" :class="{ follow }">
    <div v-if="failed" class="fail mono">could not engrave this passage — {{ failed }}</div>
    <div class="paperwrap" :style="{ height: svgH + 'px' }">
      <div ref="paper" class="paper" @click="onClick" />
      <div
        v-if="cursor"
        class="cursor"
        :style="{ left: cursor.x + 'px', top: cursor.y + 'px', height: cursor.h + 'px' }"
      />
    </div>
  </div>
</template>

<style scoped>
/* Sheet music is read off paper, and a dark-on-light staff is what every
   player's eye is trained on - so this pane keeps its own light ground
   rather than inheriting the app's. */
.sheetbox {
  --paper: #f7f5ef;
  --ink: #16130f;
  background: var(--paper);
  border-radius: 6px;
  overflow-y: auto;
  overflow-x: hidden;
  max-height: 62vh;
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.25), 0 2px 14px rgba(0, 0, 0, 0.35);
}

.paperwrap { position: relative; }
.paper { cursor: pointer; }
.paper :deep(svg) { display: block; }

.cursor {
  position: absolute;
  width: 2px;
  background: var(--red-bright, #e8455f);
  box-shadow: 0 0 8px 1px rgba(232, 69, 95, 0.6);
  pointer-events: none;
  transition: left 0.08s linear, top 0.12s ease;
}

.fail {
  padding: 10px 12px;
  font-size: 12px;
  color: #8a2436;
}
</style>
