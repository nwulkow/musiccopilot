<script setup>
/**
 * Importing a multitrack the band already recorded, rather than separating a mix.
 *
 * Three doors, because the two DAWs hand over different things and from
 * different places:
 *
 *  - **GarageBand** is *server-side* on purpose. A project is a package, not a
 *    file, so it cannot be uploaded - and it does not need to be: Scriptum runs
 *    on the machine GarageBand is open on (CLAUDE.md, "The mic is the
 *    server's"), so the server can simply look.
 *  - **BandLab** is the opposite case, and the only one that travels. There is
 *    nothing to connect to - BandLab has no public API for pulling a project's
 *    tracks - so the handover is the download, which comes out of a browser on
 *    whatever machine that browser was on. Those files are uploaded.
 *  - **Open from file** is the path field and a real Finder dialog, for
 *    anything already sitting on the server's disk.
 *
 * The mapping is always shown before anything is written. The guesses are good
 * but they are guesses, and the moment to fix "which of these two is the rhythm
 * guitar" is before a five-minute analysis, not after it.
 */
import { ref, computed } from 'vue'
import { api, STEM_CHOICES } from '../api'

const emit = defineEmits(['started', 'error'])

const open = ref(false)
const tab = ref('garageband')      // garageband | bandlab | stems
const scan = ref(null)             // { open[], recent[], blocked, hint, app }
const busy = ref(false)
const err = ref('')
const path = ref('')
const session = ref(null)          // the previewed mapping, before import
const overrides = ref({})
const uploading = ref(null)        // { name, progress }
const dragging = ref(false)

// Shared with `TrackPanel`, which offers the same choice after the import:
// the two have to agree about what a track may become.
const CHOICES = STEM_CHOICES

async function show() {
  open.value = true
  await rescan()
}

async function rescan() {
  busy.value = true
  err.value = ''
  try {
    scan.value = await api.garageband()
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

/** Ask the server to put a real Finder dialog on its own screen. */
async function browse(kind) {
  busy.value = true
  err.value = ''
  try {
    const { path: picked } = await api.dawBrowse(kind)
    if (picked) { path.value = picked; await preview(picked) }
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

/**
 * Open a Finder window on a project macOS will not let Scriptum read.
 *
 * The fix for that block is to drag the project somewhere unprotected, and
 * Scriptum cannot do the drag - it may not copy what it may not read. Pointing
 * at it is the most the app can do, and it works because Finder does the
 * opening.
 */
async function reveal() {
  const row = scan.value?.open?.[0] || scan.value?.recent?.[0]
  if (!row) return
  try { await api.dawReveal(row.path) } catch (e) { err.value = e.message }
}

async function preview(src) {
  busy.value = true
  err.value = ''
  session.value = null
  try {
    session.value = await api.dawPreview(src, overrides.value)
    path.value = src
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

/** Re-preview with a corrected row, so numbering settles around the choice. */
async function setStem(track, stem) {
  overrides.value = { ...overrides.value, [track]: stem }
  await preview(path.value)
}

// --- the uploaded door -------------------------------------------------------

/** Send files to the server, then read them back as a session to be checked. */
async function send(files, name) {
  const list = [...files].filter((f) => f.size > 0)
  if (!list.length) return
  err.value = ''
  session.value = null
  uploading.value = { name: name || `${list.length} files`, progress: 0 }
  try {
    const { path: staged } = await api.dawUpload(list, name,
      (p) => { uploading.value.progress = p })
    await preview(staged)
  } catch (e) { err.value = e.message } finally { uploading.value = null }
}

/** A folder picker names the folder in every file's relative path. */
function pick(event) {
  const files = [...event.target.files]
  const rel = files.find((f) => f.webkitRelativePath)?.webkitRelativePath || ''
  send(files, rel.split('/')[0] || '')
  event.target.value = ''
}

/** Walk a dropped directory. `readEntries` yields at most 100 at a time. */
async function walk(entry, out, depth = 0) {
  if (entry.isFile) {
    out.push(await new Promise((res, rej) => entry.file(res, rej)))
  } else if (entry.isDirectory && depth < 4) {
    const reader = entry.createReader()
    for (;;) {
      const batch = await new Promise((res, rej) => reader.readEntries(res, rej))
      if (!batch.length) break
      for (const e of batch) await walk(e, out, depth + 1)
    }
  }
}

async function drop(event) {
  dragging.value = false
  const dt = event.dataTransfer
  // webkitGetAsEntry has to be read before the first await: the item list is
  // only valid for the duration of the event handler, the entries outlive it.
  const entries = [...(dt.items || [])]
    .map((i) => i.webkitGetAsEntry?.()).filter(Boolean)
  if (!entries.length) return send(dt.files, '')
  const files = []
  for (const e of entries) await walk(e, files)
  const one = entries.length === 1 && entries[0].isDirectory ? entries[0].name : ''
  await send(files, one)
}

async function start() {
  busy.value = true
  err.value = ''
  try {
    const job = await api.dawImport(path.value, overrides.value)
    open.value = false
    session.value = null
    overrides.value = {}
    emit('started', job)
  } catch (e) { err.value = e.message; emit('error', e.message) } finally { busy.value = false }
}

const blocked = computed(() => scan.value?.blocked)
const who = computed(() => scan.value?.app || 'the app running Scriptum')
</script>

<template>
  <div class="importer">
    <button class="btn" @click="open ? (open = false) : show()">
      <span class="gb">◍</span> Import multitrack
    </button>

    <div v-if="open" class="panel card">
      <div class="tabs">
        <button :class="{ on: tab === 'garageband' }" @click="tab = 'garageband'">
          GarageBand
        </button>
        <button :class="{ on: tab === 'bandlab' }" @click="tab = 'bandlab'">
          BandLab
        </button>
        <button :class="{ on: tab === 'stems' }" @click="tab = 'stems'">
          Open from file
        </button>
      </div>

      <div v-if="err" class="err">{{ err }}</div>

      <!-- macOS is refusing the folder: a setting to change, not a failure -->
      <div v-if="blocked && tab === 'garageband'" class="warn">
        <strong>macOS is blocking {{ who }} from reading these.</strong>
        <p>
          The permission belongs to <em>{{ who }}</em>, not to Scriptum — macOS
          files it under whichever app started the server. Two ways through:
        </p>
        <ol>
          <li>
            <strong>No permission at all:</strong> drag the project out of
            <span class="mono">~/Music/GarageBand</span> into
            <span class="mono">Downloads</span> in Finder, then open the copy
            under <a href="#" @click.prevent="tab = 'stems'">Open from file</a>.
          </li>
          <li>
            <strong>Or grant it:</strong> give {{ who }} Full Disk Access in
            System&nbsp;Settings → Privacy&nbsp;&amp;&nbsp;Security and start
            Scriptum again. (Files and Folders has no add button — those rows
            only appear after an app has asked — so Full Disk Access is the one
            you can switch on yourself.)
          </li>
        </ol>
        <div class="pick">
          <button class="btn btn-sm" @click="reveal">Show in Finder</button>
          <button class="btn btn-sm" @click="rescan">Try again</button>
        </div>
      </div>

      <template v-if="tab === 'garageband'">
        <div v-if="busy && !scan" class="muted pad">Looking…</div>
        <template v-else-if="scan">
          <div v-if="scan.open.length" class="group">
            <div class="eyebrow">Open in GarageBand now</div>
            <button
              v-for="p in scan.open" :key="p.path"
              class="row" :disabled="!p.readable" @click="preview(p.path)"
            >
              <span class="dot" />
              <span class="rowname">{{ p.name }}</span>
              <span class="rowpath mono dim">{{ p.path }}</span>
            </button>
          </div>
          <div v-if="scan.recent.length" class="group">
            <div class="eyebrow">Elsewhere on this Mac</div>
            <button
              v-for="p in scan.recent" :key="p.path"
              class="row" :disabled="!p.readable" @click="preview(p.path)"
            >
              <span class="rowname">{{ p.name }}</span>
              <span class="rowpath mono dim">{{ p.path }}</span>
            </button>
          </div>
          <p v-if="!scan.open.length && !scan.recent.length && !blocked" class="muted">
            No GarageBand projects found. Open one in GarageBand, or use
            <a href="#" @click.prevent="tab = 'stems'">Open from file</a>.
          </p>
        </template>
      </template>

      <!-- BandLab: nothing to connect to, so the handover is the download -->
      <template v-else-if="tab === 'bandlab'">
        <p class="muted small">
          BandLab has no public API for pulling a project's tracks, so there is
          no account to connect — the handover is the download, and it is exact:
          one full-length file per track, all starting at zero. Nothing is
          guessed and nothing is separated.
        </p>
        <ol class="steps">
          <li>Open the song in the BandLab web Studio.</li>
          <li>
            Project menu (top left) → <strong>Download</strong> →
            <strong>Tracks</strong>, and choose <strong>WAV</strong>.
          </li>
          <li>Download each track — they land in your Downloads folder.</li>
          <li>Drop them here, or zip the folder and drop the zip.</li>
        </ol>
        <div class="pick">
          <a class="btn btn-sm" href="https://www.bandlab.com/studio"
             target="_blank" rel="noopener">Open BandLab Studio ↗</a>
        </div>

        <div
          class="drop" :class="{ over: dragging }"
          @dragover.prevent="dragging = true"
          @dragleave.prevent="dragging = false"
          @drop.prevent="drop"
        >
          <template v-if="uploading">
            <div class="upname mono">{{ uploading.name }}</div>
            <div class="bar"><div class="fill" :style="{ width: uploading.progress * 100 + '%' }" /></div>
          </template>
          <template v-else>
            <p class="dropline">Drop the tracks, the folder, or a zip here</p>
            <div class="pick center">
              <label class="btn btn-sm">
                Choose files…
                <input type="file" multiple accept="audio/*,.wav,.m4a,.aif,.aiff,.mp3,.flac,.zip" @change="pick" />
              </label>
              <label class="btn btn-sm">
                Choose a folder…
                <input type="file" webkitdirectory directory @change="pick" />
              </label>
            </div>
          </template>
        </div>
        <p class="muted small">
          Already downloaded them onto this Mac? <a href="#" @click.prevent="tab = 'stems'">Open
          from file</a> reads the folder in place, with nothing to upload.
        </p>
      </template>

      <template v-else>
        <p class="muted small">
          A GarageBand project, a folder of exported stems, or a zip of them.
        </p>
        <div class="pick">
          <input
            v-model="path" class="path mono" spellcheck="false"
            placeholder="/Users/you/Music/GarageBand/Practice.band"
            @keyup.enter="preview(path)"
          />
          <button class="btn btn-sm" :disabled="busy" @click="preview(path)">Read</button>
        </div>
        <div class="pick">
          <button class="btn btn-sm" :disabled="busy" @click="browse('band')">
            Browse for a project…
          </button>
          <button class="btn btn-sm" :disabled="busy" @click="browse('folder')">
            Browse for a stems folder…
          </button>
        </div>
      </template>

      <!-- The mapping, always shown before a byte is written -->
      <div v-if="session" class="mapping">
        <div class="eyebrow">
          {{ session.tracks.length }} tracks · check what each one becomes
        </div>
        <div v-for="w in session.warnings" :key="w" class="warnline">! {{ w }}</div>
        <div v-for="t in session.tracks" :key="t.name" class="track">
          <span class="tname">{{ t.name }}</span>
          <span class="arrow dim">→</span>
          <select
            class="stemsel" :value="t.stem"
            @change="setStem(t.name, $event.target.value)"
          >
            <option v-for="s in CHOICES" :key="s" :value="s">{{ s }}</option>
          </select>
          <span class="why dim">{{ t.why }}</span>
        </div>
        <footer class="foot">
          <span class="muted small">
            No stem separation needed — these are the real tracks.
          </span>
          <button class="btn btn-primary btn-sm" :disabled="busy" @click="start">
            Import and analyse
          </button>
        </footer>
      </div>
    </div>
  </div>
</template>

<style scoped>
.importer { position: relative; }
.gb { color: var(--gold); }

.panel {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 30;
  width: min(660px, 88vw); padding: 14px 16px 16px;
  box-shadow: 0 18px 50px rgba(0, 0, 0, 0.5);
  max-height: 80vh; overflow-y: auto;
}

.tabs { display: flex; gap: 4px; margin-bottom: 12px; }
.tabs button {
  font-size: 12.5px; padding: 5px 11px; border-radius: 5px;
  color: var(--text-3); background: transparent; border: 1px solid transparent;
}
.tabs button.on { color: var(--text); background: var(--surface-2); border-color: var(--line-soft); }

.eyebrow { font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-4); margin: 10px 0 6px; }
.group + .group { margin-top: 4px; }

.row {
  display: flex; align-items: center; gap: 9px; width: 100%;
  padding: 7px 9px; border-radius: 6px; text-align: left;
  background: transparent; border: 1px solid var(--line-soft); margin-bottom: 5px;
}
.row:hover:not(:disabled) { border-color: var(--red-bright); background: rgba(168, 23, 47, 0.08); }
.row:disabled { opacity: 0.45; cursor: not-allowed; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); flex: none; }
.rowname { font-size: 13.5px; }
.rowpath { font-size: 11px; margin-left: auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 55%; }

.pick { display: flex; gap: 7px; margin-top: 8px; }
.pick.center { justify-content: center; }
.path {
  flex: 1; font-size: 12px; padding: 6px 9px; border-radius: 5px;
  background: var(--bg-deep); border: 1px solid var(--line-soft); color: var(--text);
}

/* The BandLab door: steps, then somewhere to put what they produced. */
.steps { margin: 10px 0 4px; padding-left: 20px; font-size: 12.5px; color: var(--text-2); }
.steps li { margin-bottom: 4px; }
.steps strong, .warn strong { color: var(--text); }

.drop {
  margin-top: 12px; padding: 18px 14px; border-radius: 8px; text-align: center;
  border: 1px dashed var(--line-soft); background: var(--bg-deep);
}
.drop.over { border-color: var(--red-bright); background: rgba(168, 23, 47, 0.08); }
.dropline { margin: 0 0 10px; font-size: 12.5px; color: var(--text-3); }
.drop label { position: relative; overflow: hidden; cursor: pointer; }
.drop input[type="file"] { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.upname { font-size: 12px; margin-bottom: 8px; }
.bar { height: 4px; border-radius: 2px; background: var(--surface-2); overflow: hidden; }
.fill { height: 100%; background: var(--red-bright); transition: width 0.15s linear; }

.mapping { margin-top: 14px; border-top: 1px solid var(--line-soft); padding-top: 10px; }
.track { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 12.5px; }
.tname { min-width: 145px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stemsel {
  font-size: 12px; padding: 3px 6px; border-radius: 4px;
  background: var(--surface-2); border: 1px solid var(--line-soft); color: var(--text);
}
.why { font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.warnline { font-size: 11.5px; color: var(--gold); margin-bottom: 4px; }

.foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; }
.small { font-size: 11.5px; }
.pad { padding: 14px 0; }

.err, .warn {
  padding: 9px 12px; border-radius: 6px; font-size: 12.5px; margin-bottom: 10px;
  border: 1px solid var(--red-deep); color: #f0a8b4; background: rgba(168, 23, 47, 0.12);
}
.warn { border-color: var(--gold-dim); color: var(--gold); background: rgba(217, 164, 65, 0.09); }
.warn p { margin: 5px 0 6px; color: var(--text-2); }
.warn ol { margin: 0 0 4px; padding-left: 18px; color: var(--text-2); }
.warn li { margin-bottom: 5px; }
</style>
