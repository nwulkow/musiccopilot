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
python -m musiccopilot import "Practice.band" --dry-run   # what each track would become
python -m musiccopilot import "Practice.band" --analyze   # GarageBand, no separation
python -m musiccopilot import ./bandlab-stems --map "Acoustic=guitar-2"
python -m musiccopilot tracks song.wav                    # what each imported track became
python -m musiccopilot tracks song.wav --map "Track3_VoiceAudio=vocals"

python -m musiccopilot analyze song.mp3 --llm      # full pass, writes the cache
python -m musiccopilot parts song.mp3              # the form: parts, bars, times, chords
python -m musiccopilot chart song.mp3              # the recreate sheet (also -> chart.md)
python -m musiccopilot show song.mp3 --what chords # read the cache
python -m musiccopilot tab song.mp3 --part "guitar solo" --stem guitar --play
python -m musiccopilot tab song.mp3 --stem guitar --start 1:02 --end 1:18 --audio
python -m musiccopilot tab song.mp3 --part "guitar solo" --stem guitar --llm-clean
python -m musiccopilot tab song.mp3 --part verse --stem guitar --voice melody
python -m musiccopilot snippets song.mp3 --stems   # re-cut the per-part wavs
python -m musiccopilot solo song.mp3 --prompt "slow bluesy" --play
python -m musiccopilot models                      # Gemini models this key can reach

python -m musiccopilot voices song.mp3             # who is playing the guitar stem
python -m musiccopilot voices song.mp3 --count 2   # ...insist on two of them
python -m musiccopilot voices song.mp3 --undo      # ...put them back into one stem

python -m musiccopilot transcribers                # note engines this install can run
python -m musiccopilot analyze song.mp3 --backend crepe
python -m musiccopilot transcribe song.mp3 --backend crepe --stem guitar

python -m musiccopilot tab song.mp3 --part "guitar solo" --follow   # play along
python -m musiccopilot tab song.mp3 --bars 97-112 --follow --minus-stem --count-in 4
python -m musiccopilot record --instrument guitar  # play in: live notes/tab/chords

python -m scriptum                                 # the web front end on :8420
cd web && npm install                              # vue, vue-router, vexflow
cd web && npm run build                            # required after any web/ change
cd web && npm run dev                              # or Vite on :5173, API proxied
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
the flag. It is capped to a **snippet** (`LLM_CLEAN_MAX_NOTES` /
`LLM_CLEAN_MAX_SECONDS`); see "What a Gemini button is allowed to cost".

`--voice` picks one reading of a stem that holds more than one part at once —
a riff and a strummed chord played by the same person into the same file.
`melody` reads out the line, `backing` what is under it, `all` (the default) is
everything. See "One stem, two parts, one reader".

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
| `note_backends.json` | which transcriber each stem's notes came from |
| `notes_clean.json` | which revision of each note-shaping pass (`clean.py`, then `texture.py`) passed over them |
| `voices.json` | which stems held more than one player — and which held one |
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
  actually is** over the part, by how much it moves around, and by **whether it is
  playing a line at all** (`FORM["lead_chord_penalty"]` over `texture.chordness`).
  That last one is the same argument that keeps `bass` out of `LEAD_STEMS` — a
  lead is one note at a time — and it only became measurable once `texture.py`
  could say which notes were struck together. It matters most exactly where it
  is newest: before a stem is split into players a solo's runner-up is a
  near-silent piano and the margin is huge, but once `voices.py` has split one
  guitar into a rhythm part and a lead, the runner-up *is* the rhythm guitar,
  strumming all the way through the solo. Measured on waves-bon-jovi's split
  solo the margin goes 1.45 (demoted to "Instrumental", losing the monophonic
  re-transcription and every bend with it) to 2.35. It is not set higher than
  0.6 because a piano lead genuinely does play chords and a line at once.
  Note that the part is named for `base_stem(lead)`, so a split guitar's solo
  is a "Guitar solo" and not a bare "Solo". Raw note counts are not
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

**A stem name is no longer the same thing as an instrument.** Separation can only ever produce
one guitar, but an imported multitrack (below) has two guitarists and a backing vocal, so a
stem may carry a `-2` suffix — `guitar-2` is its own stem, its own `notes/guitar-2.json` and
its own tab, while being a guitar for every lookup keyed by instrument. `config.base_stem`
does that strip, and **every** dict keyed by instrument goes through it: `PITCH_RANGE` and the
`auto` backend's mono/poly split in [notes.py](musiccopilot/notes.py), `LEAD_STEMS` and
`LEAD_STEM_BIAS` in [form.py](musiccopilot/form.py), `TRANSCRIBE_STEMS` in
[pipeline.py](musiccopilot/pipeline.py), and `TUNINGS` via `config.fretboard_for` — which also
replaced three copies of the `stem in TUNINGS or stem == "guitar"` idiom in `cli`, `app` and
`chart` with one. Miss one and a second guitarist gets `other`'s pitch window, or a fretboard
it has no strings for. Names that are not a suffixed canonical stem (`mix`) come back
unchanged, so `.get(name, default)` lookups behave exactly as they did.

`audio.mix` deliberately did **not** get this treatment: it matches exact names, and callers
that mean "every guitar" call `audio.stems_of` first. Widening inside `mix` would have made
`Song.backing(exclude=("guitar-2",))` silently put guitar-2 back into the bed it was excluded
from. `backing` itself accepts either — an instrument drops every stem of it (what you want to
solo over), one exact stem name drops only that one (what the second guitarist wants from
`--minus-stem`).

### One separated stem can hold several guitarists

Separation gives exactly one `guitar` file however many guitarists played, so a
band with a rhythm part, a lead and an acoustic gets all three summed into it —
and one tab with three people's notes stacked on one fretboard, in one hand
position, with the acoustic's open strings interleaved between the lead's bends.
[voices.py](musiccopilot/voices.py) splits such a stem again into `guitar`,
`guitar-2`, `guitar-3`, which the rest of the pipeline already knows how to read
(above): a suffixed stem is its own wav, its own notes and its own tab while
being a guitar for every lookup keyed by instrument. `Song.split_voices` runs it
as a pipeline stage; `musiccopilot voices` and `POST /api/songs/{id}/voices` are
the same call by hand.

**The unit it clusters is the strum, not the note.** Six strings and one pick
is one player, so the notes of a chord were all played by whoever played any of
them — and a cue read off a single note does not know that. Measured against
ground truth (a known power-chord part and a known lead line, summed and split
back apart) clustering notes one at a time cut *the rhythm player* in half and
handed the halves to two different stems, because the root, fifth and octave of
one power chord differ in register and in everything register drags along with
it. `texture.strums` says which notes those are (see "A strum is one event");
each group is one row, one weight and one label, and every note in it inherits
that label. The old `_chords` post-pass — a majority vote that pulled a chord
back onto one player *after* the clustering had already split it — is gone,
subsumed by doing it before.

**And the fourth cue is what the player is doing.** Timbre, pan and register
all describe an instrument; none of them describes a *part*, so two guitars
through the same amp at the same pan were indistinguishable however chordal one
was and however linear the other. `chordness` — 0 for a note struck alone, 0.5
for one of a pair, 1 for one of three or more — is the cue that says which is
the rhythm player, and `Voice.role` reports it in words ("chords" / "single
notes"), which is the half of a description a reader recognises first.

It is gated twice, because it is the most dangerous cue here:

- **`_distinct` has to claim what it says.** The clause reads "one plays chords
  where the other plays single notes", so one side must actually be at or above
  `TEXTURE["rhythm_at"]` and the other at or below `["lead_at"]`. A *gap* is not
  enough, and testing one was the mistake worth recording: a rhythm part whose
  chords the tracker caught whole half the time and one note of the other half
  splits into "all chords" and "some chords", which are 0.57 apart on this scale
  and are the same guitarist. Gating on the gap claimed a third player in all
  three two-player test mixes and cost 16 points of note accuracy in the panned
  one (91.9 → 75.6).
- **The two textures have to sound at the same time** (`_interleaved` against
  `VOICES["min_interleave"]`). One guitarist who strums the verses and plays the
  solo differs from *himself* in texture by more than two players usually do, so
  without this the cue splits every song with a solo into a "rhythm player" and a
  "lead player" who are one person — measured, on one rig, one pan, eight bars of
  chords then eight of solo, it does exactly that. Two people differ from one
  person doing two things in exactly one observable way: they can play at once.

`_interleaved` **reads the notes, not the clusters**, and that is the whole
point — the clustering is what is on trial, and when it is wrong it is wrong by
scattering each player's notes through the other's half, which makes both look
busy everywhere. The cluster-based version scored that one guitarist 73% "at
once" for two parts with no overlap at all, and let the split through. It also
reads the *quiet end* of the distribution rather than the average: both textures
appear somewhere in almost any stem, because a strum the tracker half caught
reads as single notes, so the measure is the 10th percentile of the smaller of
the two shares across two-second windows — "in nine windows out of ten, both a
chord part and a single-note part are audibly present". Measured 0.18/0.17/0.29
on three two-player mixes against 0.00, 0.06 and 0.11 for one-guitarist stems and
real single DI takes; the threshold sits in the middle of a thin margin, and it
only ever *guards* the texture clause, since pan decides any mix that has some.

**What this cost.** Two rhythm guitars playing the same texture are now harder
to tell apart, because the strongest axis says they are the same thing. On a
three-player mix (two rhythm guitars panned hard apart, a lead up the middle)
the split finds one player where the note-level version found two of the three
— and no weighting recovers it, because summing three parts compresses their
measured pan to 0.31 apart when they were mixed 1.2 apart. `--count` is the
override. Offering `_choose` both partitions per k — one clustered with
texture, one on the instrument cues alone — and letting `_distinct` judge both
was tried for this and made things worse, five of six right down to three: the
pan-only proposal re-admits precisely the over-splits texture was suppressing,
and the gate cannot tell one guitarist cut along a bleed-induced pan gap from a
second guitarist. The partition is part of the evidence, not just a starting
point for it. Against that, the four cases the note-level version got wrong — one
rhythm and one lead panned apart, dead centre, and legato, plus the one-player
false positives — now come out right: 2 of 6 correct before, 5 of 6 after, and
note accuracy on the panned pair went 65.8% → 91.9%.

**It clusters notes, not audio, and that is the whole design.** The obvious
approach — decompose the stem with NMF and cluster the parts by how they sound —
was built first and measured against ground truth (two known tracks summed,
split, compared back). It reliably found two of *something* and just as reliably
got them wrong, separating low notes from high ones or a guitar's fundamentals
from its own upper partials, and scoring several dB **worse than not splitting at
all**. The reason is structural: an NMF basis has no known pitch, so a timbre
feature read off one is anchored on a guessed fundamental, and a wrong guess
turns a timbre measurement into a pitch measurement. A *transcribed note* has a
known pitch, so its harmonics can be read where they actually are. That single
fact is why this stage runs after transcription rather than before — and it
means the split produces the per-player note lists directly, so no stem is ever
transcribed twice.

Four cues, weighted unevenly on purpose (`VOICES` in
[config.py](musiccopilot/config.py)). **Timbre** is the energy at each of a
note's own harmonics — the only cue that survives both guitars playing the same
riff in the same octave. **Pan** is the strongest single cue whenever it exists
at all, because two rhythm guitars are nearly always spread left and right and
demucs preserves the mix's stereo image. **Register** is weighted low: it is
real, but one guitarist who plays a low riff and then a high solo would be split
in two if it led. **Texture** is `chordness`, described above: the only cue that
tells a rhythm part from a lead line, and the only one that is about the *part*
rather than the instrument, which is why it is the one with two gates on it.
Measured against ground truth, the shipped ratio places 92% of notes with the
right player when the two are panned (93% when the lead is legato) and 75% dead
centre, where texture is the only thing separating them at all — the note-level
version did not split that mix at all.

None of the four is standardised to its own spread, which is deliberate:
standardising would make a cue carrying *no* information as loud as one carrying
all of it, and weighting pan fully on a centred mix took note accuracy from 84%
down to 60%. A cue that is not there should contribute nothing, and in natural
units it does.

**`_detrend` is load-bearing.** Reading harmonics relative to a note's own
fundamental is *supposed* to make timbre pitch-invariant and is not enough on its
own: on a single DI take the envelope still drifted with register hard enough
that one guitarist split into a "warm low player" and a "bright high player" more
convincingly (brightness gap 0.39) than two real guitars split apart (0.20). So
the fitted pitch-dependence is regressed out across the whole stem and only the
residual is clustered. The quadratic term matters — the relationship bends, and a
straight line leaves enough curvature behind to cluster on.

**Deciding *not* to split is the normal outcome, and cluster separation cannot
be what decides it.** Across five single-instrument stems and four two-player
mixes, silhouette scores ran 0.22–0.43 on *both*: one guitar cut in half scores
as convincingly as two guitars do. What separates them is *which cue* the halves differ on, which is what
`_distinct` gates on — an audible gap in placement; or one of them playing
chords while the other plays single notes, when the stem holds both at once;
or, with no pan to go on, a large gap in tone that also beats pitch at
explaining the split. The last clause is what rejects the DI take, and the
texture one carries its own guard for the same reason (above). The tone-only
path is the marginal one (0.185 accepted against 0.177 rejected in the
calibration set); pan is where the accuracy is, and a spurious `guitar-2` costs
a real tab and buys a tab of leakage.

**The audio follows the notes, and it is the weaker half.** Once each note has a
player, the stem is masked by that player's harmonics — pitch-informed
separation. The masks are normalised against each other so they **sum to one**,
which makes the split a partition: `guitar + guitar-2` is sample-for-sample the
file they came from. That is why splitting does not invalidate `analysis.json` —
`harmonic_bed` sums to exactly what it summed to before — and why
`merge_voices` can undo it exactly rather than approximately. Measured, the
audio gains +2 to +3.5 dB SDR when the players are panned and roughly nothing
when they are centred, so the notes (which are what the tabs are made of) are the
deliverable and the audio is a bonus.

What a split *does* invalidate is the form: which stem leads a solo is a
different question once there are two guitars to choose between, so
`_forget_form` drops `form.json`, `chart.md` and `snippets/`.

**It reads the stem again rather than reading back what a later stage wrote.**
`_refine_lead_notes` splices a monophonic transcription over each solo, which
honestly reports far fewer notes there — so clustering the cached notes weights a
solo at a tenth of its real size, and the same stem asked twice came back as two
guitarists once and three the next time depending only on whether the song had
been analysed before. It is the solo-notes feedback trap one stage along, and the
fix is the same: re-transcribe the source before clustering it. That re-read is
**not** written back unless the stem was also merged first — writing it over a
refined cache would replace the monophonic solo with a polyphonic read and take
every bend in the tab with it, silently, because a stem that turns out to hold
one player changes nothing else that would notice.

**An imported multitrack is never touched.** Its stems are the band's own
tracks, one player each by construction; looking for a second guitarist inside
one guitarist's DI take can only invent one. A wrong row there is a labelling
mistake, and `daw.reassign` is what fixes it.

`--undo` (and the Put back together button) is **sticky**: it leaves a
one-player record in `voices.json` rather than no record, so the next analysis
does not helpfully split the stem straight back apart. A correction the next run
silently reverses is not a correction. `--force`/`--count` is that same undo
followed immediately by a fresh look, so it clears the record first
(`merge_voices(remember=False)`).

### Importing a DAW multitrack skips separation entirely

[daw.py](musiccopilot/daw.py) (`musiccopilot import`) writes the stems demucs would otherwise
have *estimated*. Nothing downstream changes: it fills `analyzed_songs/<id>/stems/`, sums a
mixdown to be `Song.path`, and `analyze` then runs from `analysis.json` onward as usual. The
cache layout is the import API — that is why the feature is one module and not a second
pipeline.

This is a quality change, not only a speed one. Three problems documented elsewhere in this
file are separation damage and simply do not occur on a real multitrack: `other` holding bleed
from whichever stem demucs did not split cleanly, `harmonic_bed` feeding chord detection a
reconstruction rather than the instruments, and Basic Pitch firing on the noise floor of a
near-silent stem (the "Piano solo" that was a guitar).

Three front doors, because the two DAWs give you different things and hand them over from
different places:

- **A folder of per-track audio** is exact, and the only thing BandLab needs (Project →
  Download → Tracks gives full-length WAVs from zero). One file per track; a file named like a
  mixdown (`master`, `mix`, `bounce`) is taken as the song's audio instead of as a stem.
- **A `.zip` of that folder** is the same door with a lid on. `_unzip` extracts it to the temp
  directory, keyed by the zip's path/mtime/size and reused, because the web layer reads a
  session twice — once to show the mapping, once to import the corrected one — and unpacking a
  few hundred MB of WAV twice to answer the same question is not a cost worth paying.
  `_unwrap` then descends through folders that contain nothing but one folder, since Finder and
  BandLab both wrap a download in a folder named after itself. `Session.source` stays the
  *zip*, so `sources.json` records where the tracks came from rather than a temp path nobody
  should be sent back to.
- **A `.band` package** is read directly out of `Media/`. GarageBand has no stem export at all
  — the documented route is solo-and-export, once per track — but the package is a folder and
  the recorded takes are sitting in it. The cost is one assumption: **that each region starts
  at bar 1.** Where a region actually sits on the timeline lives in `projectData`, an
  undocumented Apple format, and inventing an answer would be worse than stating the limit.
  For the case this is for — a practice room, one take, everyone playing through — it holds
  exactly. `Output/` is used as the mix when GarageBand has bounced one, since the band's own
  fader balance beats anything summed here.

A track with several regions is the edited case, and `_group_regions` keeps only the longest
and warns. Laying regions end to end would invent an arrangement nobody played; for repeated
takes of one song the longest region *is* the take, and for a punch-in it is everything but
the fix — which the warning says out loud so the folder door is one sentence away.

**The GarageBand door is gated by TCC, and the block is not Scriptum's to fix.** On current
macOS `~/Music/GarageBand` is protected, so `readable()` is refused a path it knows perfectly
well. The part worth knowing is *whose* refusal it is: TCC grants nothing to `python`, it
grants to the application that launched it, so the toggle is filed under Terminal or VS Code
and someone told to "grant Scriptum access" is looking for a row that cannot exist.
`responsible_app()` walks the process ancestry to the outermost `.app` bundle and `_tcc_hint`
names it. It offers Full Disk Access and *not* Files and Folders, because that pane has no add
button — its rows appear only after an app has asked and been answered, which is no use to
someone already looking at the failure. The other route it offers needs no permission at all:
copy the project out in Finder. `reveal()` (`open -R`, `POST /api/daw/reveal`) is as far as the
app can help with that — Finder does the opening, so it works on a blocked path, but the drag
itself cannot be automated: the app may not copy what it may not read.

`_classify` maps track names onto stems in two tiers, and the tiers matter more than the word
lists. A name that says the instrument (`Gtr Nik`, `Gesang`, `Drum Kit OH`) ranks above one
that only implies it (`Acoustic`, `Lead`, `DI`), and `assign` allocates **most-confident
first** — so "Electric Guitar" takes `guitar` and a track called just "Acoustic" becomes
`guitar-2`, rather than whichever the filesystem listed first. The same split is what lets
"Acoustic Piano" be a piano while bare "Acoustic" is a guitar, without the scoring having to
arbitrate. `--dry-run` prints the whole mapping with its reasons and writes nothing; `--map
"Rhythm Gitarre=guitar-2"` overrides a row, and an override naming an exact slot claims it
before anything is auto-numbered.

**The two tiers also match differently, and that is not cosmetic.** A strong keyword may be a
substring — `git` is in the list precisely to catch "Gitarre" — but a weak one has to be a
whole word (`_WEAK_WORD`, letters as the boundary so "Amp2" and "Mic1" still match). Weak
words are short and standalone, and matching those loosely is actively wrong: `di` means a DI
box, and it is also the middle of **Au*di*o**. BandLab names every mic-recorded track
`VoiceAudio` by default, so a whole band's vocals imported as guitars — and a vocal track
labelled `guitar` is not a visible error, it is an empty Lyrics tab, because `pipeline.run`
transcribes lyrics from the stem literally called `vocals`. `voice` is in the strong vocals
list for the other half of the same case.

**`sources.json` is load-bearing, not a receipt.** Its presence is what makes
`pipeline.run` skip separation *even under `--force`*: running demucs over imported stems
would replace the recording with a guess at the recording, and there is no way back. It also
records which of the band's tracks each stem was, which nothing else can reconstruct —
`guitar-2` could be either guitarist.

That record is also what makes the mapping **correctable after the fact**. `daw.reassign`
(`musiccopilot tracks --map`, `POST /api/songs/{id}/tracks`, the `TrackPanel` beside the song
title) renames the stems in place rather than re-importing: the audio was always right, only
the labels on it were wrong, and re-importing to fix one row would mean handing over the whole
multitrack again. Four things it has to get right:

- **The renames may be a permutation.** Two guitarists swapping is `guitar → guitar-2` and
  `guitar-2 → guitar` at once, so `_shuffle` parks every file under a temporary name before
  any takes its final one. The temporary keeps the real suffix in the middle
  (`guitar.wav.moving`), so a crash between the passes leaves nothing the `stems/*.wav` glob
  reads back as a stem called `~guitar`.
- **Notes travel or die by instrument.** A track that only changed number is the same
  instrument on the same audio and its notes are relabelled with it; a change of instrument
  means they were read with the wrong `PITCH_RANGE` and the wrong mono/poly split, so they are
  dropped and read again. `note_backends.json` is re-keyed either way.
- **Only an instrument change is expensive.** Chords come from `audio.harmonic_bed`, which is
  the stems *minus* drums and vocals, so a track crossing that line means they were detected
  over different audio — `analysis.json` and `lyrics.json` go. A pure renumber leaves both
  standing: same files, same groups. The form reads stem *names* (which one is a part's lead),
  so `form.json`, `snippets/` and `chart.md` go either way. The mixdown never does; it is the
  same tracks summed in the same proportions.
- **Untouched rows renumber densely around the choice**, the same rule `assign` uses at import.
  Move one of two guitars to `vocals` and the other becomes `guitar`, not a lone `guitar-2`
  reading as a second guitarist who is not there.

The work runs inside a job rather than the request even though the renames themselves are
instant, because `JOBS.start`'s one-per-song rule is what stops it renaming files out from
under a running analysis. The argument checking is duplicated in the endpoint so a bad row is
a 400 rather than something to dig out of a failed job's transcript.

### The transcriber is a setting, and the cache remembers which one ran

`notes.BACKENDS` is the registry of note transcribers, `notes.DEFAULT_BACKEND`
is `basic-pitch`, and `notes.resolve_backend(name, stem)` turns a choice into
the tracker that actually runs. Four entries: `basic-pitch` (polyphonic),
`crepe` (torchcrepe's contour through `_segment_contour`, so it keeps bends),
`pyin` (librosa, the floor every other backend degrades to) and `auto` — the
old hardcoded split, pYIN for `bass`/`vocals` and Basic Pitch for everything
else, kept as an option because it was the behaviour of every cache written
before the choice existed.

The registry is **metadata only**: `_transcriber()` maps a name to a function
at call time, so importing `notes.py` still does not import torch or
TensorFlow (see "Lazy imports are deliberate"). `backend_status()` reports
per-backend availability by `find_spec`, which is what the `transcribers`
command and the web settings pane list — every backend but pYIN is optional,
and an engine that cannot import should be explained in a list rather than
blow up mid-analysis.

**`note_backends.json` is what makes it a setting rather than a no-op.** The
notes stage is cached per stem, so without a record of which engine wrote
`notes/guitar.json`, asking for CREPE on an already-analysed song would hit
`stem in self.notes`, reload the Basic Pitch notes and look exactly like the
setting did nothing. `Song.transcribe_notes` treats a backend mismatch as a
cache miss; `Song.open` backfills a missing record as `auto`, because that is
genuinely what produced every pre-existing cache — the alternative is
re-transcribing minutes of audio the first time anyone opens an old song.

It lives at the work root and **not** in `notes/`, which is globbed by stem
name: a `notes/backends.json` would load back as a stem called "backends".

`Song.retranscribe` is the cheap half of `run()` — stems, chords, lyrics and
form are left alone — and it re-refines the lead windows of **only** the stems
that changed, because a fresh pass over a stem overwrites the monophonic solo
notes spliced into it. `run()` passes `retranscribed` into the same condition
for the same reason: change the engine without that and every bend in the tab
silently disappears.

Two deliberate asymmetries. Changing the default to `basic-pitch` means an old
cache's `bass` and `vocals` (pYIN under `auto`) are re-read on the next
`analyze`; that is the default actually changing, not a bug. And
`transcribe_lead` stays monophonic whatever the stem's backend is — that stage
exists precisely because a polyphonic model is wrong on one string at a time —
so only an explicit `pyin` is honoured there, since someone who picked the
lightest tracker should not get torch loaded behind their back.

**Omnizart is deliberately not a backend.** It needs `madmom`, whose 0.16.1
release does `from collections import MutableSequence` (removed in Python 3.10)
and fails to build without Cython and setuptools declared; and TensorFlow 2.5,
which has no 3.11 wheel. This repo is pinned to 3.11 from the other end by
demucs and basic-pitch, so the two requirements have no overlapping version.
An Omnizart entry could only ever be a permanently dead row in the settings
pane. Do not add one back without checking that both constraints have moved.

### A transcription is checked against the audio before it is cached

Every tracker in `notes.py` answers "what pitch is most likely here?" and none
of them answers "is anything here at all?". [clean.py](musiccopilot/clean.py)
asks the second question, from one CQT of the stem, and it only ever *removes*
notes — everything it knows is evidence that something is not there, and
nothing it knows is evidence about what should have been there instead.

**The measurement that makes it possible.** Across this repo's material, notes
read off a stem the band actually played sit about 10 dB under that stem's own
loud level; notes read off a stem separation merely produced — the piano in a
song with no piano — sit **50 to 60 dB** under it, because they come from the
noise floor. That gap is enormous and a pitch tracker cannot see any of it.
`CLEAN["note_floor_db"]` (−35) is set inside it: crystallize's fake piano stem
goes from 1112 notes to 54, waves-bon-jovi's from 1336 to 151, while real stems
lose 3–8%.

**"Is this instrument in the song" is a question about the *set* of stems**, and
cannot be answered from one of them — a stem holding only residue is perfectly
self-consistent, and the own-floor threshold above is measured against a floor
that is all there is. So `clean.absent` compares each stem's loud level with the
loudest stem in the song (`presence_db`, −25): on the solo-piano test file that
is what correctly writes *no notes at all* for `vocals`, `other` and `bass`
rather than a page of them. An imported multitrack is exempt, for the same
reason `voices.py` never looks inside one: those stems are the band's own
tracks, and a quiet one is a quiet player.

**Two passes need the audio for reasons that are not about level.**

- `_merge_repeats`. A polyphonic model splits one held note wherever its own
  activation dips — 509 of crystallize's 1438 guitar notes start the instant
  another at the same pitch ended. Which of those were re-picked and which were
  one note *cannot be settled from the note list*: both look identical there.
  It can be settled from the audio, because **a struck string gets louder**. So
  a junction is merged only when the pitch's own band does not rise across it
  (`attack_db`), which is what keeps a driving eighth-note strum reading as
  eight notes rather than one long one. Merging on the gap alone was tried and
  ate a third of the guitar stem.
- `_drop_overtones`. A struck chord puts real energy an octave, a twelfth and
  two octaves above each note in it, and a polyphonic model transcribes some of
  those as notes; they are the ones that surface as a lone `e15` over a low
  chord shape. Level alone cannot find them — a partial of a loud note is
  louder than the whole of a quiet one — so the test is the **ratio**. Measured
  on isolated notes here, a guitar's octave partial sits ~10 dB under its
  fundamental and its twelfth ~17 (`overtone_rolloff`). A note no stronger than
  that over a note already sounding underneath it has not been shown to exist
  separately. Notes leave the candidate pool as they fail, so a partial cannot
  go on to justify the partial above it.

**`notes_clean.json` is what makes it a setting rather than a one-off**, exactly
as `note_backends.json` is for the engine: the notes stage is cached per stem,
so without a record of which revision checked `notes/guitar.json`, improving the
checking would only ever reach songs nobody had analysed yet.
`clean.REVISION` is compared on load and a mismatch is a cache miss — bump it
when a pass changes what it keeps. Unlike the backend record there is nothing
sensible to backfill a missing entry with, so a pre-existing cache is simply
re-read once. It lives at the work root for the same reason
`note_backends.json` does: `notes/` is globbed by stem name.

**The solo splice is deliberately not checked.** `_refine_lead_notes` runs
after this and replaces the solo window with a monophonic CREPE read, whose
gates are already relative to that clip (see below) and whose window is by
definition one where the lead is playing. Re-checking it against the whole
stem's loud level would argue away the quiet end of a solo.

### A strum is one event, and that is what a chord is made of

Every tracker in `notes.py` emits one note at a time and none of them ever
says which notes were *struck together*. That single missing fact is behind
both of the ways a guitar tab here used to read as something nobody played,
and [texture.py](musiccopilot/texture.py) recovers it once for both.

**Printed, an ungrouped strum is an arpeggio.** `tabs` puts a note in the
column its onset falls in, and a separated stem delivers a strum smeared over
58ms at the median and 151ms at the 90th percentile (a real multitrack's
rhythm track: 17ms and 66ms) - one to two sixteenth columns at any ordinary
tempo. So one strummed chord came out as a cascade down the staff. `align`
pulls each group onto the earliest of its onsets: the moment the pick reached
the first string, which is the beat the player counted and the only one of
them that is not late. Ends are left alone - a chord whose bass note rings
under a damped top is real, and the tab reads onsets.

**Clustered, an ungrouped strum is split between players.** Six strings and
one pick is one player, so a chord's notes were all played by whoever played
any of them - and a cue read off a single note does not know that. See "One
separated stem can hold several guitarists".

**What makes a strum.** Two stages: cluster in time, then cut the cluster into
the voicings inside it.

- `link` is the pick crossing one string to the next; `span` is how long the
  whole stroke may take. **The chained one is load-bearing.** A window anchored
  on the first note cannot tell a chord from a fast run at all - a sixteenth
  at 120bpm is 125ms, inside any window wide enough to catch a smeared strum -
  while a strum's *successive* onsets are pick-travel apart whatever the tempo.
  Measured on a legato solo over a strummed part, an anchored 120ms window
  welded 4% of the solo's notes into chords that were never played; chained at
  60ms it welds none. `link` is calibrated on real DI takes rather than on a
  synthetic strum, which is faster than anyone plays: 0.06 finds chords in 54%
  of a rhythm take's events against 49% at 0.04, leaves a lead take at 5%, and
  is the last value before a legato solo starts to fuse.
- **No two adjacent pitches in the cluster may be more than `max_gap` apart**,
  or it is cut there. Not a fitted threshold but a fact about guitars: a
  voicing sits on adjacent strings, so its sorted pitches are 3-7 semitones
  apart. Across all 236 intervals in `OPEN_CHORDS` and `MOVABLE_SHAPES` the
  widest gap any shape has is 11 (the one `maj7` with a muted string) and 99%
  are 8 or under, so an octave is wider than every chord this repo can print.
  A wider gap means a chord and *something else sounding over it*.

Cutting the cluster afterwards, rather than testing each note as it joins, is
what makes the grouping independent of the order notes arrive in: a note an
octave and a half over a bare fifth looks unreachable until the third note of
the chord turns up in between.

**Ground truth.** A known power-chord part and a known lead line, rendered
separately, summed, transcribed, and every transcribed note traced back to the
part it came from - panned apart, dead centre, and a fast legato solo ringing
into itself. The measure is the *ceiling* the grouping puts on any later
attempt to separate the two players. Simultaneity alone caps it at 78%,
because a fifth of the groups already hold both; the octave cut lifts that to
95-99% while still finding a chord in about half of all events.

Two tests were built, measured and dropped: requiring a group's notes to *ring
together* changed nothing (a legato line overlaps exactly as a chord does),
and asking `tabs._placement` whether one hand could hold the group bought
nothing at all - 79.2% against 79.2% - because the open low E and open high e
are two octaves apart and perfectly reachable, so "one hand could play it" is
not "one hand did".

**It is deliberately not part of `clean.py`.** That module's whole contract is
that it only ever *removes*, because everything it knows is evidence that
something is not there. This one only ever *moves*, on evidence that is
musical rather than acoustic. They run one after the other -
`pipeline._shaped` is the pair - and stay separate modules for that reason.
`notes_clean.json` records both revisions as `[clean, texture]`, and a
mismatch in either is a cache miss exactly as a different backend is; a bare
int is what the file held before `texture` existed and reads back as
`[n, 0]`. `align` is idempotent by construction (a second run finds the same
groups, already flush), so re-running it over notes that have been through it
changes nothing.

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

`voices.split_voices` is caught by the same trap for the same reason and works
around it the same way — see "One separated stem can hold several guitarists".

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

### One stem, two parts, one reader

`voices.py` splits a stem when two *people* played into it. `tabs.split_melody`
(`tab --voice`, `?voice=` on `/api/songs/{id}/tab` and `/score`) answers the
question that is left when they did not: one guitarist plays a riff and strums
a chord, and the tab of the verse is both at once on the same six strings, with
the line you were trying to learn somewhere inside it.

It chooses one note per event by Viterbi, paying for melodic leaps, for sitting
below the top of its own chord, and for being the quiet note in it. **The leap
term is what does the real work.** A strummed chord's top note and a lick's next
note are indistinguishable one event at a time and only look different as a
path; taking the highest note of every event instead (a skyline) hops onto
whichever chord tone happens to be on top and reads as an arpeggio, which is the
thing this exists to get out of the way. The weights are balanced against each
other on purpose — an octave below the top of your own chord costs about what an
octave leap in the line costs — so neither "always take the top note" nor "never
move" can win outright.

It is **display-time and changes no cache**, like `--llm-clean`: both halves are
notes the stem really contains, so this is a reading and not a finding. `all` is
byte-for-byte what the tab was before the option existed. `cli._voice` is the
one implementation and `scriptum/app.py` calls it, for the same reason
`app._window` calls `cli._window`: which notes "melody" means must not differ
between the browser and the terminal.

### Fretboard placement is position-aware

`tabs.fret_notes`' Viterbi state is the **hand position**, not the individual
note's `(string, fret)`. A guitarist keeps the left hand in a four-fret box
(`BOX`) and crosses strings inside it; costing each note only against the
previous one misses that, because every pitch is reachable somewhere on the
high E string, so a purely local optimiser walks the melody up that one string
and climbs the neck. That is what put crystallize's solo at frets 0–3 on the
high E when it is actually played at 5–9 on the B string, and why it looked
like it drifted sharp in the second half.

**Each event is placed as a whole against each candidate hand** (`_placement`,
a bitmask DP over which strings are still free, memoised because a song plays
the same chord in the same position over and over). It used to anchor the
event's lowest note and then find somewhere for the rest, and that is what
produced the tab's worst lie: B3 + E4 + G4 took the open B and open e for the
first two notes, leaving G4 nothing under the hand but the **twelfth fret of
the G string** — while the shape a guitarist actually plays (G4 / B5 / e3,
three fingers in one box) was never considered, because the anchor had already
been committed. Placing an event jointly cannot strand its own later notes.

The same search is what lets a note be **left out** (`_DROP_COST`) rather than
printed at a fret nobody would reach for. It is offered only to a note in a
*chord*, and that restriction is the whole safety of it: a chord can have more
notes than the hand has strings (a phantom partial stacked on a six-note strum
leaves nothing free but the far end of the neck), while a single note always
has somewhere to go, and hiding one because the hand happens to be elsewhere
would silently delete the highest note of a phrase. Whether a lone high note is
real is `clean.py`'s question, not the fretboard's.

`_open_penalty` is the other half. Open strings are the cheapest thing on the
neck, so on a short window they win by default even for a lead line at the
fifth fret. The penalty scales with the melody's own **10th-percentile** pitch —
a line that dips to an open string once still lives up the neck — which is what
keeps a two-bar window fretting the same way as the whole solo.

**Low positions win ties (`_LOW_BIAS`), and the tie is the common case.**
Everything under the hand costs about the same (0.02 a fret across the box), so
the open shape of a strummed chord and a barre seven frets up come out within a
hundredth of each other — and `_hand_cost` then makes that accidental choice
permanent, because coming back down a fifth costs 4.5 and no per-event saving
of 0.02 repays it. One event that genuinely wants a high hand dragged the rest
of the passage with it: crystallize's verse went up to the seventh at bar 33
and printed the open `E5` it is built on as a five-string barre for the rest of
the part. A bias of 0.02 per fret settles the tie towards the nut and is far too
small to move a real solo, which costs several points down at the nut. The
open-string term in `_position_cost` is bounded (`min(hand, BOX)`) for the same
reason — letting go of the box is no harder from the twelfth fret than from the
fifth, and unbounded it charged an open voicing more than a barre.

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

### Engraved notation is a second renderer, not a better staff

[score.py](musiccopilot/score.py) turns notes into *written* music — bars of
note values with rests, ties, accidentals, a key signature and one or two
clefs — and the browser engraves it with VexFlow (`ScoreSheet.vue`). It does
not replace `tabs.StaffLayout`: that is a time-proportional grid the terminal
can print and `--follow` can put a cursor on, and it stays the CLI's renderer.
The score is what a reader expects to see, and it only exists on the web.

The division of labour is the tab grid's rule one level up. Every *musical*
decision is Python's — which hand a note is on, what value it is written as,
how it is spelled, where the rests fall — and the client only draws glyphs.
So the pieces worth knowing:

- **The column grid is the tab's grid.** `col_of` is copied from `TabLayout`
  verbatim, so a score and a tab of the same passage agree about where bar 17
  is. `subdiv` is the rhythmic resolution the notation is read at; the web UI
  exposes it as "Rhythm" because 16ths off a noisy transcription are a wall of
  ties and 8ths are readable.
- **Gaps under an eighth are not rests** (`_events`). A note released a
  sixteenth early is a finger lifting, not a rest, and writing it as one turns
  every bar into note-rest-note-rest confetti that reads nothing like what was
  played.
- **Notes and rests obey different alignment rules** (`_fits`, `value_for`).
  A quarter or shorter goes wherever it lands — an eighth on the second
  sixteenth of a beat is ordinary rhythm — but a half note may not start on
  beat 2 and hide where beat 3 is. Rests are stricter still (undotted, aligned
  to their own length), because showing where the beat is *is* a rest's job.
  Being loose about short values is what stops late onsets shattering a bar
  into tied fragments; being strict about long ones is what keeps the bar
  readable.
- **Spelling comes from the key signature** (`spell`). Candidates are ranked
  by: the signature's own spelling first (it needs no printed accidental),
  then fewest accidentals, then the direction the key points. That is why C
  major writes C# where B♭ major writes D♭, and why E major writes F natural
  rather than E#. `_MAJOR_SIG`/`_MINOR_SIG` also re-spell the key itself —
  `detect_key` only ever names sharps, so without them a song in B♭ would be
  engraved with ten sharps. *Which* accidentals actually print is VexFlow's
  call (`Accidental.applyAccidentals`, per stave): that is a convention about
  the bar, not a fact about the note.
- **A grand staff is earned** (`_split_hands`). Two staves need notes on both
  sides of middle C *and* a range no one hand covers; otherwise a bass line
  gets an empty treble stave above it, which is harder to read than the single
  stave it should have had.

On the drawing side, `ScoreSheet.vue` packs measures into systems in two
passes — ask each bar how narrow it can be (`preCalculateMinTotalWidth`), fill
a line, hand the slack back in proportion — and measures each system's own
vertical reach. Two things there are load-bearing: `new Stave(x, y, w)` puts
the top of VexFlow's *reserved band* at `y`, not the top staff line
(`STAVE_HEAD` measures that band once and subtracts it), and the reach is
computed **per system**, because one high note at the top of the page would
otherwise buy that much headroom for every line — and a chord symbol stranded
eighty pixels above its own bar reads as the previous system's chord.

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

### Scriptum: the web front end

`scriptum/` (FastAPI) + `web/` (Vue 3 + Vite). Run it with `python -m scriptum`;
the built client is served from `web/dist`, so **a front-end change needs
`npm run build`** before it shows up on :8420 (or use `npm run dev` on :5173,
which proxies the API across).

The layer is deliberately thin, and the reasons it stays thin are worth keeping:

- **No musical decision lives here.** `app._window` calls straight into
  `cli._window`, and `_grid_cols` into `cli._grid_cols`, so a passage means the
  same thing in the browser as on the command line — including `1:02` and
  `bar17`. If those diverge, the browser and the terminal disagree about what
  "bars 17-24" is, which is worse than either being wrong alone.
- **Same cache, same ids.** A song's id is its file stem, which is also its
  `analyzed_songs/<id>/` folder, so the CLI and the web app see each other's
  work. `library.find` matches ids against file stems and never walks outside
  the library root; `app._safe` keeps media requests inside one song's folder.
- **`_song()` refuses instead of computing.** `cli._load` silently runs the
  whole pipeline when the cache is cold, which is right for a terminal and
  wrong for a request — the browser gets a 409 `not_analyzed` and an explicit
  Analyse button rather than a five-minute hang.

**The tab grid is computed in Python, drawn in JavaScript.**
`serialize.layout_json` reads geometry off a `TabLayout`/`StaffLayout` —
columns with their absolute time, bar number and chord, cells addressed by
(row, column) — and `TabGrid.vue` only maps a column index to an x position.
Do not reimplement `col_of` or the bar-grid maths in JS: it is load-bearing
(see "Grid columns"), and a second copy silently drifts from the first. The
layouts keep their source notes (`TabLayout.fretted`, `StaffLayout.notes`)
purely so a cell can carry its technique and pitch without re-deriving them.
Rows come out in *rendered* order (high string first), matching `line_rows`.

`/api/songs/{id}/score` is the same arrangement for engraved notation
(`serialize.score_json` over `musiccopilot.score`, drawn by `ScoreSheet.vue`);
it takes the same window vocabulary because it goes through the same
`cli._window`. The client picks the endpoint from how the stem is being read —
a fretless stem defaults to `Sheet`, a fretted one to `Tab` — and both
payloads carry `notes`, so the `Notes` ruler works off either.

**"Whole song" has to say so.** `cli._window` reads a request with no part,
bars, start or end as *the first twenty seconds* — right for a terminal that
would otherwise print a four-minute tab, wrong for a browser. Both
`PlayAlongView.windowParams` and `TabsView.windowParams` therefore send an
explicit `start=0` and `end=duration` when no passage is chosen. Without it the
play-along stops dead twenty seconds in, with the cursor running off the end of
a grid that has nothing left to show. That window also arrives with the song
rather than the route, so both views re-load once `analysis` lands.

**A long tab is virtualised; a long score is not.** A whole song at sixteenths
is ~2000 columns, and with several instruments on screen the cursor's per-frame
class updates touched more nodes than a frame has time for — so `TabGrid` puts
only the columns near the viewport in the DOM. `ScoreSheet` does not need it:
engraving all 132 bars of `crystallize` takes ~180ms and the cursor is a
sibling overlay, not a class on every glyph. (If a page ever *seems* to take
fifteen seconds to draw a score, check what `networkidle` is waiting for — the
mix mp3 is minutes long.)

**Following a seek is not the same as following playback.** Both `TabGrid` and
`ScoreSheet` scroll instantly for a long jump and glide for a short one.
`TabGrid` also tracks the scroll it asked for (`aiming`): a smooth `scrollTo`
does not move `scrollLeft` until it lands, so re-issuing it every frame — as
the first version did — cancels and restarts the animation forever, and the tab
creeps or stops.

The web renderer draws columns at a **uniform** width where the ASCII one sizes
each to its widest cell. That is a deliberate divergence: on screen it makes
the x axis proportional to time, so spacing reads as rhythm. `layout["text"]`
still carries the exact ASCII rendering for copy/paste.

**The vocal line gets the words as well as the notes.** A vocals tab is a row
of note names, which tells a singer the melody and nothing about what they are
singing — so `LyricStrip.vue` puts the transcribed lines under it, reading
`cursorTime` off the same shared transport as `TabGrid` and `ScoreSheet` so the
words cannot drift from the notes above them. The current line stays lit until
the *next* one starts rather than going out when it ends: Whisper's segments
have gaps between them (a breath, an instrumental bar) and a highlight that
blanks in those gaps reads as having lost your place at exactly the moment you
look down to find it. Timing is per line, because per line is all Whisper gives.

It is offered for the stem literally called `vocals` and not for anything whose
`base_stem` is vocals: `pipeline.run` transcribes lyrics from that one stem, so
a backing vocal imported as `vocals-2` sings different words and has none of its
own. Showing the lead's words under it would be a confident lie.

**Play-along runs on one clock.** `useTransport` owns a single `<audio>`
element and every visible tab reads its `currentTime`; several instruments can
be on screen at once and must not drift apart. Position is sampled from the
element on `requestAnimationFrame`, never from a `Date.now()` clock started
next to it — the same reason `playalong.Transport` reads the audio callback's
frame counter, and the same failure if you don't.

**The engine choice is the client's, the engine list is the server's.**
`/api/transcribers` reports `notes.backend_status()` — whether torchcrepe
imports is a fact about the machine Scriptum runs on, so the settings pane
never keeps its own copy of the list. The preference itself is browser-local
(`composables/useSettings.js`, one shared reactive object so the pane and the
song pages cannot disagree), and it stores `null` for "follow the server's
default" rather than freezing today's default into every browser.
`POST /api/songs/{id}/transcribe` is `Song.retranscribe` as a job: `SongView`
offers it whenever a song's `note_backends` disagree with the chosen engine,
which is the normal state of any song analysed before the setting existed.

**The BandLab door is the one thing the browser uploads.** Every other import question is
about the *server's* filesystem, which is right when Scriptum is on the machine the session is
on — a `.band` is a package a file input cannot carry anyway. BandLab is the case where it is
not: there is no public API to pull a project's tracks from, so the handover is a download,
and it comes out of whatever browser was doing the downloading. `POST /api/daw/upload` takes
those files (or a folder, walked through `webkitGetAsEntry`, or a zip) into a staging folder
under `library_root()/.imports` and returns its path, so `daw_preview` and `daw_import` read it
exactly as if someone had pointed at it — the import path does not learn a new shape. Staging
is scaffolding: `import_session` transcodes every track into `stems/` anyway, so anything older
than a day is pruned on the next upload, and `mkdtemp` names the folder because two uploads a
second apart would otherwise merge into one.

**Slow work is a job, not a request** (`jobs.py`, progress over SSE from
`/api/jobs/{id}/stream`). Analysis is minutes; the Gemini calls are tens of
seconds and *variable* — the same 75-note cleanup measured 48s once and over
110s the next time on identical input, which the fixed thinking budgets have
since brought to ~11s (see "What a Gemini button is allowed to cost"), but a
model that decides to think is still not a request you hold open. `clean_solo`
and `suggest_solo` have no timeout of their own, so `Jobs.start` takes a
`timeout` that is a
**reporting deadline, not a kill**: Python cannot interrupt a thread blocked in
a socket read, so the job is marked failed, the orphaned daemon thread is left
to die with the process, and a late result is discarded rather than
overwriting the reported failure. `SCRIPTUM_LLM_TIMEOUT` overrides the 300s
default. One analysis per song at a time, or two runs race on the same cache.

**Deep links need the SPA fallback — and a built asset must not get it.**
`StaticFiles(html=True)` only serves `index.html` for a *directory*; every
other miss is a 404, so reloading `/song/x/tabs` broke until `_SPAFiles` fell
back to the shell. It deliberately does not do that for `/api` or `/ws`, so a
mistyped endpoint still fails as an endpoint instead of quietly returning HTML.

Anything under `assets/`, and anything with a built file's suffix
(`_SPAFiles.ASSETS`), is excluded for a sharper reason. Vite fingerprints every
lazy route into its own chunk, so a tab left open across an `npm run build`
asks for `LibraryView-<old hash>.js`, which no longer exists — and answering
*that* with the shell hands the browser HTML where it asked for a JavaScript
module. The import rejects, vue-router abandons the navigation, and the link is
**dead for the life of that tab**, with nothing in the network log but a 200
and nothing on screen at all. It presents as "I cannot click on Library any
more". A real 404 is what makes the failure identifiable, and `main.js`
(`router.onError`) then reloads once onto the current build — guarded by a
`sessionStorage` key cleared on the next successful navigation, so a chunk that
is genuinely broken cannot become a reload loop. The two halves only work
together: without the 404 the client sees a MIME-type error it cannot
distinguish, and without the reload the 404 is merely a legible dead end.

**The mic is the server's.** Both live panes open `record.Recorder` in the
server process and stream frames over `/ws/live`, reusing `analysis_worker`
unchanged — so the machine running Scriptum is the one in the practice room.
The now-playing readout comes off the pitch contour, not the committed notes,
because no committed note ever covers the present moment (see the recording
notes above). `live.KeyTracker` recomputes the key on its own slower cadence
for the same reason chords are rarer than pitch in `analysis_worker`:
`detect_key` runs HPSS and costs far more than one analysis period.

### Musical constants

[config.py](musiccopilot/config.py) is the single source for tunings, fret limits, per-instrument
pitch windows, the chord vocabulary, open-chord voicings and movable barre shapes. `tabs.py`
consumes these both for placement cost and for printing fingerings; don't hardcode intervals
or tunings elsewhere.

### Gemini

[gemini.py](musiccopilot/gemini.py) uses structured output — the pydantic `Solo` schema is passed
as `response_schema`, so prompt changes must keep the schema satisfiable. Model comes from
`GEMINI_MODEL` (default `gemini-3.5-flash`), except cleanup, which uses the cheaper
`GEMINI_CLEAN_MODEL`; `listening_notes` uploads the mp3 via the Files API.
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

#### What a Gemini button is allowed to cost

Everything else in this repo is computed locally, so these three calls are the
entire bill, and each one is expensive for a different reason. The limits are
in [config.py](musiccopilot/config.py) with the rest of the constants.

**Thinking tokens were the dominant cost, not the model.** Nothing here set
`thinking_config`, so every call thought on the automatic budget — which is
what the "same 75-note cleanup measured 48s once and over 110s the next time"
note in the Scriptum section was actually describing, and why a cleanup could
sit for three minutes and come back `RemoteProtocolError: Server disconnected`.
Thinking is billed as output *and* counts against `max_output_tokens`, so the
two have to be set together: a budget that eats the ceiling truncates the JSON
and the parse fails after you have paid for it. With `LLM_CLEAN_THINKING=512`
the same window is ~11s on either model. Do not remove the budgets to "let it
think about it" — a cleanup is a pass over given data, not a decision.

**`clean_solo` is billed twice over, so it is a snippet operation.** The model
is sent the window and writes it back, and the shape of the request is what
makes size fatal rather than merely costly:

| window | notes | in | out |
|---|---|---|---|
| `guitar solo` part | 75 | ~4.4k | ~5k |
| whole song, `guitar` | 1438 | ~65k | ~65k |

A whole song is not a bigger version of the intended request, it is about a
hundred times the bill of it. And a whole-song window is the *default* on the
Tabs page, because `cli._window` reads "no passage" as the first twenty
seconds and `windowParams` therefore has to send `start=0, end=duration`
explicitly (see the Scriptum section) — so the expensive request was one click
on a page that had never been asked which passage it meant.

`clean_solo` raises `TooLongToClean` and **the limit is enforced there**, not
at each caller, for the same reason `app._window` calls `cli._window`: the
browser and the terminal must not disagree about what a snippet is.
`clean_window_cost` answers the same question without spending anything, so
the front end can grey the button out (`clean_ok`/`clean_size` ride along on
the tab payload) and `clean_tab` can refuse with a 400 up front rather than
letting the user watch a job fail. `cli._llm_clean` reports the refusal and
returns the raw notes — the tab is still worth printing.

`notes_to_solonotes` **rounds**, because these numbers are sent and every digit
is a billed token: a raw `(n.start - t0) * bps` prints as `14.341232328869047`,
17 significant figures of a float that came off a 10ms CREPE frame. Three
decimals of a beat is 1.5ms at 123bpm. That plus dropping `indent=2` (about a
fifth of the tokens, and the model never reads it) halves the payload.

**`listening_notes` is the one call billed by the length of the song**, not the
size of a passage: it uploads the mp3 and Gemini charges per second of audio —
crystallize is 6.9k input tokens before a word of prompt. It is cached in
`llm_notes.txt` and has always been opt-in on the CLI (`analyze --llm`), but
both web call sites hardcoded `llm: true`, so every browser analysis bought it
without asking. It is now `settings.llmNotes`, default off, in the settings
pane next to the transcriber. The upload is deleted afterwards — Files API
storage expires on its own after 48h, but leaving every song ever analysed in
the quota serves nobody.

`GEMINI_CLEAN_MODEL` (default `gemini-3.5-flash-lite`) is separate from
`GEMINI_MODEL` because cleanup is mechanical and composition is not. On
crystallize's solo the two tiers agree closely (75 → 69 notes on lite, 75 → 64
on flash, both merging the same jitter runs); set it to `gemini-3.5-flash` if a
cleanup ever looks careless. `suggest_solo` keeps the full model and a real
thinking budget — it is the one call actually being asked to decide something.
