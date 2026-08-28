import { ref, shallowRef, onBeforeUnmount } from 'vue'
import { api } from '../api'

/**
 * One open mic session on the server, as reactive state.
 *
 * The mic belongs to the machine running the Python process - this only
 * opens the socket and holds the newest frame. Frames arrive several times a
 * second and each carries a whole layout, so the payload is kept in
 * `shallowRef`: Vue has no reason to walk a few hundred grid columns looking
 * for changes it will redraw wholesale anyway.
 */
export function useLive() {
  const connected = ref(false)
  const connecting = ref(false)
  const error = ref('')
  const frame = shallowRef(null)
  const saved = ref(null)
  let ws = null

  function start(params) {
    stop()
    connecting.value = true
    error.value = ''
    saved.value = null
    ws = api.liveSocket(params)

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data)
      if (msg.type === 'error') { error.value = msg.message; connecting.value = false; return }
      if (msg.type === 'started') { connected.value = true; connecting.value = false; return }
      if (msg.type === 'saved') { saved.value = msg; return }
      frame.value = msg
    }
    ws.onerror = () => { error.value = error.value || 'could not reach the microphone'; connecting.value = false }
    ws.onclose = () => { connected.value = false; connecting.value = false; ws = null }
  }

  function stop() {
    if (ws) {
      try { ws.send(JSON.stringify({ cmd: 'stop' })) } catch { /* already closing */ }
      ws.close()
      ws = null
    }
    connected.value = false
  }

  function save(name = '') {
    if (ws) ws.send(JSON.stringify({ cmd: 'save', name }))
  }

  onBeforeUnmount(stop)

  return { connected, connecting, error, frame, saved, start, stop, save }
}
