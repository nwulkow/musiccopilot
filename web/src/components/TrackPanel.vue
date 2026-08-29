<script setup>
/**
 * Correcting which instrument each of an imported multitrack's tracks is.
 *
 * `ImportPanel` shows the same mapping before the import, which is the right
 * moment for "which of these two is the rhythm guitar". It is the wrong moment
 * for the mistakes that only a finished analysis makes visible: a vocal track
 * labelled `guitar` costs you the lyrics, and the first sign of it is a Lyrics
 * tab with nothing in it. Re-importing to fix one row would mean handing the
 * whole multitrack over again, so the stems are relabelled in place.
 *
 * What that costs is worth saying out loud before the button is pressed, and
 * it depends on the change. Moving a track between instruments means the chord
 * detection ran over different audio (`audio.harmonic_bed` is the stems minus
 * drums and vocals) and the notes were read with the wrong instrument's pitch
 * window, so both are read again. Swapping two guitarists' numbers is only a
 * relabelling: the same files stay in the same groups, and only the form -
 * which names a part's lead stem - has to be worked out again.
 */
import { computed, ref } from 'vue'
import { api, STEM_CHOICES } from '../api'

const props = defineProps({
  id: { type: String, required: true },
  sources: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['started', 'error'])

const open = ref(false)
const saving = ref(false)
const draft = ref({})            // track name -> stem, only where it differs

const tracks = computed(() => props.sources?.tracks || [])
const stemOf = (t) => draft.value[t.name] ?? t.stem
const changed = computed(() =>
  tracks.value.filter((t) => stemOf(t) !== t.stem))

/** Whether any change crosses instruments, which is what makes it expensive. */
const instrument = (s) => s.replace(/-\d+$/, '')
const deep = computed(() =>
  changed.value.some((t) => instrument(stemOf(t)) !== instrument(t.stem)))

function set(track, stem) {
  draft.value = { ...draft.value, [track.name]: stem }
}

function reset() {
  draft.value = {}
  open.value = false
}

async function save() {
  const map = Object.fromEntries(changed.value.map((t) => [t.name, stemOf(t)]))
  saving.value = true
  try {
    emit('started', await api.reassign(props.id, map))
    draft.value = {}
    open.value = false
  } catch (e) { emit('error', e.message) } finally { saving.value = false }
}
</script>

<template>
  <div class="tracks">
    <button class="btn btn-sm" @click="open ? reset() : (open = true)">
      <span class="ico">◍</span>
      {{ open ? 'Cancel' : 'Which track is which' }}
    </button>

    <div v-if="open" class="panel card">
      <div class="eyebrow">
        {{ tracks.length }} imported tracks · what each one is playing
      </div>

      <div v-for="t in tracks" :key="t.name" class="row">
        <span class="tname" :title="t.file">{{ t.name }}</span>
        <span class="arrow dim">→</span>
        <select
          class="stemsel" :class="{ moved: stemOf(t) !== t.stem }"
          :value="stemOf(t)" :disabled="saving || busy"
          @change="set(t, $event.target.value)"
        >
          <option v-for="s in STEM_CHOICES" :key="s" :value="s">{{ s }}</option>
        </select>
        <span v-if="stemOf(t) !== t.stem" class="was dim">was {{ t.stem }}</span>
        <span v-else class="why dim">{{ t.why }}</span>
      </div>

      <p v-if="changed.length" class="cost">
        <template v-if="deep">
          The chords, notes, lyrics and form all read the stems by instrument,
          so they are worked out again — about as long as the first analysis,
          minus the separation that imported stems never need.
        </template>
        <template v-else>
          Only the numbering changes, so the same audio stays in the same
          groups: the chords and lyrics stand, and just the form and the chart
          are worked out again.
        </template>
      </p>

      <footer class="foot">
        <span class="muted small">
          The audio is already right — only the labels on it were wrong.
        </span>
        <button
          class="btn btn-primary btn-sm"
          :disabled="!changed.length || saving || busy"
          @click="save"
        >
          {{ saving ? 'Saving…' : `Reassign ${changed.length || ''}`.trim() }}
        </button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.tracks { position: relative; display: inline-block; }
.ico { color: var(--gold); }

.panel {
  /* The button sits at the right of the song header, so the panel hangs from
     that edge - anchored left it runs off the side of the window. */
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 30;
  width: min(600px, 88vw); padding: 13px 15px 14px;
  box-shadow: 0 18px 50px rgba(0, 0, 0, 0.5);
  max-height: 70vh; overflow-y: auto;
}

.eyebrow {
  font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--text-4); margin-bottom: 8px;
}

.row { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 12.5px; }
.tname { flex: none; width: 152px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* A select sizes itself to its longest option, which on a flex row leaves the
   reason column with nothing. Fixed here, so the reason stays readable. */
.stemsel {
  flex: none; width: 106px;
  font-size: 12px; padding: 3px 6px; border-radius: 4px;
  background: var(--surface-2); border: 1px solid var(--line-soft); color: var(--text);
}
.stemsel.moved { border-color: var(--gold-dim); color: var(--gold); }
.why, .was {
  flex: 1; min-width: 0;
  font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.was { color: var(--gold); }

.cost {
  margin: 10px 0 0; padding: 8px 11px; border-radius: 6px; font-size: 11.5px;
  border: 1px solid var(--gold-dim); color: var(--text-2);
  background: rgba(217, 164, 65, 0.09);
}

.foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 11px; }
.small { font-size: 11.5px; }
</style>
