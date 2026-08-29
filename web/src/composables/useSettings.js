// Client-side preferences, persisted in localStorage.
//
// One shared reactive object rather than a per-component ref: the settings
// pane writes the transcription engine and the song pages read it, and two
// copies would disagree until a reload. Nothing here is a musical decision -
// the engine names are the ones `/api/transcribers` reports, and what each
// one means is decided in `musiccopilot.notes`.

import { reactive, ref, watch } from 'vue'
import { api } from '../api'

const KEY = 'scriptum.settings'

const DEFAULTS = {
  // Which note transcriber a new analysis uses. Null means "whatever the
  // server calls its default", so a server-side change of default is picked
  // up rather than frozen into every browser that ever loaded the app.
  transcriber: null,

  // Whether an analysis also asks Gemini for listening notes. Off, because it
  // is the one call whose cost is set by the length of the song rather than
  // the size of a passage - it uploads the whole mp3, which Gemini bills per
  // second of audio. The CLI has always made it opt-in (`analyze --llm`); this
  // is the browser having the same opinion instead of quietly saying yes.
  llmNotes: false,
}

function load() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || '{}') }
  } catch {
    return { ...DEFAULTS }     // private window, cleared storage, blocked site data
  }
}

export const settings = reactive(load())

watch(settings, (s) => {
  try { localStorage.setItem(KEY, JSON.stringify(s)) } catch { /* not worth failing over */ }
}, { deep: true })

export function useSettings() {
  return settings
}

// --- what this install can actually run --------------------------------------
//
// Fetched once for the whole app and shared: the settings pane lists the
// engines and every song page needs the same names and the same server-side
// default, and two fetches would be two chances to disagree.

const backends = ref([])
const serverDefault = ref('')
const loaded = ref(false)
let inflight = null

export function useTranscribers() {
  if (!inflight) {
    inflight = api.transcribers()
      .then((r) => { backends.value = r.backends; serverDefault.value = r.default })
      .catch(() => { /* the pane shows an empty list; nothing else breaks */ })
      .finally(() => { loaded.value = true })
  }
  return { backends, serverDefault, loaded }
}

/** The engine a new analysis will use: the stored choice, else the server's. */
export function chosenTranscriber() {
  return settings.transcriber || serverDefault.value
}

/** A backend's display label, falling back to its bare name. */
export function transcriberLabel(name) {
  return backends.value.find((b) => b.name === name)?.label || name
}
