# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

Python **3.11** only — torch/demucs/basic-pitch do not support 3.14. The venv at `.venv`
is uv-managed (cpython-3.11.14); use `.venv/bin/python` or `source .venv/bin/activate`.

`.env` holds `GEMINI_API_KEY` but **nothing loads it** — there is no python-dotenv
dependency and `config.gemini_api_key()` only reads `os.getenv`. Export it manually:

```bash
set -a; source .env; set +a
```

`librosa` needs ffmpeg on PATH to decode mp3 (`brew install ffmpeg`).

## Commands

```bash
python -m musiccopilot analyze song.mp3 --llm      # full pass, writes the cache
python -m musiccopilot parts song.mp3              # the form: parts, bars, times, chords
python -m musiccopilot chart song.mp3              # the recreate sheet (also -> chart.md)
python -m musiccopilot show song.mp3 --what chords # read the cache
python -m musiccopilot tab song.mp3 --part "guitar solo" --stem guitar --play
python -m musiccopilot tab song.mp3 --stem guitar --start 1:02 --end 1:18 --audio
python -m musiccopilot tab song.mp3 --part "guitar solo" --stem guitar --llm-clean
python -m musiccopilot snippets song.mp3 --stems   # re-cut the per-part wavs
python -m musiccopilot solo song.mp3 --prompt "slow bluesy" --play
python -m musiccopilot models                      # Gemini models this key can reach

python -m musiccopilot tab song.mp3 --part "guitar solo" --follow   # play along
python -m musiccopilot tab song.mp3 --bars 97-112 --follow --minus-stem --count-in 4
python -m musiccopilot record --instrument guitar  # play in: live notes/tab/chords
```

`--follow` plays the passage and scrolls the tab under a cursor; `--minus-stem`
drops your instrument out of the mix so you play that part, `--speed 0.5` slows
it down without dropping the pitch, and `--follow-view notes` swaps the
fretboard for note names (for stems where frets would be a lie).

`--llm-clean` sends the transcribed window to Gemini (`gemini.clean_solo`) to
merge pitch-jitter fragments, correct octave errors and drop spurious
noise-floor notes before the tab is rendered. It is a display-time pass over
whatever `--part`/`--bars`/`--start`/`--end` selected - it never writes
`notes/<stem>.json`, so a bad cleanup costs nothing but re-running without
the flag.

There is no test suite, linter config, or build step. To smoke-test a change, run against
the checked-in `crystallize.mp3` — its stems, notes, lyrics and analysis are already in
`analyzed_songs/crystallize/`, so a rerun only recomputes what you deleted. Deleting
`form.json` and running `analyze` takes ~3s and exercises the whole form/chart/snippet
path; deleting `analysis.json` too costs ~30s more.

`cli.main` swallows every exception into a one-line red message. **Pass `--debug` anywhere
in argv to get the traceback** — it is stripped from argv before argparse sees it, so it
works next to any subcommand.

## Architecture

### Cache-first pipeline

`Song` ([pipeline.py](musiccopilot/pipeline.py)) is the hub every command goes through.
`Song.open()` reloads whatever already exists in `analyzed_songs/<song-stem>/` (created next
to the audio file by `config.workdir_for`, or under `$MUSICCOPILOT_OUT`); `Song.run()`
computes only the missing stages. Each stage is independently cached:

| file | stage |
|---|---|
| `stems/*.wav` | Demucs separation |
| `analysis.json` | tempo, beats, key, chords, structure |
| `notes/<stem>.json` | per-stem note transcription (rewritten after `form.json` for solo stems) |
| `lyrics.json` | Whisper on the vocal stem |
| `form.json` | song form — needs notes + lyrics, so it runs after both |
| `snippets/*.wav` | one audio excerpt per part (`--stems` also writes per-instrument) |
| `chart.md` | the recreate sheet, rewritten by `analyze` and `chart` |
| `llm_notes.txt` | Gemini listening notes (`--llm`) |

To recompute one stage, delete its file; `--force` redoes everything. `show`, `tab` and
`solo` silently call `run()` when `analysis.json` is missing (`cli._load`), so a "read"
command can trigger the whole slow pipeline.

`workdir_for` **moves** a leftover `.musiccopilot/<song>/` from the old layout into
`analyzed_songs/<song>/` rather than recomputing it — deleting stems that took minutes to
separate is not an acceptable migration. Do not reintroduce a second cache root.

### Dataclass ↔ JSON contract

`Note`, `Chord`, `Section`, `Line`, `Analysis`, `Part` and `Form` are plain dataclasses that
round-trip through `asdict`/`Cls(**row)`. Adding a field **with a default** stays compatible
with existing caches; renaming or adding a required field will make old caches raise on load
— delete `analyzed_songs/<song>/` or pass `--force` after such a change. `form.json` is
separate from `analysis.json` on purpose: the form is cheap to recompute, the chord track
is not.

### Lazy imports are deliberate

`musiccopilot/__init__.py` resolves `Song` through `__getattr__`, and demucs, basic-pitch,
whisper, sklearn, pretty_midi and google-genai are all imported *inside* the functions that
use them. This keeps `tabs`, `synth` and `notes` importable without torch/librosa. Keep new
heavy imports function-local.

### Time coordinates

Four coordinate systems meet and must stay consistent:

- **Seconds** — everything in `analysis.py`, `notes.py`, `synth.py`.
- **Bars** — 1-based, `Form.bar_times[n-1:n+1]` spans bar `n`; `form.bar_edges(analysis)`
  rebuilds the same grid from downbeats when there is no form yet. `cli.position` parses
  `62`, `1:02` and `bar17` into seconds, `cli._window` folds in `--part` / `--bars`.
  Part start/end are rounded to 2dp in JSON, so bar lookups add a 0.05s tolerance —
  without it a part start lands one bar early.
- **Beats from section start** — what Gemini emits (`SoloNote.beat`); `gemini.solo_to_notes`
  converts to absolute seconds using `tempo` and `t0`.
- **Grid columns** — `tabs.render_tab.col_of` maps seconds back to `(t - t0) * tempo/60 * subdiv`.
  Callers must pass the same `t0` they used to slice the notes, or the tab shifts. Pass
  `first_bar=` as well, or the printed bar numbers restart at 1 and cannot be seeked to.

### Chord detection chain

`audio.harmonic_bed` (stems minus drums/vocals) → beat-synced CQT chroma → cosine match against
97 templates from `CHORD_QUALITIES` → Viterbi smoothing. Two subtleties:

- `QUALITY_BIAS` in [config.py](musiccopilot/config.py) exists because cosine matching structurally
  favours sparse templates (a power chord can never score worse than the triad containing it).
  Tweak it there, not in the matcher.
- The "no chord" score is **not** flat: `nc_score - nc_drop * loudness`. A fixed threshold
  mislabels ~12% of loud beats as N.C., and `self_prob` then keeps the Viterbi path in the
  N.C. state for bars at a time (it cost 44% of `crystallize` before this). N.C. should mean
  silence, not a mediocre match.
- `_beat_bounds` guarantees bounds start at 0 and end at `n_frames`, so `librosa.util.sync`
  returns exactly `len(bounds) - 1` columns and column `i` spans `edges[i:i+2]`. Both
  `detect_chords` and `detect_structure` rely on this; preserve it if you touch either.

Tuning knobs live in `detect_chords(self_prob=, sharpness=, nc_score=)` — raise `self_prob`
for slow ballads, lower it for fast changes.

### Song form

[form.py](musiccopilot/form.py) turns the analysis into named parts (`Part`, `Form`), and
[chart.py](musiccopilot/chart.py) turns those into the recreate sheet. The pipeline is
`segment → _trim_silence → _refine → _assign → Part`, and each step has a reason:

- `segment` is the McFee/Ellis laplacian recipe: a beat-synchronous recurrence matrix
  (harmonic repetition) balanced by degree against a path matrix (timbral continuity),
  spectral-clustered for every `k` in `FORM["k_range"]`. `_score` picks the `k` that looks
  most like a pop song — repeated labels, sane section lengths, four-bar multiples.
- `_refine` trims each occurrence of a family back to the bars that fit that family's chord
  loop **in that occurrence's own key** (`compare_loops` finds the transposition first — a
  lifted last chorus must not be trimmed to death). Leftover bars become parts in their own
  right and are regrouped by progression; that is where a pre-chorus comes from. A trim
  normally needs to improve the chord fit by 0.1, but an occurrence whose length **disagrees
  with the length the rest of its family agreed on** is accepted on a much smaller gain: the
  disagreement is itself the evidence. Crystallize's pre-chorus is the case — one 20-bar
  chorus among two 16-bar ones, where cutting the extra four bars only moves the fit
  0.80 → 0.86 because a pre-chorus's chords resemble the chorus either side of it.
- `_assign` names families rather than segments: the chorus is loud, repeated, and repeats
  its *words* (`_lyric_repeat`); the verse comes back early with different words. A family
  is judged only on its **sung** occurrences — one cluster routinely holds the verse, the
  solo over the verse changes, and the outro, and averaging over all three hides the verse.
  Only a family sung **more than once** can be the verse: a one-off pre-chorus scores well on
  "early, and different words every time" and would otherwise take the verse slot outright —
  and since each family gets at most one role, the real verse then silently gets none.
- Which stem is the `lead` on a solo is note density **weighted by how audible that stem
  actually is** over the part, and by how much it moves around. Raw note counts are not
  usable on their own: the near-silent piano stem in crystallize (RMS 0.001, 40× below the
  guitar) had *more* Basic Pitch notes than the guitar playing the solo, because the model
  fires on the noise floor demucs leaves behind — that is how a guitar solo came to be
  labelled "Piano solo". `bass` is deliberately not a lead candidate (`LEAD_STEMS`): it is
  loud and busy through the whole arrangement, so no loudness/density heuristic separates a
  bassline from a bass solo. `other` is down-weighted because it is demucs' catch-all and
  usually holds bleed from whichever named stem did not separate cleanly.
- A part with no lyric line inside it is instrumental whatever the vocal stem says; stem
  bleed and reverb tails otherwise turn outros into verses.

Chord comparison throughout is deliberately loose (`same_chord`): root plus major/minor,
with power and sus chords matching either, because template matching cannot tell `Em` from
`E5` from `Em7` on one beat and a chart does not care. `find_loop` prints the consensus
chord per slot; `FORM["loop_agreement"]` is how exactly a loop must repeat to be called a
loop, `FORM["same_loop"]` (stricter) is how exactly two repeats must agree before the chart
claims they are the same chords.

### Stem names are load-bearing

The six `htdemucs_6s` outputs (`drums bass other vocals guitar piano`) are simultaneously the
wav filenames, the keys of `Song.stems` and `Song.notes`, the `--stem` CLI values, the keys of
`PITCH_RANGE`, and the entries of `TRANSCRIBE_STEMS`. Renaming one means touching all of them.
`pipeline.run` picks the transcriber by stem: `bass`/`vocals` → monophonic pYIN, everything
else → polyphonic Basic Pitch. With no stems at all it transcribes the raw mix as `"mix"`.

### Solos are re-transcribed monophonically

Basic Pitch is polyphonic, which is the wrong model for a solo: on one string playing one
note at a time it invents extra simultaneous pitches and chops a bend into a staircase of
separate notes. So `pipeline._refine_lead_notes` runs *after* the form is known and, for
every part with a `lead` stem, re-transcribes **just that window** with `notes.transcribe_lead`
(torchcrepe → `notes._crepe_notes`) and splices it in with `notes.replace_window`. The rest of
the stem keeps its polyphonic transcription, because rhythm parts really are chords. This is
why `notes/<stem>.json` is rewritten after `form.json`, and why deleting `form.json` alone
also re-does the solo notes.

Two things in `_crepe_notes` are load-bearing. Its gates are **relative to the clip** — a
demucs stem is far quieter than a mix (the crystallize solo sits at ~-55 dBA), and
torchcrepe's shipped speech defaults (`Silence(-50)`, `Hysteresis`) discarded 82% of the
frames, i.e. the whole sustain of every note. And a note is held through short unvoiced
dropouts and up to the next attack, because a guitar rings on; without that you get 0.07s
stabs with silence between them. Technique detection reads the note *body* only (the glide
out of a note belongs to the next one), and a bend must go **up** and be *held* — downward
drift is a release or a slur, not a bend.

### The solo-notes cache feedback trap

`form.detect_form` picks each part's `lead` stem from **note density**, and
`pipeline._refine_lead_notes` then rewrites that stem's notes over the solo
window with a *monophonic* transcription that honestly reports far fewer notes
than the polyphonic pass. Those two facts interact badly: delete `form.json`
alone and lead detection re-runs against the previous run's *already thinned*
notes, the guitar's density drops below `FORM["solo_density"]`, and the part
silently demotes from "Guitar solo" to "Instrumental" — losing `lead`, and with
it the monophonic re-transcription and every bend in the tab.

So when re-running the form after touching anything in `notes.py`, delete
`notes/<stem>.json` **as well as** `form.json`. A demotion to "Instrumental" on
a part that was a solo before is the signature of this, not evidence that the
transcription change was bad.

### Live audio (play-along and recording)

Both live features need `sounddevice`; everything else still imports without it.

[playalong.py](musiccopilot/playalong.py) is `tab --follow`. Its `Transport`
reports position from the **audio callback's frame counter**, not from a
`time.monotonic()` clock started next to it — a wall clock drifts against the
device clock, and by the end of a 30s solo that is enough to park the cursor a
full beat away from what you are hearing. The cursor's column comes from the
same `TabLayout` the printed tab uses, so the bar points at the cell you would
have read.

[record.py](musiccopilot/record.py) is `record`. Capture and analysis are
separate threads on purpose: the audio callback only appends to a preallocated
buffer, because an underrun there is a hole in the take and the take is the one
thing that cannot be recomputed. The worker re-transcribes a 4s tail every
0.4s and commits only notes older than `_SETTLE` behind the write head —
otherwise every pass re-emits the note being played with a different end and
the display flickers. "What am I playing now" therefore *cannot* come from the
committed notes; it is read off the last voiced frames of the contour directly.
Pitch and chords run on **different cadences** (`_CHORD_EVERY`): together they
cost more than one period, and a single loop makes the now-playing readout
inherit the latency of the slowest thing in it. On stop the take is
re-transcribed **whole** — the offline segmenter can look forward, which is
what tells a bend from two notes — so what is saved beats what was displayed.

`notes.py` exposes both entry points off one implementation:
`_torchcrepe_contour` / `_crepe_notes` take a path, `_torchcrepe_contour_audio`
/ `_crepe_notes_from_audio` take an array, and both end in `_segment_contour`.
A take you played and a stem on disk must not segment differently.

### Fretboard placement is position-aware

`tabs.fret_notes`' Viterbi state is `(string, fret, hand position)`, not just
`(string, fret)`. A guitarist keeps the left hand in a four-fret box (`BOX`) and
crosses strings inside it; costing each note only against the previous one
misses that, because every pitch is reachable somewhere on the high E string,
so a purely local optimiser walks the melody up that one string and climbs the
neck. That is what put crystallize's solo at frets 0–3 on the high E when it is
actually played at 5–9 on the B string, and why it looked like it drifted sharp
in the second half.

`_open_penalty` is the other half. Open strings are the cheapest thing on the
neck, so on a short window they win by default even for a lead line at the
fifth fret. The penalty scales with the melody's own **10th-percentile** pitch —
a line that dips to an open string once still lives up the neck — which is what
keeps a two-bar window fretting the same way as the whole solo.

Bass is unaffected: its notes sit near its open strings, so the penalty is ~0
and open-position basslines still print as open position.

Bend sizes are quantised to `0.5/1.0/1.5/2.0` semitones in `_segment_contour`.
CREPE reads a whole-step bend at ~0.75–0.8 (it averages in the climb), which
rounded to a half-step bend — not a thing a guitarist plays. The magnitude is
measured from `base`, the pitch the note is *written* as, not from wherever the
contour started: `_cell` prints `fret + round(bend)`, so a bend measured off a
different origin renders as `7b7`, a bend to the note you are already on.
`_cell` also refuses a target at or below the fret, in case that ever recurs.

### Stems without a fretboard get a staff, not a fret lie

`TUNINGS` only has `guitar` and `bass`. `cmd_tab` and `chart._tab_of` both
check `stem in TUNINGS` (or `== "guitar"`) before calling `fret_notes` —
anything else (`piano`, `vocals`, `other`, and whatever a solo's `lead` stem
turns out to be) renders through `tabs.StaffLayout`/`render_staff` instead: a
text staff, one clef auto-picked per window by `pick_clef` (median pitch vs.
middle C, the same idea as `pick_instrument`), sized to the notes actually in
the window plus `LEDGER_PAD` rows, not a fixed grand-staff span — a full
treble+bass ladder is mostly empty rows for a part that lives in one clef.
`StaffLayout` mirrors `TabLayout`'s column/bar-grid math (`col_of`/`time_of`,
`per_line` wrapping) exactly, so a caller that lays out a tab can lay out a
staff the same way; only the row axis differs (staff line/space slot instead
of string). Before this, `--stem piano` silently fell through `pick_instrument`
and printed a 4-string bass fretboard for a piano part — fixing that is why
the check exists, not just the new renderer. `--follow` has no live cursor for
a staff yet; following a fretless stem always uses `--follow-view notes` (a
scrolling note-name ruler), and `_follow` overrides `--follow-view tab` to
`notes` with a warning rather than crashing on a `StaffLayout` passed to
`follow_tab`.

### Transcription is not bit-reproducible, and why it is close

Re-transcribing the same audio does **not** give byte-identical notes: torch's
CPU convolutions do not reduce in a fixed order, and a fresh process gives
different logits even single-threaded under `use_deterministic_algorithms(True)`.
This is upstream of this repo — it cannot be fixed here, only contained.

What contains it is the **decoder choice** in `_torchcrepe_contour_audio`:
torchcrepe's default Viterbi decoder picks a discrete pitch bin per frame, and
where two adjacent bins are near-equally likely, float noise flips the winner —
a whole bin, ~0.25 semitones, which is enough to move notes across
`_segment_contour`'s thresholds. `weighted_argmax` interpolates instead, which
took the run-to-run spread from 0.27 semitones to a bounded ~0.10 and made the
voiced/unvoiced mask exactly stable. About 90% of notes then reproduce exactly;
the rest is mostly one 10ms frame of boundary jitter. **Do not switch this back
to the default decoder.**

Median-smoothing the contour to chase the last 10% was tried and reverted: it
damps the wobble but also flattens the approach to each note enough to split a
sustained one into two or three, and the extra onsets then pull the fretting
off the string the phrase is played on. Stability bought by inventing notes is
not worth having.

The practical consequence: when comparing two transcriptions, expect ~10% of
notes to differ by a frame. A change is only real if it moves more than that.

### Musical constants

[config.py](musiccopilot/config.py) is the single source for tunings, fret limits, per-instrument
pitch windows, the chord vocabulary, open-chord voicings and movable barre shapes. `tabs.py`
consumes these both for placement cost and for printing fingerings; don't hardcode intervals
or tunings elsewhere.

### Gemini

[gemini.py](musiccopilot/gemini.py) uses structured output — the pydantic `Solo` schema is passed
as `response_schema`, so prompt changes must keep the schema satisfiable. Model comes from
`GEMINI_MODEL` (default `gemini-3.5-flash`); `listening_notes` uploads the mp3 via the Files API.
`cli.main` seeds `np.random.seed(0)`, which also makes the synth's reverb impulse deterministic.
`python -m musiccopilot models` lists what a given key can actually reach — `models.list()`
can still list a model your key can no longer call (`generate_content` 404s with "no longer
available to new users"), so a listing is not proof a model works; only a real call is.

Always bind `genai.Client(...)` to a name before calling into it (every `gemini._client()`
caller does this). An unnamed temporary can be garbage-collected mid-request — tenacity's
retry loop and paginated calls like `models.list()` both reuse the client's httpx transport
across multiple round trips — and the GC'd client's `__del__` closes that transport out from
under the in-flight call. The failure then surfaces as `RuntimeError: Cannot send a request,
as the client has been closed`, which hides whatever the real error was (wrong model name,
bad key, quota).

`_config()` disables automatic function calling by default (`AutomaticFunctionCallingConfig
(disable=True)`) — nothing here passes `tools`, but the SDK checks the AFC config before it
checks whether any tools exist, so leaving it on its default logs a "use Chat.send_message
instead" warning on every process regardless.

`clean_solo` (`tab --llm-clean`, wired through `cli._llm_clean`) declutters a transcribed
note window rather than composing one: it reuses the `Solo`/`SoloNote` schema and
`solo_to_notes` unit conversion, but with a system prompt (`CLEAN_SYSTEM`) that explicitly
forbids inventing notes or changing the phrase's contour — only merging pitch-jitter
fragments, dropping noise-floor grace notes, and fixing octave errors. It runs at
`temperature=0.2` (cleanup, not composition) and is display-time only: it never writes
`notes/<stem>.json`, so a bad cleanup is one flag away from the raw transcription.
