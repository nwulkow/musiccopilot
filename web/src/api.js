// Every call the client makes to the Python side, in one place.
//
// Paths are relative so the same build works behind Vite's dev proxy and
// served straight out of FastAPI.

async function req(url, opts = {}) {
  const res = await fetch(url, opts)
  if (!res.ok) {
    let detail
    try { detail = (await res.json()).detail } catch { detail = await res.text() }
    const err = new Error(typeof detail === 'string' ? detail : (detail?.error || res.statusText))
    err.status = res.status
    err.detail = detail
    throw err
  }
  return res.status === 204 ? null : res.json()
}

const post = (url, body) => req(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

const qs = (params) => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== null && v !== undefined && v !== '') p.set(k, v)
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const api = {
  health: () => req('/api/health'),
  devices: () => req('/api/devices'),
  transcribers: () => req('/api/transcribers'),

  library: () => req('/api/library'),
  upload(file, onProgress) {
    // XHR rather than fetch: an upload of a 40MB wav wants a progress bar,
    // and fetch still cannot report request progress.
    return new Promise((resolve, reject) => {
      const form = new FormData()
      form.append('file', file)
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/library')
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total)
      }
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText))
        else {
          let msg = xhr.statusText
          try { msg = JSON.parse(xhr.responseText).detail || msg } catch { /* keep statusText */ }
          reject(new Error(msg))
        }
      }
      xhr.onerror = () => reject(new Error('upload failed'))
      xhr.send(form)
    })
  },
  remove: (id) => req(`/api/library/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // Importing a DAW multitrack. All of these are about the *server's* own
  // filesystem: a GarageBand project is a package that cannot be uploaded,
  // and the machine running Scriptum is the machine it is sitting on.
  garageband: () => req('/api/daw/garageband'),
  dawBrowse: (kind) => post('/api/daw/browse', { kind }),
  dawReveal: (path) => post('/api/daw/reveal', { path }),
  // ...except this one. BandLab's tracks come out of a browser, on whatever
  // machine that browser is on, so they are the one case that has to travel.
  // Same XHR-for-progress reason as `upload`, several hundred MB at a time.
  dawUpload(files, name, onProgress) {
    return new Promise((resolve, reject) => {
      const form = new FormData()
      for (const f of files) form.append('files', f, f.name)
      form.append('name', name || '')
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/daw/upload')
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total)
      }
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText))
        else {
          let msg = xhr.statusText
          try { msg = JSON.parse(xhr.responseText).detail || msg } catch { /* keep statusText */ }
          reject(new Error(msg))
        }
      }
      xhr.onerror = () => reject(new Error('upload failed'))
      xhr.send(form)
    })
  },
  dawPreview: (path, map = {}) => post('/api/daw/preview', { path, map }),
  dawImport: (path, map = {}, opts = {}) =>
    post('/api/daw/import', { path, map, ...opts }),
  // Correcting a row after the fact. A wrong label is not always visible until
  // the analysis comes back - a vocal track read as a guitar shows up as an
  // empty Lyrics tab - and re-importing to fix one row would mean handing the
  // whole multitrack over again.
  reassign: (id, map, opts = {}) =>
    post(`/api/songs/${encodeURIComponent(id)}/tracks`, { map, ...opts }),

  song: (id) => req(`/api/songs/${encodeURIComponent(id)}`),
  analyze: (id, opts = {}) => req(`/api/songs/${encodeURIComponent(id)}/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
  }),
  // Re-read the notes with another engine. Cheaper than a re-analysis - the
  // stems, chords and form are untouched - but still a job, not a request.
  transcribe: (id, body) => req(`/api/songs/${encodeURIComponent(id)}/transcribe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  chart: (id) => req(`/api/songs/${encodeURIComponent(id)}/chart`),
  chords: (id) => req(`/api/songs/${encodeURIComponent(id)}/chords`),
  tab: (id, params) => req(`/api/songs/${encodeURIComponent(id)}/tab${qs(params)}`),
  score: (id, params) => req(`/api/songs/${encodeURIComponent(id)}/score${qs(params)}`),
  cleanTab: (id, body) => req(`/api/songs/${encodeURIComponent(id)}/tab/clean`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  notes: (id, params) => req(`/api/songs/${encodeURIComponent(id)}/notes${qs(params)}`),
  solo: (id, body) => req(`/api/songs/${encodeURIComponent(id)}/solo`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),

  jobs: () => req('/api/jobs'),
  job: (jid) => req(`/api/jobs/${jid}`),

  media: {
    mix: (id) => `/api/songs/${encodeURIComponent(id)}/media/mix`,
    stem: (id, stem) => `/api/songs/${encodeURIComponent(id)}/media/stem/${stem}`,
    snippet: (id, name) => `/api/songs/${encodeURIComponent(id)}/media/snippet/${name}`,
    file: (id, name) => `/api/songs/${encodeURIComponent(id)}/media/file/${name}`,
    backing: (id, exclude) =>
      `/api/songs/${encodeURIComponent(id)}/media/backing${qs({ exclude })}`,
  },

  liveSocket(params) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    return new WebSocket(`${proto}//${location.host}/ws/live${qs(params)}`)
  },
}

// Follow a job's progress over SSE. Returns a stop() that closes the stream.
export function followJob(jobId, { onLine, onEnd, onError } = {}) {
  const src = new EventSource(`/api/jobs/${jobId}/stream`)
  src.addEventListener('log', (e) => onLine && onLine(JSON.parse(e.data).line))
  src.addEventListener('end', (e) => {
    onEnd && onEnd(JSON.parse(e.data))
    src.close()
  })
  src.onerror = () => { onError && onError(); src.close() }
  return () => src.close()
}

// --- small shared formatters ------------------------------------------------

export const mmss = (t) => {
  if (t == null || !isFinite(t)) return '–:––'
  const s = Math.max(0, Math.floor(t))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export const bytes = (n) => {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)))
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`
}

// The stem names a track can be assigned to, at import and afterwards. A band
// has two guitarists and the six separation names cannot say that on their own,
// so the suffixed slots are offered explicitly rather than left to be typed.
const ASSIGNABLE = ['guitar', 'bass', 'drums', 'vocals', 'piano', 'other']
export const STEM_CHOICES = [...ASSIGNABLE, ...ASSIGNABLE.map((s) => `${s}-2`),
                             'guitar-3', 'vocals-3']

// Stems, in the order a band reads them, with the colour each gets everywhere.
export const STEM_META = {
  guitar: { label: 'Guitar', color: '#e0573f', fretted: true },
  bass: { label: 'Bass', color: '#c78a3a', fretted: true },
  piano: { label: 'Piano', color: '#7fa7d4', fretted: false },
  vocals: { label: 'Vocals', color: '#d8607d', fretted: false },
  other: { label: 'Other', color: '#8d7fa8', fretted: false },
  drums: { label: 'Drums', color: '#6f8b7d', fretted: false },
  mix: { label: 'Mix', color: '#9a8085', fretted: false },
}

/**
 * What a stem is, for any stem name - including a suffixed one.
 *
 * This is `config.base_stem` on the client side, and it has to be: an
 * imported multitrack has two guitarists, so a stem may be `guitar-2`, which
 * is a guitar in every way that matters here (it has a fretboard, and it
 * belongs beside guitar in the palette) while staying its own stem with its
 * own notes and its own tab. An exact-match lookup made `guitar-2` fall to
 * the unknown-stem default, `fretted: false` - so the Tabs and play-along
 * pages dropped the Tab button for the second guitarist and read a guitar as
 * sheet music. The number stays in the label, because two buttons both
 * saying "Guitar" is no better.
 */
const STEM_SUFFIX = /-(\d+)$/

export const stemMeta = (s) => {
  const m = STEM_SUFFIX.exec(s)
  const meta = STEM_META[m ? s.slice(0, m.index) : s]
  if (!meta) return { label: s, color: '#9a8085', fretted: false }
  return m ? { ...meta, label: `${meta.label} ${m[1]}` } : meta
}
