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

  song: (id) => req(`/api/songs/${encodeURIComponent(id)}`),
  analyze: (id, opts = {}) => req(`/api/songs/${encodeURIComponent(id)}/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
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

export const stemMeta = (s) =>
  STEM_META[s] || { label: s, color: '#9a8085', fretted: false }
