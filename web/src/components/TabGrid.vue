<script setup>
/**
 * Draws a tab or a staff from the layout the Python side laid out.
 *
 * Deliberately dumb about music: every column already carries its absolute
 * time, its bar number and its cells, because `TabLayout`/`StaffLayout`
 * computed them. Re-deriving any of that here would be a second
 * implementation of `col_of` to keep in step with the first, which is exactly
 * what the project's notes warn against. So this component only maps
 * `column.i` to an x position and `cell.row` to a y position.
 *
 * Columns are drawn at a uniform width, which the ASCII renderer cannot do
 * (it sizes each column to its widest cell). On screen that is an
 * improvement: uniform columns make the x axis proportional to time, so
 * spacing reads as rhythm.
 */
import { computed, ref, watch, nextTick } from 'vue'

const props = defineProps({
  layout: { type: Object, required: true },
  cursorTime: { type: Number, default: null },   // absolute seconds, or null
  colWidth: { type: Number, default: 17 },
  follow: { type: Boolean, default: false },     // keep the cursor in view
  showChords: { type: Boolean, default: true },
  showBars: { type: Boolean, default: true },
  compact: { type: Boolean, default: false },
})
const emit = defineEmits(['seek'])

const scroller = ref(null)

const rowH = computed(() => (props.compact ? 17 : 21))
const gutter = 30

const cols = computed(() => props.layout?.columns || [])
const rows = computed(() => props.layout?.rows || [])
const isStaff = computed(() => props.layout?.kind === 'staff')
const width = computed(() => gutter + cols.value.length * props.colWidth)
const bodyH = computed(() => rows.value.length * rowH.value)

/** Bar lines, taken from the columns the layout marked as bar starts. */
const barLines = computed(() =>
  cols.value.filter((c) => c.bar_start).map((c) => ({
    x: gutter + c.i * props.colWidth,
    bar: c.bar,
    i: c.i,
  })),
)

/** Chord changes, at the column the layout put them on. */
const chordMarks = computed(() =>
  cols.value.filter((c) => c.chord).map((c) => ({
    x: gutter + c.i * props.colWidth,
    name: c.chord,
  })),
)

/** Every cell, flattened with its screen position. */
const cells = computed(() => {
  const out = []
  for (const c of cols.value) {
    for (const cell of c.cells) {
      out.push({
        ...cell,
        col: c.i,
        x: gutter + c.i * props.colWidth,
        y: cell.row * rowH.value,
        key: `${c.i}-${cell.row}-${cell.pitch}`,
      })
    }
  }
  return out
})

/** Which column the cursor sits on - a plain scan over times the layout gave us. */
const cursorCol = computed(() => {
  const t = props.cursorTime
  if (t == null || !cols.value.length) return null
  const first = cols.value[0].t
  const step = cols.value.length > 1 ? cols.value[1].t - first : 0.1
  const i = Math.floor((t - first) / step + 0.5)
  return i >= 0 && i < cols.value.length ? i : null
})

const cursorX = computed(() =>
  cursorCol.value == null ? null : gutter + cursorCol.value * props.colWidth,
)

/** Notes sounding at the cursor, for the "play this now" highlight. */
const activeKeys = computed(() => {
  const t = props.cursorTime
  if (t == null) return new Set()
  return new Set(cells.value.filter((c) => c.start <= t && t < c.end).map((c) => c.key))
})

// Keep the cursor on screen while playing, scrolling in steps rather than
// continuously so the tab does not slide under the eye on every frame.
watch(cursorX, async (x) => {
  if (!props.follow || x == null || !scroller.value) return
  await nextTick()
  const box = scroller.value
  const left = box.scrollLeft
  const w = box.clientWidth
  if (x < left + w * 0.15 || x > left + w * 0.7) {
    box.scrollTo({ left: Math.max(0, x - w * 0.3), behavior: 'smooth' })
  }
})

function onClick(e) {
  const box = scroller.value
  if (!box) return
  const x = e.clientX - box.getBoundingClientRect().left + box.scrollLeft - gutter
  const i = Math.round(x / props.colWidth)
  const col = cols.value[Math.min(cols.value.length - 1, Math.max(0, i))]
  if (col) emit('seek', col.t)
}

/** A cell's colour: technique is the thing worth seeing at a glance. */
function cellClass(cell) {
  return [
    'cell',
    `tech-${cell.technique}`,
    activeKeys.value.has(cell.key) ? 'is-active' : '',
  ]
}
</script>

<template>
  <div class="tabgrid" :class="{ staff: isStaff }">
    <div ref="scroller" class="scroll" @click="onClick">
      <div class="canvas" :style="{ width: width + 'px' }">

        <!-- chord row -->
        <div v-if="showChords" class="chordrow" :style="{ height: '20px' }">
          <span
            v-for="m in chordMarks"
            :key="'ch' + m.x"
            class="chordmark"
            :style="{ left: m.x + 'px' }"
          >{{ m.name }}</span>
        </div>

        <!-- the grid itself -->
        <div class="body" :style="{ height: bodyH + 'px' }">
          <!-- string / staff lines -->
          <div
            v-for="(r, ri) in rows"
            :key="'r' + ri"
            class="row"
            :class="{ line: r.line, ledger: isStaff && r.line && !r.staff }"
            :style="{ top: ri * rowH + 'px', height: rowH + 'px' }"
          >
            <span class="rowlabel">{{ r.label }}</span>
          </div>

          <!-- bar lines -->
          <div
            v-for="b in barLines"
            :key="'b' + b.i"
            class="barline"
            :style="{ left: b.x + 'px' }"
          />

          <!-- cursor -->
          <div v-if="cursorX != null" class="cursor" :style="{ left: cursorX + 'px' }" />

          <!-- cells -->
          <span
            v-for="cell in cells"
            :key="cell.key"
            :class="cellClass(cell)"
            :style="{ left: cell.x + 'px', top: cell.y + 'px', height: rowH + 'px' }"
            :title="`${cell.name} · ${cell.technique}${cell.bend ? ' ' + cell.bend + '↑' : ''}`"
          >{{ cell.text }}</span>
        </div>

        <!-- bar numbers -->
        <div v-if="showBars" class="barrow">
          <span
            v-for="b in barLines"
            :key="'n' + b.i"
            class="barnum mono"
            :style="{ left: b.x + 3 + 'px' }"
          >{{ b.bar }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tabgrid { position: relative; }

.scroll {
  overflow-x: auto;
  overflow-y: hidden;
  cursor: pointer;
  padding-bottom: 2px;
}

.canvas { position: relative; min-width: 100%; }

/* --- chords -------------------------------------------------------------- */
.chordrow { position: relative; }
.chordmark {
  position: absolute;
  top: 0;
  transform: translateX(-1px);
  font-size: 11.5px;
  font-weight: 600;
  color: var(--gold);
  letter-spacing: 0.01em;
  white-space: nowrap;
  pointer-events: none;
}

/* --- grid ---------------------------------------------------------------- */
.body { position: relative; }

.row { position: absolute; left: 0; right: 0; }
.row.line::after {
  content: '';
  position: absolute;
  left: 30px;
  right: 0;
  top: 50%;
  height: 1px;
  background: var(--line);
}
/* A ledger row is outside the five printed lines: drawn fainter so the staff
   itself still reads as five lines. */
.row.ledger::after { background: var(--line-soft); }
.staff .row:not(.line)::after { content: none; }

.rowlabel {
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 24px;
  text-align: right;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-4);
  pointer-events: none;
}

.barline {
  position: absolute;
  top: -2px;
  bottom: -2px;
  width: 1px;
  background: var(--line-strong);
  pointer-events: none;
}

.cursor {
  position: absolute;
  top: -4px;
  bottom: -4px;
  width: 2px;
  background: var(--red-glow);
  box-shadow: 0 0 10px 1px rgba(255, 69, 107, 0.7);
  pointer-events: none;
  z-index: 3;
  transition: left 0.06s linear;
}

/* --- cells --------------------------------------------------------------- */
.cell {
  position: absolute;
  display: flex;
  align-items: center;
  justify-content: center;
  transform: translateX(-50%);
  padding: 0 3px;
  min-width: 15px;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  color: var(--text);
  background: var(--surface);
  border-radius: 3px;
  white-space: nowrap;
  pointer-events: none;
  z-index: 2;
}

/* Technique is the one thing a player needs to spot without reading: a bend
   and a plain fret must not look the same at a glance. */
.tech-bend { color: var(--ember); }
.tech-vibrato { color: #eab04a; }
.tech-slide { color: #9fc6e8; }
.tech-hammer, .tech-pull { color: #8fd0a8; }
.tech-palm_mute { color: var(--text-3); }

.cell.is-active {
  color: #fff;
  background: var(--red);
  box-shadow: 0 0 12px rgba(211, 39, 66, 0.75);
  z-index: 4;
}

/* --- bar numbers --------------------------------------------------------- */
.barrow { position: relative; height: 16px; }
.barnum {
  position: absolute;
  top: 1px;
  font-size: 10px;
  color: var(--text-4);
  pointer-events: none;
}
</style>
