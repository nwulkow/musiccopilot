# Porting MusicCopilot to iOS

A plan for shipping this app as a native iPhone app, and an honest assessment of
whether the result can be as strong as the desktop version.

**Verdict up front: yes, it can — and in several areas it will be better. But it is
a rewrite of the engine, not a port of it.** Every algorithm here survives the move;
almost none of the *code* does. Budget 12,000–15,000 lines of Swift to replace 6,845
lines of Python, and plan it as a staged migration behind a golden-reference test
harness that does not exist yet.

---

## 1. What we are actually moving

Measured on this repo, 2026-08-29.

| | |
|---|---|
| Python | 6,845 lines / 25 files (`musiccopilot` ~4,900, `scriptum` ~1,900) |
| Web client | Vue 3 + VexFlow, 23 files, 1.2 MB built |
| HTTP surface | 27 routes + 1 WebSocket |
| Test suite | **none** |

### Stage costs (Apple M5, 25.7 GB RAM, `crystallize.mp3`, 4:24)

| Stage | Time | Nature |
|---|---|---|
| Demucs `htdemucs_6s` separation | minutes | neural, 52 MB fp16 |
| `analyze()` — beats, key, chords, structure | **29.6 s** | pure DSP (librosa/scipy) |
| `audio.load()` mp3 → 44.1 k mono | 1.5 s | decode |
| `harmonic_bed()` | 0.4 s | mixing |
| `detect_form()` | 1.8 s | DSP + sklearn + pure logic |
| Note transcription (Basic Pitch / CREPE) | seconds/stem | neural |
| Lyrics (faster-whisper `base`) | seconds | neural |
| `Song.open()` — warm cache reload | **0.01 s** | JSON |

That last row is the whole reason this is feasible. The pipeline is already
cache-first: analysis is a one-time cost per song, and everything the user actually
*touches* during band practice reads JSON. An iPhone does not need to be fast at
analysis. It needs to be fast at the warm path, and the warm path is already free.

### Model weights

| Model | Size | iOS status |
|---|---|---|
| Basic Pitch | 1.9 MB | **already ships `nmp.mlpackage`** — CoreML, today |
| torchcrepe `tiny` / `full` | 1.9 MB / 97 MB | trivial conversion (6-layer CNN) |
| `htdemucs_6s` | 52 MB (fp16) | hardest conversion — see §6 |
| faster-whisper `base` | 141 MB (int8) | `whisper.cpp` + CoreML encoder, mature |

Realistic shipped payload: **~250 MB** of models, downloaded on first run rather
than bundled.

---

## 2. Why "just run the Python on the phone" does not work

Python itself is no longer the blocker — PEP 730 landed official iOS support and
BeeWare/Briefcase ship a working runtime. The blocker is everything underneath it:

- **`librosa` hard-requires `numba`** (verified: `numba>=0.51.0`), which is an LLVM
  JIT. iOS forbids W^X memory outside of WKWebView's JavaScript engine, so the App
  Store will not accept it. This kills the entire DSP layer directly.
- **`torch` has no iOS wheels** — 518 MB of the venv, and the base for demucs and
  torchcrepe both.
- **`scipy` needs a Fortran toolchain**; `llvmlite` is another 129 MB of LLVM.
- **`basic-pitch`** pulls ONNX/TF runtimes; **`ctranslate2`** (faster-whisper) and
  **`sounddevice`** are desktop-native.

The venv is 1.2 GB and essentially none of it is portable. The algorithms port;
the dependency stack does not.

---

## 3. Target architecture

```
┌──────────────────────────────────────────────┐
│  SwiftUI app                                  │
│    ├── Library · Song · Chart · Lyrics        │
│    ├── Tabs / Score      (native or VexFlow)  │
│    ├── Play-along        (AVAudioEngine)      │
│    └── Live mic          (AVAudioEngine)      │
├──────────────────────────────────────────────┤
│  MusicCopilotKit  (Swift package)             │
│    ├── DSP     — Accelerate/vDSP             │
│    ├── Form    — ported logic                 │
│    ├── Tabs · Score · Chart — ported logic    │
│    └── Cache   — same JSON contract           │
├──────────────────────────────────────────────┤
│  CoreML: demucs · basic-pitch · CREPE         │
│  whisper.cpp                                  │
├──────────────────────────────────────────────┤
│  Gemini via a thin auth proxy (never on device)│
└──────────────────────────────────────────────┘
```

Two constraints carry over from `CLAUDE.md` and must not be relaxed:

- **The JSON cache contract is the port's contract.** `Note`, `Chord`, `Section`,
  `Line`, `Analysis`, `Part`, `Form` round-trip through `asdict`/`Cls(**row)`. Make
  the Swift `Codable` structs read and write byte-compatible JSON. Then a song
  analysed on the Mac opens instantly on the phone, and — critically — the Python
  implementation stays usable as the reference oracle for the entire migration.
- **No musical decision moves to the view layer.** Scriptum's discipline (`app._window`
  calls straight into `cli._window`) is what keeps the browser and terminal agreeing
  about what "bars 17–24" means. The phone must call the same shared layer, not
  reimplement window parsing in SwiftUI.

---

## 4. Component-by-component

| Python | iOS replacement | Difficulty | Notes |
|---|---|---|---|
| `config.py` | Swift constants | trivial | tunings, `CHORD_QUALITIES`, `QUALITY_BIAS`, voicings |
| `pipeline.py` | Swift actor + `Codable` | easy | same cache layout, same staging |
| `tabs.py` | Swift | **easy but load-bearing** | Viterbi over `(string, fret, position)`, `_open_penalty` |
| `score.py` | Swift | easy | `_events`, `_fits`, `spell`, `_split_hands` |
| `chart.py`, `report.py` | Swift | easy | pure string building |
| `form.py` (assignment half) | Swift | medium | `_refine`, `_assign`, `compare_loops` — pure logic |
| `form.py` (`segment`) | Accelerate LAPACK | medium | spectral clustering, ~500×500 — small matrices |
| `analysis.py` | **Accelerate/vDSP** | **hard** | STFT, CQT, chroma, beat DP, Viterbi, HPSS |
| `audio.py` | AVFoundation | easy | decode, mix, resample |
| `notes.py` | CoreML | medium | Basic Pitch free; CREPE easy; `_segment_contour` ports as-is |
| `lyrics.py` | `whisper.cpp` | easy | well-trodden iOS path |
| `synth.py` | AVAudioUnitSampler | easy | **upgrade** — real sampler beats the numpy synth |
| `playalong.py` | AVAudioEngine | easy | **upgrade** — see §7 |
| `record.py`, `live.py` | AVAudioEngine | medium | **upgrade** — local mic |
| `gemini.py` | URLSession + proxy | easy | key must not ship in the binary |
| `scriptum/app.py` | deleted | — | the phone *is* the server |

The single biggest chunk of work is `analysis.py` and the DSP half of `form.py`.
Everything librosa gives for free — `stft`, `cqt`, `chroma_cqt`, `beat_track`,
`onset_strength`, `hpss`, `util.sync`, `frames_to_time` — has to be written against
vDSP. Call it 2,500–3,000 lines of Swift, and it is the part where correctness is
subtle rather than the part where it is hard.

---

## 5. Phased plan

### Phase 0 — Golden-reference harness *(do this first; ~1 week)*

There is no test suite. Porting tuned DSP without an oracle is how a port silently
gets worse. Before any Swift is written:

1. Freeze fixtures from `analyzed_songs/crystallize/` plus 3–5 more songs covering
   different tempos, keys and arrangements.
2. Dump **intermediate** arrays, not just final JSON: onset envelope, beat frames,
   CQT chroma, the harmonic bed, per-beat chord scores, the recurrence matrix.
3. Write a comparator with per-stage tolerances that already encode what
   `CLAUDE.md` documents: transcription is not bit-reproducible, ~10% of notes
   differ by a frame, and **a change is only real if it moves more than that**.
4. Wire it to run against either implementation, Python or Swift.

This harness is the deliverable that makes every later phase verifiable. It is also
worth having even if the iOS port is cancelled.

### Phase 1 — Mobile-ready the existing UI *(~2 weeks)*

Get something on a phone immediately, server-backed, to validate the UX before
committing to the engine rewrite.

- The Vue client has exactly two `@media` queries and **no touch handling at all**
  (verified: no `touchstart`/`pointerdown` anywhere in `web/src`). Add touch scrubbing
  to `TabGrid` and `ScoreSheet`, larger hit targets, a bottom tab bar instead of the
  sidebar.
- Wrap it in a native shell (Capacitor or a plain `WKWebView`) pointed at a Mac or
  home server running `python -m scriptum`.
- **This is a stepping stone, not the product.** Rehearsal rooms have bad wifi, and
  the whole point of the phone app is that it works standing up, offline, in a room
  with other people. Do not let this phase become the plan.

### Phase 2 — `MusicCopilotKit`: the pure-logic layer *(~4 weeks)*

No ML, no DSP. Port the code whose inputs are already JSON:

- `Codable` structs matching the dataclass contract exactly.
- `tabs.py` fretting Viterbi, `score.py` notation, `chart.py`, `form.py`'s naming
  and refinement logic, `config.py` constants.
- Validate against Phase 0 fixtures: feed `analysis.json` + `notes/*.json` in, expect
  byte-identical `chart.md` and identical tab/score layouts out.

At the end of this phase the phone can render everything for an already-analysed
song, offline, natively. That is most of the band-practice value.

### Phase 3 — DSP core *(~6 weeks)*

The hard one. `analysis.py` and `form.segment` against Accelerate.

- STFT/CQT/chroma on vDSP, beat tracking DP, Viterbi chord smoothing, HPSS median
  filtering, spectral clustering via LAPACK.
- Preserve `_beat_bounds`' invariant (bounds start at 0, end at `n_frames`, so column
  `i` spans `edges[i:i+2]`) — both `detect_chords` and `detect_structure` depend on it.
- Re-verify `QUALITY_BIAS` and the loudness-scaled `nc_score - nc_drop * loudness`
  against fixtures. These are tuned against *librosa's specific* CQT and chroma; a
  reimplementation that is 2% different in normalisation will shift them. **Expect to
  re-tune, and treat "N.C. rate went up" as the canary** — it cost 44% of `crystallize`
  once already.

### Phase 4 — Models on CoreML *(~4 weeks)*

In ascending order of risk:

1. **Basic Pitch** — ships `nmp.mlpackage` already. Wire it up and move on.
2. **CREPE** — 6-layer CNN, mechanical `coremltools` conversion. Keep
   `weighted_argmax` decoding; **do not fall back to the Viterbi decoder** (it costs
   run-to-run stability, per `CLAUDE.md`). Port `_segment_contour` unchanged — its
   clip-relative gates, note-holding through dropouts, and 0.5-semitone bend
   quantisation are all load-bearing.
3. **Whisper** — `whisper.cpp` with the CoreML encoder. `base` is 141 MB.
4. **Demucs** — see §6.

### Phase 5 — Native audio *(~3 weeks)*

- `AVAudioEngine` graph for play-along; position from the render callback's frame
  count, **never** from a wall clock — the same rule `playalong.Transport` and
  `useTransport` already follow, and the same failure if broken.
- `AVAudioUnitTimePitch` for `--speed` (replaces librosa time-stretch, and is
  realtime).
- Mixer-node gain for `--minus-stem`.
- Live mic: `AVAudioEngine` input tap feeding the ported `_segment_contour`. Keep
  `record.py`'s two-thread discipline — capture only appends, analysis is separate,
  now-playing reads the contour rather than committed notes, pitch and chords on
  different cadences.

### Phase 6 — Storage, jobs, LLM *(~3 weeks)*

- Storage redesign (§8).
- `BGProcessingTask` for analysis, with real progress; the existing `jobs.py` +
  SSE model maps cleanly onto it.
- Gemini through an auth proxy. `SCRIPTUM_LLM_TIMEOUT`'s lesson carries over: the
  same 75-note cleanup measured 48 s and 110 s on identical input, so the deadline
  is a reporting deadline, not a kill.

### Phase 7 — Ship *(~3 weeks)*

Import from Files/AirDrop/iCloud, iPad layout, VoiceOver, App Store review.

**Total: ~6 months of focused solo work.** Phases 2 and 3 are the critical path;
Phase 1 can run in parallel and Phase 4's demucs spike should start early because
it is the one item that can force an architecture change.

---

## 6. The demucs risk, specifically

This is the only component where the answer might be "no".

`htdemucs_6s` is a hybrid time/frequency U-Net with a cross-domain transformer.
Conversion problems, in order:

- Complex-valued STFT/iSTFT inside the graph — CoreML has no complex dtype, so it
  has to be split into real/imaginary and stitched, or lifted out of the model into
  Swift on either side.
- The transformer's attention may not map to ANE, dropping it to GPU or CPU.
- Memory: it already segments long audio, so this is manageable, but segment length
  will need tuning for a phone's budget.

**Mitigations, in the order I would try them:**

1. Spike this in week 1, not month 4. It is the only true architectural unknown.
2. `demucs.cpp` and existing community CoreML/ONNX ports are prior art — do not
   start from the PyTorch graph if someone has already solved the STFT split.
3. Fall back to 4-stem `htdemucs` if 6-stem will not convert. This costs the
   `guitar` and `piano` stems, which is a real feature loss — guitar solos are the
   app's centre of gravity — so treat it as a fallback, not a plan.
4. Worst case: hybrid separation. Analyse on a server *once*, sync stems to the
   phone, everything else on-device. Preserves offline practice, loses offline import.

Expect roughly **2–4× realtime** on an A17/A18 for separation. A 4-minute song is
then 1–2 minutes as a background task — acceptable for a one-time import, and it is
already the slowest stage on the desktop too.

---

## 7. What actually gets *better* on iOS

This is not a downgrade everywhere. Several things improve:

- **The mic stops being the server's.** `CLAUDE.md` records that both live panes open
  `record.Recorder` in the *server* process — so the machine running Scriptum has to
  be the one in the practice room. On a phone the mic is finally where the player is.
  This removes a whole architectural compromise.
- **Timing gets tighter.** `AVAudioEngine` gives a sample-accurate render callback and
  a hardware clock. The current design already goes out of its way to read the audio
  callback's frame counter instead of a wall clock; iOS makes that the easy path.
- **Realtime time-stretch.** `AVAudioUnitTimePitch` does what `--speed 0.5` currently
  needs offline librosa processing for.
- **Better synth.** `AVAudioUnitSampler` with a real soundfont beats `synth.py`'s
  numpy oscillators for auditioning generated solos.
- **Offline by construction**, which is what a rehearsal room actually needs.
- **Always in your pocket** — the thing a Mac in the corner running `:8420` is not.

---

## 8. Storage — measured, and fixable

Currently **490 MB for one 4:24 song**. The stems are 16-bit **stereo** 44.1 kHz WAV
— but nothing in the pipeline reads them that way: every transcriber loads
`mono=True`, CREPE resamples to 16 kHz, Basic Pitch to 22.05 kHz. The stereo and
most of the bit depth are paid for and never used.

### What compression actually costs (measured)

Guitar stem, solo window 187–210 s, transcribed with each engine and matched
note-for-note against the uncompressed baseline (exact pitch, onset within 20 ms):

| Variant | Size/stem | CREPE match | Basic Pitch match |
|---|---|---|---|
| wav 44k stereo (baseline) | 44.5 MB | — | — |
| wav 44k mono | 22.3 MB | **100.0%** | **100.0%** |
| wav 22k mono | 11.1 MB | **100.0%** | **100.0%** |
| flac mono | 5.9 MB | **100.0%** | **100.0%** |
| opus 96k stereo | 3.2 MB | 98.4% | 93.6% |
| aac 96k mono | 3.1 MB | 91.9% | 96.8% |
| **opus 64k mono** | **2.1 MB** | **96.8%** | **95.7%** |
| aac 64k mono | 2.1 MB | 90.3% | 93.6% |
| opus 32k mono | 1.1 MB | 85.5% | 87.2% |

**The control is what makes this readable.** Re-running CREPE on the *identical
file* in four fresh processes gives note counts of 63 / 62 / 62 / 58 and
cross-process agreement of **85.7% / 90.5% / 88.9%**. Within a single process both
engines are exactly deterministic; across processes they are not, which is the
non-reproducibility `CLAUDE.md` already documents.

So every codec at 64 kbps and above lands **at or above the floor of simply running
the transcriber twice**. Lossy compression at 64k is not distinguishable from
re-running the analysis. Only opus 32k drops to the floor itself, which is where I
would stop.

### Chords survive it too

All six stems at opus 64k mono, full `analyze()` pass:

| | wav stereo (267 MB) | opus 64k mono (11 MB) |
|---|---|---|
| tempo | 123.0 | 123.0 |
| key | E minor | E minor |
| chords / sections | 142 / 10 | 142 / 10 |
| **per-beat agreement** | — | **93.9%** |
| **under `same_chord()`** | — | **95.7%** |
| N.C. beats *(the canary)* | 3 | 4 |

Compare chords **on a time grid, not index-to-index** — run boundaries merge
differently, and a naive index comparison reports a meaningless 28% for what is
actually the same E5/C/D loop shifted by one slot.

### The plan

| Component | Now | After |
|---|---|---|
| `stems/` (6 × stereo WAV) | 267 MB | **11 MB** — mono, AAC/Opus 64k |
| `_backing_*.wav` (5 mixes) | 110 MB | **0** — a mixer gain change at playback |
| `snippets/` (12 files) | 90 MB | **0** — slices of stems; `Part` knows its bounds |
| generated solo wavs | ~17 MB | **0** — regenerate on demand |
| source mp3 | 4 MB | 4 MB |
| all JSON | 0.7 MB | 0.7 MB — *keep forever, this is the product* |
| **total** | **~490 MB** | **~16 MB** |

**~30× smaller.** Ten songs go from 5 GB to 160 MB.

**On iOS, prefer AAC over Opus** despite Opus scoring marginally better per bit.
AAC has hardware decode on Apple silicon — lower battery cost and lower latency
when six stems are decoding simultaneously under a play-along — and Opus needs a
container shim. The measured difference between them is inside the noise floor
anyway, so take the hardware path.

Two cautions. Encode from the *stems as separated*, never re-separate from a
compressed mix — the same rule `workdir_for` follows when it **moves** a legacy
cache rather than recomputing it. And compressing an existing cache will not
reproduce its notes exactly; nothing does, across processes.
## 9. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Demucs will not convert to CoreML | **high** | Spike week 1; 4-stem fallback; server-side separation |
| DSP reimplementation shifts tuned constants (`QUALITY_BIAS`, `nc_drop`) | **high** | Phase 0 harness with intermediate-array comparison |
| No test suite to port against | **high** | Phase 0 exists precisely for this |
| Scope: 6.8 k Python → ~15 k Swift | medium | Phased; Phase 2 alone delivers real value |
| Thermal throttling / battery on long analysis | medium | Background tasks, chunked work, "plug in" hint |
| Storage | medium | §8, and it is a known fix |
| Gemini key management | low | Auth proxy; never ship the key |
| App Store review of a 250 MB model download | low | Download on first run, not in the bundle |

---

## 10. So — can it be as strong?

**Yes.** Every algorithm in this repo is portable; none of them needs a desktop.
The tuned parts — the fretting Viterbi's position state, `_open_penalty`'s
10th-percentile scaling, `_segment_contour`'s clip-relative gates and bend
quantisation, `_assign`'s sung-occurrences-only rule, the loudness-scaled N.C.
score — are all *logic*, and logic ports. The cache-first design means the phone
never has to be fast at the slow things.

Three honest caveats:

1. **It is a rewrite, not a port.** The Python is 6,845 lines; the Swift will be
   12,000–15,000 because librosa and scipy have to be written out longhand. Roughly
   six months solo.
2. **Demucs is the one genuine unknown.** Everything else has a known path. Spike it
   before committing.
3. **The DSP port is where quality silently leaks.** Not because it is hard, but
   because chord detection is tuned against librosa's exact normalisation. Without
   the Phase 0 harness you will not notice the day it gets 3% worse.

Do Phase 0 and the demucs spike first — together about two weeks — and you will know
with confidence whether the remaining five months are worth committing.
