# Porting MusicCopilot to iOS

A plan for shipping this app as a native iPhone app, and an honest assessment of
whether the result can be as strong as the desktop version.

**Verdict up front: yes, it can — and in several areas it will be better. But it is
a rewrite of the engine, not a port of it.** Every algorithm here survives the move;
almost none of the *code* does. Budget 18,000–22,000 lines of Swift to replace 10,660
lines of Python, and plan it as a staged migration behind a golden-reference test
harness that does not exist yet.

---

## 0. What changed since the first draft (2026-08-29 → 2026-08-31)

Two days, and the engine grew by more than half. Re-measured on this repo today:

| | first draft | now |
|---|---|---|
| Python | 6,845 lines / 25 files | **10,660 lines / 30 files** |
| HTTP surface | 27 routes + 1 WS | **36 routes + 1 WS** |
| Web client | 23 files | **27 files** |
| Swift estimate | 12,000–15,000 | **18,000–22,000** |
| Timeline | ~6 months | **~7–8 months** |

Five modules landed that the first draft never saw, and one of them
(`daw.py`, 802 lines) it had already missed:

- **`texture.py`** (231 lines) — strum grouping. Pure logic, **measured at under
  1 ms** on a 773-note stem. This is a free Phase 2 win, and it is load-bearing
  for both tabs and `voices.py`.
- **`clean.py`** (257 lines) — checking a transcription against the audio. Needs
  one CQT per stem; **measured 1.34 s/stem**, so about +8 s on a six-stem
  `analyze()`. Same DSP machinery as `analysis.py`, one more consumer.
- **`voices.py`** (702 lines) — splitting one stem into several players. STFT,
  KMeans, silhouette, pitch-informed masking, iSTFT. **Measured 0.62 s** on
  crystallize's guitar. Cheap to run, expensive to verify.
- **`daw.py`** (802 lines) — multitrack import from GarageBand/BandLab. Absent
  from the first draft entirely. It gets its own section (§5) because iOS
  changes the shape of it more than any other module — mostly by deleting it.
- **`scriptum/capture.py`** (151 lines) — recording an input device into the
  library from the browser. Strengthens §8's argument, and one half of it does
  not port.

The premise the whole plan rests on **held up under re-measurement**: warm
`Song.open()` is 1.6–6.0 ms across four cached songs (crystallize, waves-bon-jovi,
an imported BandLab multitrack, and a solo-piano file), including a song whose
guitar has been split three ways. Everything the user touches during band
practice still reads JSON, and JSON is still free.

Two findings that change decisions rather than numbers:

1. **Storage is worse than measured, and worse in a new way** — 690 MB for
   crystallize, not 490, because `_backing_*.wav` has grown to **14 files and
   312 MB on one song**. It is combinatorial in `--minus-stem` selections, not a
   fixed cost. See §9.
2. **Lossy stems break `voices.py`'s exact-partition invariant.** §9's
   compression plan and `merge_voices`' "recovers the original stem exactly
   rather than approximately" cannot both be true. See §9.

---

## 1. What we are actually moving

Measured on this repo, 2026-08-31.

| | |
|---|---|
| Python | 10,660 lines / 30 files (`musiccopilot` 8,551, `scriptum` 2,109) |
| Web client | Vue 3 + VexFlow, 27 files, 1.3 MB built |
| HTTP surface | 36 routes + 1 WebSocket |
| Test suite | **none** |
| venv | 1.2 GB, essentially none of it portable |

### Stage costs (Apple M5, 25.7 GB RAM, `crystallize.mp3`, 4:24)

| Stage | Time | Nature |
|---|---|---|
| Demucs `htdemucs_6s` separation | minutes | neural, 52 MB fp16 |
| `analyze()` — beats, key, chords, structure | **29.6 s** | pure DSP (librosa/scipy) |
| `audio.load()` mp3 → 44.1 k mono | 1.5 s | decode |
| `harmonic_bed()` | 0.4 s | mixing |
| `detect_form()` | 1.8 s | DSP + sklearn + pure logic |
| Note transcription (Basic Pitch / CREPE) | seconds/stem | neural |
| `clean.clean()` — one CQT per stem | **1.34 s/stem** | DSP |
| `texture.strums` / `chordness` / `align` | **< 1 ms** | pure logic |
| `voices.split()` — cluster + mask + iSTFT | **0.62 s/stem** | DSP + sklearn |
| Lyrics (faster-whisper `base`) | seconds | neural |
| `Song.open()` — warm cache reload | **1.6–6.0 ms** | JSON |

That last row is the whole reason this is feasible, and it survived the engine
growing by half: four songs, up to 8 stems and 3,770 notes each, all under 6 ms.
The pipeline is cache-first — analysis is a one-time cost per song, and everything
the user actually *touches* during band practice reads JSON. An iPhone does not
need to be fast at analysis. It needs to be fast at the warm path, and the warm
path is already free.

The three new stages are all cheap next to what was already there. `texture.py`
in particular costs nothing measurable and buys the strum grouping that both the
tab renderer and the voice splitter depend on — port it in Phase 2 and two later
phases get easier.

### Model weights

| Model | Size | iOS status |
|---|---|---|
| Basic Pitch | 1.9 MB | **already ships `nmp.mlpackage`** — CoreML, today |
| torchcrepe `tiny` / `full` | 1.9 MB / 97 MB | trivial conversion (6-layer CNN) |
| `htdemucs_6s` | 52 MB (fp16) | hardest conversion — see §7 |
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
  `form.segment` alone needs `scipy.linalg.eigh`, `scipy.sparse.csgraph.laplacian`
  and `scipy.ndimage.median_filter`.
- **`sklearn`** is now a *two*-consumer dependency: `analysis.detect_structure` and
  `form.segment` use `KMeans`, and `voices._choose` adds `KMeans` plus
  `silhouette_score`.
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
│    ├── Import            (UIDocumentPicker)   │
│    └── Live mic / record (AVAudioEngine)      │
├──────────────────────────────────────────────┤
│  MusicCopilotKit  (Swift package)             │
│    ├── DSP     — Accelerate/vDSP              │
│    ├── Form · Texture · Clean · Voices        │
│    ├── Tabs · Score · Chart — ported logic    │
│    ├── Import  — ported `_classify`/`assign`  │
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
  `Line`, `Analysis`, `Part` and `Form` round-trip through `asdict`/`Cls(**row)`;
  since the first draft, `Voice` and `Split` (`voices.json`) have joined them.
  Make the Swift `Codable` structs read and write byte-compatible JSON. Then a
  song analysed on the Mac opens instantly on the phone, and — critically — the
  Python implementation stays usable as the reference oracle for the entire
  migration.

  The cache grew by **four files** the first draft did not account for, and three
  of them are *revision records* rather than data:

  | file | what it is | why it exists |
  |---|---|---|
  | `note_backends.json` | which engine read each stem | else a backend change is a silent no-op against a warm cache |
  | `notes_clean.json` | `[clean.REVISION, texture.REVISION]` per stem | same argument, for the note-shaping passes; a bare int reads back as `[n, 0]` |
  | `voices.json` | `Split`/`Voice` per stem, **including one-player records** | a `--undo` that the next run reverses is not a correction |
  | `sources.json` | which of the band's tracks each stem was | makes `pipeline.run` skip separation *even under `--force`* |

  Port the revision-comparison semantics, not just the fields. A Swift
  implementation that reads `notes_clean.json` but never checks it against its
  own `REVISION` will happily serve notes shaped by a version of the code that no
  longer exists.

- **No musical decision moves to the view layer.** Scriptum's discipline
  (`app._window` calls straight into `cli._window`, `serialize.layout_json` reads
  geometry off the Python `TabLayout`) is what keeps the browser and terminal
  agreeing about what "bars 17–24" means. The phone must call the same shared
  layer, not reimplement window parsing in SwiftUI.

---

## 4. Component-by-component

| Python | iOS replacement | Difficulty | Notes |
|---|---|---|---|
| `config.py` | Swift constants | trivial | tunings, `CHORD_QUALITIES`, `QUALITY_BIAS`, voicings, and now `CLEAN`/`TEXTURE`/`VOICES` |
| `pipeline.py` | Swift actor + `Codable` | easy | same cache layout, same staging, same revision records |
| `texture.py` | Swift | **trivial** | pure logic, no audio, < 1 ms. Do it first |
| `tabs.py` | Swift | **easy but load-bearing** | Viterbi over hand position, `_placement` bitmask DP, `_open_penalty` |
| `score.py` | Swift | easy | `_events`, `_fits`, `spell`, `_split_hands` |
| `chart.py`, `report.py` | Swift | easy | pure string building |
| `daw.py` (classify/assign/reassign) | Swift | easy | pure logic — and see §5 |
| `daw.py` (TCC / `lsof` / `osascript`) | **deleted** | — | ~200 lines that iOS makes meaningless. §5 |
| `form.py` (assignment half) | Swift | medium | `_refine`, `_assign`, `compare_loops` — pure logic |
| `form.py` (`segment`) | Accelerate LAPACK | medium | spectral clustering, ~500×500 — small matrices |
| `clean.py` (logic) | Swift | easy | `_merge_repeats`, `_drop_overtones`, `presence`/`absent` |
| `clean.py` (spectrum) | vDSP | medium | one CQT per stem — reuses `analysis.py`'s |
| `voices.py` (cues + clustering) | Swift + Accelerate | **medium–hard** | KMeans, silhouette, `_detrend` regression, `_distinct`'s gates |
| `voices.py` (masking) | vDSP | medium | STFT → per-player comb mask → iSTFT, **and it must be an exact partition** (§9) |
| `analysis.py` | **Accelerate/vDSP** | **hard** | STFT, CQT, chroma, beat DP, Viterbi, HPSS |
| `audio.py` | AVFoundation | easy | decode, mix, resample |
| `notes.py` | CoreML | medium | Basic Pitch free; CREPE easy; pYIN is real DSP; `_segment_contour` ports as-is |
| `lyrics.py` | `whisper.cpp` | easy | well-trodden iOS path |
| `synth.py` | AVAudioUnitSampler | easy | **upgrade** — real sampler beats the numpy synth |
| `playalong.py` | AVAudioEngine | easy | **upgrade** — see §8 |
| `record.py`, `live.py` | AVAudioEngine | medium | **upgrade** — local mic |
| `scriptum/capture.py` | AVAudioEngine | easy | mic half **upgrades**; loopback half does not port (§8) |
| `cli.py` (`_window`, `position`, `_voice`) | Swift, shared | **easy but load-bearing** | the browser/terminal agreement rule, one level up |
| `gemini.py` | URLSession + proxy | easy | key must not ship in the binary |
| `scriptum/app.py`, `serialize.py`, `jobs.py`, `library.py` | deleted / `BGProcessingTask` | — | the phone *is* the server |

The single biggest chunk of work is still `analysis.py` and the DSP half of
`form.py`. Everything librosa gives for free — `stft`, `cqt`, `chroma_cqt`,
`beat_track`, `onset_strength`, `pyin`, `hpss`, `util.sync`, `frames_to_time`,
`sequence.viterbi_discriminative`, `segment.recurrence_matrix` — has to be written
against vDSP. Call it 2,500–3,000 lines of Swift, and it is the part where
correctness is subtle rather than the part where it is hard.

What the new modules change about that estimate: **not much, and in a good
direction.** `texture.py` is free. `clean.py` adds one consumer of a CQT you were
already writing. `voices.py` is the only genuinely new DSP, and it is bounded —
STFT, a comb mask, an iSTFT, and a KMeans small enough to write by hand. The
growth is mostly in the *pure logic* tier, which is Phase 2, which is the cheap
phase.

---

## 5. The import door is different on iOS, and mostly simpler

`daw.py` is 802 lines — the third-largest module in the repo — and the first draft
of this plan did not mention it. It matters more than its size suggests: `CLAUDE.md`
records that importing a real multitrack **skips separation entirely**, and that
this is a *quality* change and not only a speed one. Three problems documented
elsewhere in that file — `other` holding bleed, `harmonic_bed` feeding chord
detection a reconstruction, Basic Pitch firing on a near-silent stem's noise floor
— simply do not occur on a real multitrack. If the band records at practice, this
is the main path in, not a side door.

### What iOS deletes

Roughly 200 lines of `daw.py` are macOS permission archaeology: `readable()`,
`_tcc_hint`, `responsible_app()` walking the process ancestry to find which `.app`
owns the TCC grant, `_lsof_projects()`, the `osascript` fallback, `reveal()`'s
`open -R`, and `_PROJECT_DIRS` scanning `~/Music/GarageBand`.

**None of it has an iOS equivalent, and none of it needs one.** iOS has no
ambient filesystem to be refused access to — `UIDocumentPickerViewController`
grants access *by the act of the user choosing the file*, and a security-scoped
bookmark keeps it. There is no row to hunt for in System Settings, no
"responsible app", nothing to explain. This is one of the very few places where
the port comes out **smaller** than the original.

The same collapse happens to the three-front-doors design. On the desktop, folder,
zip and `.band` are three doors because two of them are questions about the
*server's* filesystem and one is a browser upload. On iOS every door is the
document picker, plus the Share Sheet as a second entrance to the same code. So:

| desktop door | iOS |
|---|---|
| folder of per-track audio | picker, `.folder` type |
| `.zip` of that folder | picker, `.zip` — `_unzip`/`_unwrap` port as-is |
| `.band` package | picker, `.package` type — **see the spike below** |
| `POST /api/daw/upload` (BandLab) | the same picker; BandLab's iOS app exports to Files |
| `/api/daw/browse`, `/api/daw/garageband`, `/api/daw/reveal` | **deleted** |

`library_root()/.imports` staging and its day-old pruning go too: the picker hands
over a URL, `import_session` transcodes into `stems/` anyway, and there is nothing
to stage.

### What ports unchanged

`_classify`'s two-tier keyword matching, `assign`'s most-confident-first
allocation, `_group_regions`' longest-region rule, and `reassign`'s four
correctness rules (permutation-safe `_shuffle`, notes travelling or dying by
instrument, only an instrument change being expensive, dense renumbering of
untouched rows) are all pure logic and port straight into Phase 2.

Two details to carry over deliberately, because they are cheap to lose and
expensive to rediscover: a **strong** keyword may match as a substring (`git`
catches "Gitarre") while a **weak** one must be a whole word (`di` is the middle
of "Au*di*o", and BandLab names every mic track `VoiceAudio` by default); and
`sources.json` is what makes `pipeline.run` skip separation even under `--force`,
because running demucs over imported stems replaces the recording with a guess at
the recording and there is no way back.

### The spike: is an iOS `.band` the same package?

**Unknown, and worth a week.** The desktop reader takes exactly one assumption —
that each region starts at bar 1 — and otherwise just reads audio out of
`Media/`, with `Output/` as the mix when GarageBand has bounced one. GarageBand
for iOS also writes `.band` projects and also exposes them through the Files app,
which would make this the *easiest* import path on the platform rather than the
hardest. But it is a different app with a different project writer, and nothing
here has verified that `Media/` is populated the same way, or at all.

Test it the cheap way before committing: record a two-track project in GarageBand
for iOS, export the project (not a mixdown) to Files, AirDrop it to the Mac, and
run `python -m musiccopilot import "That.band" --dry-run`. If the existing reader
prints a sane mapping, the iOS import story is *better* than the desktop one,
because the TCC block that gates the whole feature on macOS does not exist there.
If it does not, the folder door still works and the loss is one convenience.

This deserves the same treatment as the demucs spike in §7 — an early, cheap
answer to a question that changes the plan — with far less at stake.

---

## 6. Phased plan

### Phase 0 — Golden-reference harness *(do this first; ~2 weeks)*

There is no test suite. Porting tuned DSP without an oracle is how a port silently
gets worse. Before any Swift is written:

1. Freeze fixtures from `analyzed_songs/` — the five cached songs already cover
   more ground than the first draft assumed: a separated pop song
   (`crystallize`), a song whose guitar `voices.py` split three ways
   (`waves-bon-jovi`, 8 stems), an imported BandLab multitrack
   (`slave-to-the-state`, with a `bass-2`), a three-guitar import
   (`ecstasy-song`), and a solo-piano file (`bach-siloti`) that exercises
   `clean.absent` writing *no notes at all* for `vocals`, `other` and `bass`.
2. Dump **intermediate** arrays, not just final JSON: onset envelope, beat frames,
   CQT chroma, the harmonic bed, per-beat chord scores, the recurrence matrix —
   and now also the strum groups, the `chordness` vector, `voices.py`'s cue matrix
   and its per-event labels.
3. Write a comparator with per-stage tolerances that already encode what
   `CLAUDE.md` documents: transcription is not bit-reproducible, ~10% of notes
   differ by a frame, and **a change is only real if it moves more than that**.
4. Wire it to run against either implementation, Python or Swift.

**`voices.py` needs its own rig, and it is the reason this phase went from one
week to two.** Its correctness was established against synthetic ground truth —
a known power-chord part and a known lead line, rendered separately, summed,
transcribed, and every transcribed note traced back to the part it came from,
across a panned mix, a centred mix and a legato solo. That rig produced the
numbers `CLAUDE.md` quotes (92% note placement panned, 75% centred; the
strum-grouping ceiling of 95–99% against 78% for simultaneity alone; the
2-of-6 → 5-of-6 improvement from clustering strums rather than notes). It is
not checked in. **Rebuild it and check it in**, because it is the only thing
that can tell a correct Swift port of `_distinct`'s gates from one that splits
every song with a solo into a rhythm player and a lead who are one person.

This harness is the deliverable that makes every later phase verifiable. It is also
worth having even if the iOS port is cancelled — arguably it is the single most
valuable thing in this document.

### Phase 1 — Mobile-ready the existing UI *(~2 weeks)*

Get something on a phone immediately, server-backed, to validate the UX before
committing to the engine rewrite.

- The Vue client still has **two responsive breakpoints** (`App.vue`,
  `AppSidebar.vue`, both `max-width: 860px`) and **no touch handling at all** —
  re-verified today: zero files in `web/src` reference `touchstart`, `touchmove`,
  `pointerdown` or `pointermove`. Add touch scrubbing to `TabGrid` and
  `ScoreSheet`, larger hit targets, a bottom tab bar instead of the sidebar.
- Wrap it in a native shell (Capacitor or a plain `WKWebView`) pointed at a Mac or
  home server running `python -m scriptum`.
- **This is a stepping stone, not the product.** Rehearsal rooms have bad wifi, and
  the whole point of the phone app is that it works standing up, offline, in a room
  with other people. Do not let this phase become the plan.

### Phase 2 — `MusicCopilotKit`: the pure-logic layer *(~6 weeks)*

No ML, no DSP. Port the code whose inputs are already JSON. This phase grew by
about two weeks and is still the best value in the plan.

- `Codable` structs matching the dataclass contract exactly, **including the four
  revision/provenance files** and their comparison semantics (§3).
- `texture.py` first — it is trivial, it is measurably free, and `tabs.py` and
  `voices.py` both sit on top of it.
- `tabs.py` fretting Viterbi and `_placement`, `score.py` notation, `chart.py`,
  `form.py`'s naming and refinement logic, `clean.py`'s note-list passes,
  `daw.py`'s `_classify`/`assign`/`reassign`, `cli._window`/`position`/`_voice`,
  `config.py` constants.
- Validate against Phase 0 fixtures: feed `analysis.json` + `notes/*.json` in, expect
  byte-identical `chart.md` and identical tab/score layouts out.

At the end of this phase the phone can render everything for an already-analysed
song, offline, natively — including a split-guitar song and an imported multitrack.
That is most of the band-practice value.

### Phase 3 — DSP core *(~7 weeks)*

The hard one. `analysis.py`, `form.segment`, `clean.py`'s spectrum and
`voices.py`'s masking against Accelerate.

- STFT/CQT/chroma on vDSP, beat tracking DP, Viterbi chord smoothing, HPSS median
  filtering, pYIN, spectral clustering via LAPACK, KMeans and silhouette by hand.
- Preserve `_beat_bounds`' invariant (bounds start at 0, end at `n_frames`, so column
  `i` spans `edges[i:i+2]`) — both `detect_chords` and `detect_structure` depend on it.
- Re-verify `QUALITY_BIAS` and the loudness-scaled `nc_score - nc_drop * loudness`
  against fixtures. These are tuned against *librosa's specific* CQT and chroma; a
  reimplementation that is 2% different in normalisation will shift them. **Expect to
  re-tune, and treat "N.C. rate went up" as the canary** — it cost 44% of `crystallize`
  once already.
- `clean.py` has a **second canary of the same kind**: `note_floor_db` (−35) sits
  inside a 40–50 dB gap between notes a band played and notes separation invented,
  which is a very wide target — but `presence_db` (−25) decides whether a whole
  stem gets *no notes at all*, and a normalisation drift there silently deletes a
  quiet instrument. Check `bach-siloti` still writes zero notes for `vocals`,
  `other` and `bass`, and that no real stem loses more than 3–8%.
- `voices.py`'s masks must **sum to one**, so that `guitar + guitar-2` is
  sample-for-sample the file they came from. That is not a nicety: it is why a
  split does not invalidate `analysis.json`, and why `merge_voices` can undo one
  exactly. A Swift iSTFT that is merely close will break both. Assert the
  partition property in the harness, not just the SDR.

### Phase 4 — Models on CoreML *(~4 weeks)*

In ascending order of risk:

1. **Basic Pitch** — ships `nmp.mlpackage` already. Wire it up and move on.
2. **CREPE** — 6-layer CNN, mechanical `coremltools` conversion. Keep
   `weighted_argmax` decoding; **do not fall back to the Viterbi decoder** (it costs
   run-to-run stability, per `CLAUDE.md`). Port `_segment_contour` unchanged — its
   clip-relative gates, note-holding through dropouts, and 0.5-semitone bend
   quantisation are all load-bearing.
3. **Whisper** — `whisper.cpp` with the CoreML encoder. `base` is 141 MB.
4. **Demucs** — see §7.

### Phase 5 — Native audio *(~3 weeks)*

- `AVAudioEngine` graph for play-along; position from the render callback's frame
  count, **never** from a wall clock — the same rule `playalong.Transport` and
  `useTransport` already follow, and the same failure if broken.
- `AVAudioUnitTimePitch` for `--speed` (replaces librosa time-stretch, and is
  realtime).
- Mixer-node gain for `--minus-stem` — which also deletes `_backing_*.wav`
  entirely (§9).
- Live mic and recording: `AVAudioEngine` input tap feeding the ported
  `_segment_contour`. Keep `record.py`'s two-thread discipline — capture only
  appends, analysis is separate, now-playing reads the contour rather than
  committed notes, pitch and chords on different cadences, and the take is
  re-transcribed **whole** on stop because the offline segmenter can look forward.

### Phase 6 — Storage, jobs, LLM *(~3 weeks)*

- Storage redesign (§9).
- `BGProcessingTask` for analysis, with real progress; the existing `jobs.py` +
  SSE model maps cleanly onto it, including the one-per-song rule that stops a
  `reassign` renaming files out from under a running analysis.
- Gemini through an auth proxy. `SCRIPTUM_LLM_TIMEOUT`'s lesson carries over: the
  deadline is a reporting deadline, not a kill, because a socket read cannot be
  interrupted. Carry over the thinking budgets and `clean_window_cost` too — a
  whole-song `clean_solo` is ~100× the bill of the intended one, and on a metered
  phone connection that is worse, not better.

### Phase 7 — Ship *(~3 weeks)*

Import from Files/AirDrop/iCloud (§5), iPad layout, VoiceOver, App Store review.

**Total: ~7–8 months of focused solo work**, up from ~6 in the first draft. Phases
2 and 3 are the critical path; Phase 1 can run in parallel, and both spikes — demucs
(§7) and the iOS `.band` format (§5) — should start in week 1, because they are the
two items that can force a change of plan rather than a change of schedule.

---

## 7. The demucs risk, specifically

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

**One mitigation got stronger since the first draft**, and it is worth saying out
loud: `daw.py` means separation is not on the critical path for the app's own band.
An imported multitrack skips demucs entirely *and produces better input* than
separation does. If the demucs spike fails outright, "import your GarageBand or
BandLab project" is a complete product for the people this app was built for — and
§5's iOS import story is, if the `.band` spike lands, easier than the desktop's.
Separation would then be the feature that comes later, for songs off the internet,
rather than the feature the app cannot ship without.

---

## 8. What actually gets *better* on iOS

This is not a downgrade everywhere. Several things improve:

- **The mic stops being the server's.** `CLAUDE.md` records that both live panes
  open `record.Recorder` in the *server* process — so the machine running Scriptum
  has to be the one in the practice room. That compromise has since grown a third
  consumer: `scriptum/capture.py` records takes into the library the same way, with
  the same constraint ("what Scriptum can record is whatever the machine it runs on
  can hear"). On a phone the mic is finally where the player is. This removes a
  whole architectural compromise, and it now removes it from three features rather
  than two.
- **Timing gets tighter.** `AVAudioEngine` gives a sample-accurate render callback and
  a hardware clock. The current design already goes out of its way to read the audio
  callback's frame counter instead of a wall clock; iOS makes that the easy path.
- **Realtime time-stretch.** `AVAudioUnitTimePitch` does what `--speed 0.5` currently
  needs offline librosa processing for.
- **Better synth.** `AVAudioUnitSampler` with a real soundfont beats `synth.py`'s
  numpy oscillators for auditioning generated solos.
- **The import door loses its permission problem** (§5) — about 200 lines of macOS
  TCC handling deleted rather than ported, and the feature stops being gated by a
  System Settings row that cannot be added by hand.
- **Offline by construction**, which is what a rehearsal room actually needs.
- **Always in your pocket** — the thing a Mac in the corner running `:8420` is not.

**One thing does not port.** `capture.py`'s other use is a *loopback* device —
recording whatever the machine can hear, not just its microphone. iOS has no
equivalent: an app may record its own audio and the mic, and that is all. The
practice-room case (point it at the room, hit record) is the one that survives, and
it is the one the phone is for. The "capture what is playing on this computer" case
stays on the desktop, which is fine — that is a desktop thing to want.

---

## 9. Storage — measured again, and worse than it looked

The first draft measured 490 MB for one song. Re-measured today, `crystallize` is
**690 MB**, and the five-song library is **1.4 GB**.

| | first draft | now |
|---|---|---|
| `crystallize` total | 490 MB | **690 MB** |
| `stems/` | 267 MB | 267 MB (6 stems) |
| `_backing_*.wav` | 110 MB / 5 files | **312 MB / 14 files** |
| `snippets/` | 90 MB | 90 MB |
| generated solo wavs | ~17 MB | ~20 MB |
| all JSON | 0.7 MB | 0.5 MB (56–584 KB across the library) |

Two things the first draft got wrong, both in the same direction.

**`_backing_*.wav` is not a fixed cost, it is combinatorial.** Every distinct
`--minus-stem` selection writes another full-length mix, and `crystallize` has
accumulated fourteen of them — `_backing_minus_bass`,
`_backing_minus_bass-drums`, `_backing_minus_bass-drums-guitar`, and so on. With
six stems the ceiling is the power set. This is not a storage line item to
compress; on a phone it is a leak. The fix is the one already planned — a mixer
gain change at playback rather than a rendered file — and it now saves 312 MB on
one song rather than 110.

**A song can have more than six stems.** `voices.py` splits one guitar into
`guitar`, `guitar-2`, `guitar-3`, and `waves-bon-jovi` accordingly has **eight**
stems on disk, not six. The "6 × stereo WAV" row was never a ceiling.

The underlying observation still holds and is still the point: the stems are
16-bit **stereo** 44.1 kHz WAV, and nothing in the pipeline reads them that way.
Every transcriber loads `mono=True`, CREPE resamples to 16 kHz, Basic Pitch to
22.05 kHz. The stereo and most of the bit depth are paid for and never used.

*(One caveat on "never used" that the compression plan must respect: `voices.py`
reads the stem in **stereo on purpose** — pan is its strongest single cue,
because two rhythm guitars are nearly always spread left and right. Collapsing
stems to mono on disk would delete that cue. So the mono conversion belongs at
transcription time, as it already does, and the stored stem stays stereo for any
stem `voices.py` might look inside — which `VOICES["stems"]` currently limits to
`guitar`.)*

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

### The new problem: lossy stems break the voice split's partition

`voices.py`'s masks are normalised to **sum to one**, which makes a split a
partition rather than an estimate. Two things rest on that, both quoted from the
code:

- `merge_voices` "recovers the original stem exactly rather than approximately" —
  it sums `guitar.wav` and `guitar-2.wav` back into one file, and that is why
  `--undo` and `--force` can be offered at all.
- `_forget_form` deliberately **keeps** `analysis.json` across a split, on the
  grounds that "a split is a partition, so the audio chords were detected over is
  unchanged".

Store the parts as AAC 64k and neither sentence is true any more. Two independent
lossy encodes do not have cancelling quantisation noise, so
`decode(guitar.aac) + decode(guitar-2.aac) ≠ decode(guitar.aac)`-before-split, and
the drift compounds across a split/merge/re-split cycle. The damage is modest —
it is the same order as the 93.9% chord agreement the compression already costs —
but the *claims* become false, and a claim you have stopped checking is how a port
gets quietly worse.

Three fixes, cheapest first:

1. **Do not store the split audio at all.** `CLAUDE.md` is explicit that "the notes
   are the deliverable and the audio is a bonus" — the split gains +2 to +3.5 dB
   SDR when the players are panned and roughly nothing when they are centred. Keep
   the unsplit stem plus `voices.json` and the per-voice note lists; derive a
   player's audio on demand from the cached masks, or do not derive it at all and
   let `--minus-stem` be a mixer gain like everything else. This makes
   `merge_voices` trivially exact (there is nothing to merge) and is the direction
   Phase 5 is going anyway.
2. **Store split parts losslessly** — ALAC, which has hardware decode on Apple
   silicon. §9's own table puts lossless mono at 5.9 MB/stem: still a 7.5× win over
   the 44.5 MB baseline, and a split stem is the exception rather than the rule.
3. **Accept the approximation and change the docstrings**, dropping
   `_forget_form`'s chord-track exemption so a split recomputes `analysis.json`.
   This is the honest version of doing nothing, and it costs 30 seconds of CPU per
   split — but it throws away a correct optimisation to pay for a compression
   choice, which is backwards.

I would take (1).

### The plan

| Component | Now | After |
|---|---|---|
| `stems/` (6–8 × stereo WAV) | 267 MB | **11–15 MB** — AAC 64k, stereo where `voices.py` may look |
| `_backing_*.wav` (14 mixes) | 312 MB | **0** — a mixer gain change at playback |
| `snippets/` (12 files) | 90 MB | **0** — slices of stems; `Part` knows its bounds |
| split-voice part wavs | included above | **0** — see fix (1) |
| generated solo wavs | ~20 MB | **0** — regenerate on demand |
| source mp3 | 4 MB | 4 MB |
| all JSON | 0.5 MB | 0.5 MB — *keep forever, this is the product* |
| **total** | **~690 MB** | **~16–20 MB** |

**~35× smaller.** Ten songs go from 6.9 GB to under 200 MB.

**On iOS, prefer AAC over Opus** despite Opus scoring marginally better per bit.
AAC has hardware decode on Apple silicon — lower battery cost and lower latency
when six to eight stems are decoding simultaneously under a play-along — and Opus
needs a container shim. The measured difference between them is inside the noise
floor anyway, so take the hardware path.

Two cautions. Encode from the *stems as separated*, never re-separate from a
compressed mix — the same rule `workdir_for` follows when it **moves** a legacy
cache rather than recomputing it. And compressing an existing cache will not
reproduce its notes exactly; nothing does, across processes.

---

## 10. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Demucs will not convert to CoreML | **high** | Spike week 1; 4-stem fallback; server-side separation; §7's new fallback — multitrack import needs no separation at all |
| DSP reimplementation shifts tuned constants (`QUALITY_BIAS`, `nc_drop`, `presence_db`) | **high** | Phase 0 harness with intermediate-array comparison; N.C. rate and `bach-siloti`'s empty stems are the canaries |
| No test suite to port against | **high** | Phase 0 exists precisely for this |
| `voices.py` has no checked-in ground truth | **high** | Rebuild and commit the synthetic two-player rig — Phase 0 |
| Scope: 10.7 k Python → ~20 k Swift, growing weekly | **medium–high** | Phased; Phase 2 alone delivers real value; freeze the Python engine when Phase 2 starts |
| iOS `.band` may not be the macOS `.band` | medium | Cheap spike (§5); folder door is the fallback |
| Lossy stems break the split/merge partition | medium | Do not store split audio (§9 fix 1) |
| Thermal throttling / battery on long analysis | medium | Background tasks, chunked work, "plug in" hint |
| Storage | medium | §9, and it is a known fix |
| Gemini key management | low | Auth proxy; never ship the key |
| App Store review of a 250 MB model download | low | Download on first run, not in the bundle |

The scope row is the one that moved. The engine gained 3,800 lines in two days
while this document sat still. **Phase 2 should begin against a frozen Python
engine**, or the port will be chasing a moving target for its entire length —
that is not an argument for stopping the Python work, it is an argument for
deciding when the Swift copy stops tracking it.

---

## 11. So — can it be as strong?

**Yes.** Every algorithm in this repo is portable; none of them needs a desktop.
The tuned parts — the fretting Viterbi's position state, `_open_penalty`'s
10th-percentile scaling, `_segment_contour`'s clip-relative gates and bend
quantisation, `_assign`'s sung-occurrences-only rule, the loudness-scaled N.C.
score, `texture.link`'s chained window, `voices._distinct`'s two gates on the
texture cue, `clean.py`'s 40 dB gap between a played note and an invented one —
are all *logic*, and logic ports. The cache-first design means the phone never
has to be fast at the slow things, and re-measuring it today with an engine half
again as large found the warm path still under 6 ms.

Four honest caveats:

1. **It is a rewrite, not a port.** The Python is 10,660 lines; the Swift will be
   18,000–22,000 because librosa and scipy have to be written out longhand.
   Roughly seven to eight months solo.
2. **The target is moving.** It grew 56% in the two days between the first draft
   of this document and this revision. Freeze the engine before Phase 2, or budget
   for the drift explicitly.
3. **Demucs is the one genuine unknown** — but a smaller one than it was, because
   multitrack import bypasses it entirely and produces better input than
   separation does. Spike it before committing; if it fails, ship the import path.
4. **The DSP port is where quality silently leaks.** Not because it is hard, but
   because chord detection is tuned against librosa's exact normalisation, and
   `voices.py` is tuned against a ground-truth rig that is not checked in. Without
   the Phase 0 harness you will not notice the day it gets 3% worse.

Do Phase 0, the demucs spike and the `.band` spike first — together about three
weeks — and you will know with confidence whether the remaining seven months are
worth committing.
