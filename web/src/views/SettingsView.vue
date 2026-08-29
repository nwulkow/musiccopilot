<script setup>
import { computed, inject } from 'vue'
import { useSettings, useTranscribers } from '../composables/useSettings'

// The engine list comes from the server, not from a copy kept here: whether
// torchcrepe imports is a fact about the machine Scriptum runs on, and a
// hardcoded list would offer an engine that cannot run (or hide one that can).
const settings = useSettings()
const { backends, serverDefault, loaded } = useTranscribers()

const loading = computed(() => !loaded.value)
const err = computed(() => (loaded.value && !backends.value.length
  ? 'could not reach the server' : ''))

// `null` in the setting means "follow the server", so a change of default on
// the Python side reaches a browser that has already stored a preference.
const chosen = computed(() => settings.transcriber || serverDefault.value)

function pick(name) {
  settings.transcriber = name === serverDefault.value ? null : name
}

// The cleanup cap is the server's number (`musiccopilot.config`), reported on
// the health endpoint so this pane does not keep a second copy that drifts.
const health = inject('health')
const maxNotes = computed(() => health.value?.clean_limit?.max_notes ?? 250)
const maxSeconds = computed(() => Math.round(health.value?.clean_limit?.max_seconds ?? 75))
</script>

<template>
  <div class="wrap">
    <header class="head">
      <h1 class="title">Settings</h1>
      <p class="muted sub">
        Stored in this browser. Changing an engine here does not touch a song
        that is already analysed &mdash; open the song and re-transcribe it.
      </p>
    </header>

    <section class="card block">
      <div class="eyebrow">Transcription engine</div>
      <p class="muted lead">
        Which tracker turns a separated stem into notes. It is the whole
        difference between a bend and four notes, and between a chord and its
        top string &mdash; so there is no single right answer for every part.
      </p>

      <div v-if="loading" class="muted pad">Loading engines…</div>
      <div v-else-if="err" class="err">{{ err }}</div>

      <div v-else class="opts">
        <button
          v-for="b in backends" :key="b.name"
          class="opt"
          :class="{ on: chosen === b.name, off: !b.available }"
          :disabled="!b.available"
          @click="pick(b.name)"
        >
          <span class="radio" :class="{ on: chosen === b.name }" />
          <span class="body">
            <span class="row">
              <span class="name">{{ b.label }}</span>
              <span class="chip">{{ b.kind }}</span>
              <span v-if="b.name === serverDefault" class="chip chip-gold">default</span>
              <span v-if="!b.available" class="chip chip-red">
                needs {{ b.missing.join(', ') }}
              </span>
            </span>
            <span class="why muted">{{ b.summary }}</span>
          </span>
        </button>
      </div>
    </section>

    <section class="card block">
      <div class="eyebrow">Gemini</div>
      <p class="muted lead">
        What the AI features are allowed to cost. Everything else in this app is
        computed locally &mdash; these are the only buttons that spend money.
      </p>

      <button class="opt" :class="{ on: settings.llmNotes }" @click="settings.llmNotes = !settings.llmNotes">
        <span class="radio sq" :class="{ on: settings.llmNotes }" />
        <span class="body">
          <span class="row">
            <span class="name">Listening notes during analysis</span>
            <span class="chip">{{ settings.llmNotes ? 'on' : 'off' }}</span>
          </span>
          <span class="why muted">
            Uploads the whole track to Gemini once per song and asks it to
            describe the groove, form and scales. It is the one call billed by
            the length of the song rather than the size of a passage &mdash; a
            4-minute track is roughly 8,000 tokens of audio before the prompt
            &mdash; so analysis leaves it off unless you ask. The answer is
            cached in <code class="mono">llm_notes.txt</code>, so it is paid for
            once, and the solo generator uses it for style when it is there.
          </span>
        </span>
      </button>

      <p class="muted lead note">
        <strong>Clean up</strong> on the Tabs page is capped to a passage
        &mdash; {{ maxNotes }} notes or {{ maxSeconds }} seconds. The model is
        sent the notes and writes them back, so a window is billed twice over,
        and a whole song is around a hundred times a solo. Pick a part or a bar
        range and the button lights up.
      </p>
    </section>

    <section class="card block">
      <div class="eyebrow">Not offered</div>
      <p class="muted lead">
        <strong>Omnizart</strong> is not in the list because it cannot run on
        this install. It needs <code class="mono">madmom</code>, which imports
        <code class="mono">MutableSequence</code> from
        <code class="mono">collections</code> &mdash; removed in Python 3.10
        &mdash; and TensorFlow&nbsp;2.5, which has no wheel for Python&nbsp;3.11.
        MusicCopilot is pinned to 3.11 by demucs and Basic Pitch, so the two
        requirements have no version in common.
      </p>
    </section>
  </div>
</template>

<style scoped>
.wrap { padding: 22px 30px 60px; max-width: 860px; }
.title { font-family: var(--font-display); font-size: 29px; }
.sub { margin-top: 6px; font-size: 13px; max-width: 62ch; }
.head { margin-bottom: 20px; }

.block { padding: 20px 22px; margin-bottom: 16px; }
.lead { font-size: 13px; margin: 8px 0 16px; max-width: 66ch; line-height: 1.55; }
.pad { padding: 10px 0; }
.err { color: #f0a8b4; font-size: 13px; }

.opts { display: flex; flex-direction: column; gap: 7px; }
.opt {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  width: 100%;
  text-align: left;
  padding: 12px 14px;
  background: var(--bg-deep);
  border: 1px solid var(--line-soft);
  border-radius: var(--r);
  color: var(--text);
  cursor: pointer;
  font: inherit;
  transition: border-color 0.14s, background 0.14s;
}
.opt:hover:not(:disabled) { background: var(--surface-2); border-color: var(--line-strong); }
.opt.on { border-color: var(--red); background: var(--surface-2); }
.opt.off { opacity: 0.5; cursor: not-allowed; }

.radio {
  width: 12px; height: 12px; flex: none; margin-top: 3px;
  border: 1px solid var(--line-strong); border-radius: 50%;
}
.radio.sq { border-radius: 3px; }
.note { margin: 14px 0 0; }
.radio.on {
  border-color: var(--red-bright);
  background: radial-gradient(circle, var(--red-bright) 43%, transparent 45%);
  box-shadow: var(--glow-red);
}

.body { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.row { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.name { font-weight: 600; font-size: 14px; }
.why { font-size: 12.5px; line-height: 1.5; }
code { font-size: 11.5px; color: var(--text-2); }
</style>
