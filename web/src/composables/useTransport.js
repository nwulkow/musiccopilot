import { ref, shallowRef, watch, onBeforeUnmount } from 'vue'

/**
 * One audio element driving every tab on screen.
 *
 * The play-along is the reason this is a single shared clock rather than a
 * per-pane one: two guitar tabs and a bass tab have to sit on the *same*
 * moment, and two <audio> elements started together drift apart. So one
 * element is the transport and every cursor reads its `currentTime`.
 *
 * Position is sampled on requestAnimationFrame from the element itself,
 * never from a `Date.now()` clock started next to it - the same reason
 * `playalong.Transport` reads the audio callback's frame counter rather
 * than a wall clock, and the same failure if you don't (a cursor a beat
 * away from what you hear by the end of a solo).
 */
export function useTransport() {
  const el = shallowRef(null)
  const src = ref('')
  const playing = ref(false)
  const time = ref(0)
  const duration = ref(0)
  const rate = ref(1)
  const volume = ref(1)
  const ready = ref(false)
  const error = ref('')
  const loop = ref(false)
  const region = ref(null)      // { start, end } - the passage being practised
  const countIn = ref(0)        // beats of click before playback starts
  const countingIn = ref(0)     // beats remaining, for the UI
  const tempo = ref(120)

  let raf = 0
  let audioCtx = null

  function audio() {
    if (!el.value) {
      const a = new Audio()
      a.preload = 'auto'
      a.crossOrigin = 'anonymous'
      // Slowing a passage down for practice must not drop the pitch - you are
      // playing along with it. This is the browser's version of the CLI's
      // librosa time-stretch.
      a.preservesPitch = true
      a.addEventListener('loadedmetadata', () => {
        duration.value = a.duration || 0
        ready.value = true
      })
      a.addEventListener('play', () => { playing.value = true; tick() })
      a.addEventListener('pause', () => { playing.value = false })
      a.addEventListener('ended', () => { playing.value = false })
      a.addEventListener('error', () => { error.value = 'could not load audio'; ready.value = false })
      el.value = a
    }
    return el.value
  }

  function tick() {
    const a = el.value
    if (!a) return
    time.value = a.currentTime
    const r = region.value
    if (r && a.currentTime >= r.end - 0.02) {
      if (loop.value) a.currentTime = r.start
      else { a.pause(); a.currentTime = r.start; time.value = r.start }
    }
    if (!a.paused) raf = requestAnimationFrame(tick)
  }

  function load(url, { region: reg = null, autoplay = false } = {}) {
    const a = audio()
    error.value = ''
    if (src.value !== url) {
      src.value = url
      ready.value = false
      a.src = url
    }
    region.value = reg
    if (reg) {
      const seek = () => { a.currentTime = reg.start; time.value = reg.start }
      if (a.readyState >= 1) seek()
      else a.addEventListener('loadedmetadata', seek, { once: true })
    }
    if (autoplay) play()
  }

  /** A metronome click, synthesised rather than fetched - it must be exact. */
  function click(at, accent) {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)()
    const osc = audioCtx.createOscillator()
    const gain = audioCtx.createGain()
    osc.frequency.value = accent ? 1600 : 1050
    gain.gain.setValueAtTime(0.0001, at)
    gain.gain.exponentialRampToValueAtTime(0.35, at + 0.002)
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.07)
    osc.connect(gain).connect(audioCtx.destination)
    osc.start(at)
    osc.stop(at + 0.09)
  }

  async function play() {
    const a = audio()
    if (!a.src) return
    // The count-in is generated at the tempo you will actually hear, i.e.
    // after the rate change - clicking at the written tempo and then playing
    // back slowed is the one thing a count-in must not do.
    if (countIn.value > 0) {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)()
      await audioCtx.resume()
      const spb = 60 / (tempo.value * rate.value)
      const t0 = audioCtx.currentTime + 0.06
      for (let i = 0; i < countIn.value; i++) click(t0 + i * spb, i % 4 === 0)
      countingIn.value = countIn.value
      const iv = setInterval(() => { countingIn.value = Math.max(0, countingIn.value - 1) }, spb * 1000)
      await new Promise((r) => setTimeout(r, countIn.value * spb * 1000))
      clearInterval(iv)
      countingIn.value = 0
    }
    try { await a.play() } catch (e) { error.value = String(e.message || e) }
  }

  function pause() { el.value && el.value.pause() }
  function toggle() { playing.value ? pause() : play() }

  function seek(t) {
    const a = audio()
    const r = region.value
    const lo = r ? r.start : 0
    const hi = r ? r.end : (duration.value || Infinity)
    a.currentTime = Math.min(hi, Math.max(lo, t))
    time.value = a.currentTime
  }

  function stop() {
    pause()
    seek(region.value ? region.value.start : 0)
  }

  watch(rate, (v) => { if (el.value) el.value.playbackRate = v })
  watch(volume, (v) => { if (el.value) el.value.volume = v })

  onBeforeUnmount(() => {
    cancelAnimationFrame(raf)
    if (el.value) { el.value.pause(); el.value.src = '' }
    if (audioCtx) audioCtx.close()
  })

  return {
    src, playing, time, duration, rate, volume, ready, error,
    loop, region, countIn, countingIn, tempo,
    load, play, pause, toggle, seek, stop,
  }
}
