# MusicCopilot

Drop in an mp3 → get stems, chords, notes, lyrics, the song's form (verse,
chorus, solo…) with an audio snippet per part, guitar/bass tabs, and
Gemini-generated solos you can actually listen to. CLI only for now.

## Install

The ML deps (torch/demucs/basic-pitch) do **not** support Python 3.14 yet — use 3.11:

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg            # librosa needs it for mp3
export GEMINI_API_KEY=...      # your Gemini key
```

## Use

```bash
# already have the multitrack? import it instead of separating it.
# --dry-run first: it prints what each track will become, and writes nothing
python -m musiccopilot import "Band Practice.band" --dry-run
python -m musiccopilot import "Band Practice.band" --analyze
python -m musiccopilot import ./bandlab-stems --map "Acoustic=guitar-2"
python -m musiccopilot tracks song.wav --map "Track3_VoiceAudio=vocals"

# one slow pass (stems + chords + notes + lyrics + form), cached per song
python -m musiccopilot analyze song.mp3 --llm

# the song's shape: what repeats, where, on which chords
python -m musiccopilot parts song.mp3

# the minimal sheet you need to play it: one chord loop per part, the
# fingerings, what changes in each repeat, the words, tabs for the solos
python -m musiccopilot chart song.mp3

# everything the analysis found
python -m musiccopilot show song.mp3
python -m musiccopilot show song.mp3 --what chords

# tab a part by name - no need to know which bars it is
python -m musiccopilot tab song.mp3 --part "guitar solo" --stem guitar --play
python -m musiccopilot tab song.mp3 --part chorus2 --stem bass

# ...or by timestamp, or by bar
python -m musiccopilot tab song.mp3 --stem guitar --start 1:02 --end 1:18 --audio --play
python -m musiccopilot tab song.mp3 --stem guitar --bars 17-24

# piano (or vocals, or anything else with no fretboard) prints as a text
# staff instead of a fretboard - clef is picked automatically from the notes
python -m musiccopilot tab song.mp3 --stem piano --bars 17-24   # staff, not frets

# play along: the passage plays while the tab scrolls under a live cursor.
# --minus-stem drops your instrument out of the mix so you play that part,
# --speed slows playback down without changing pitch, --count-in clicks you in
python -m musiccopilot tab song.mp3 --part "guitar solo" --stem guitar --follow
python -m musiccopilot tab song.mp3 --stem guitar --bars 17-24 \
    --follow --minus-stem --speed 0.75 --count-in 4

# which note transcriber this install can run, and re-read one song's notes
# with a different one (cheap: stems, chords and form are left alone)
python -m musiccopilot transcribers
python -m musiccopilot transcribe song.mp3 --backend crepe --stem guitar
python -m musiccopilot analyze song.mp3 --backend crepe

# ask Gemini for a solo over the solo section, hear it over the real backing
# track (the song minus the guitar stem)
python -m musiccopilot solo song.mp3 --prompt "slow bluesy, lots of bends, build to a scream" --play
python -m musiccopilot solo song.mp3 --prompt "fast legato, dorian" --part bridge --over chords
```

## What you get

Everything lands in `analyzed_songs/<song>/`, next to the audio file:

| file | what it is |
|---|---|
| `chart.md` | the recreate sheet - form, chord loops, fingerings, words, tabs |
| `form.json` | the parts: role, bar range, timestamps, chord loop, key, variations |
| `snippets/03_chorus-1.wav` | every part cut out as its own audio |
| `snippets/03_chorus-1/guitar.wav` | ...per instrument, with `--stem-snippets` |
| `analysis.json` | tempo, beats, key, chord track, raw segmentation |
| `stems/*.wav` | drums, bass, other, vocals, guitar, piano |
| `notes/*.json` | transcribed notes per stem |
| `lyrics.json` | Whisper transcript of the vocal stem |

Positions can be written three ways, so you can use whichever you have to hand:
`--start 62` (seconds), `--start 1:02` (mm:ss), `--start bar17`, `--bars 17-24`,
or skip them entirely with `--part chorus2`.

## How it works

| step | module | approach |
|---|---|---|
| stems | `audio.py` | Demucs `htdemucs_6s` → drums, bass, other, vocals, guitar, piano |
| import | `daw.py` | a GarageBand `.band` or a folder of stems → the same six names, skipping Demucs |
| tempo/beats | `analysis.py` | librosa beat tracking + onset-energy downbeat phase |
| key | `analysis.py` | Krumhansl–Kessler profile correlation |
| chords | `analysis.py` | beat-synced CQT chroma → 97 templates → Viterbi smoothing |
| form | `form.py` | recurrence-matrix spectral clustering, snapped to bars, named by pop convention |
| structure | `analysis.py` | agglomerative segmentation + KMeans labelling (A/B/C) |
| patterns | `analysis.py` | repeated n-chord loop mining, riff-density windows |
| chart | `chart.py` | one chord loop per role, plus only what differs in each repeat |
| notes | `notes.py` | pick one: Basic Pitch (polyphonic, default), CREPE (mono, keeps bends), pYIN (mono, lightest) |
| lyrics | `lyrics.py` | Whisper on the isolated vocal stem |
| tabs | `tabs.py` | Viterbi over fret positions minimising hand travel |
| solos | `gemini.py` | Gemini structured JSON output → notes → tab + MIDI + audio |
| sound | `synth.py` | additive osc on a pitch curve (bends/slides/vibrato) + amp sim |

## Importing from GarageBand and BandLab

If the band already recorded the parts separately, there is nothing to separate.
`import` writes those tracks in as the stems and everything downstream — chords,
form, tabs, play-along, the web app — runs exactly as it does on a mix, minus the
slowest and least reliable stage. Two guitarists stay two guitarists: they become
`guitar` and `guitar-2`, each with its own notes and its own tab.

**BandLab** — in the Studio, Project → Download → Tracks (WAV). The tracks come
down one at a time, so collect them into a folder and point `import` at it (a
zip of that folder works too). Nothing is guessed. In the web app they can be
dropped straight onto the BandLab tab of **Import multitrack**, which is the
only door that uploads: BandLab has no public API to pull a project from, so
the download *is* the handover, and it happens in whatever browser you were in.

**GarageBand** — point `import` at the `.band` project itself. On current macOS
`~/Music/GarageBand` is TCC-protected, and the permission is filed under the app
that *launched* Scriptum (your terminal, or VS Code) rather than under Scriptum
— which is why "grant Scriptum access" is not a thing you can do. Either give
that app Full Disk Access, or drag the project to `~/Downloads` in Finder and
open the copy, which needs no permission at all. GarageBand has no
stem export (the official route is soloing each track and exporting it, one at a
time), but the project is a folder and the recorded takes are inside it, so this
reads them straight out. It assumes every region starts at bar 1, which is true
of a practice-room take — one pass, everyone playing through — and not true of an
edited project. Bounce the mix in GarageBand too if you want the band's own fader
balance; otherwise the stems are summed.

Track names are matched to instruments (in English and German), and `--dry-run`
shows the whole mapping before anything is written:

```
Bass DI         ->  bass        (matched 'bass')
Drum Kit OH     ->  drums       (matched 'drum')
Gesang          ->  vocals      (matched 'gesang')
Gtr Nik         ->  guitar      (matched 'gtr')
Rhythm Gitarre  ->  guitar-2    (matched 'gitarre'; guitar was taken)
```

Fix any row with `--map "Rhythm Gitarre=piano"`. Imported stems are never
re-separated, `--force` included.

A row can also be corrected **after** the import, which is the case that
matters when the mistake is only visible in the result — a vocal track read as
a guitar looks like an ordinary mapping until the Lyrics tab comes back empty:

```bash
python -m musiccopilot tracks song.wav                     # what each track became
python -m musiccopilot tracks song.wav --map "Track3=vocals"
```

The stems are renamed in place (the audio was always right, only the labels on
it were wrong) and only what the labels were load-bearing for is read again — a
change of instrument re-does the chords, notes and lyrics; swapping two
guitarists' numbers re-does only the form. In Scriptum it is the **Which track
is which** button beside the song title.

## How the form is worked out

`form.py` looks for the shape of a western pop/rock arrangement:

1. **Repetition, not novelty.** A beat-synchronous recurrence matrix over CQT
   chroma, balanced against a timbral path matrix, then spectral clustering of
   its normalised Laplacian - so material that comes back gets the same label.
2. **Snapped to bars.** Labels are majority-voted per bar and boundaries are
   nudged onto the four-bar grid, because pop sections are multiples of four.
3. **Consistent repeats.** Each occurrence of a block is trimmed back to the
   bars that fit that block's chord loop, in that occurrence's own key. What is
   left over is kept as a part in its own right - which is how a four-bar
   pre-chorus, always glued to the verse by timbre alone, gets its own line.
4. **Named by convention.** The chorus is the loud block that comes back with
   the *same words*; the verse comes back early and often with *different*
   words; a pre-chorus keeps handing over to the chorus; a bridge turns up late
   and once. Instrumental blocks are read off position and note density, so a
   busy one in the middle is a solo and a quiet one at the end is an outro.
5. **Compared against each other.** Repeats are matched by cycling the loop and
   transposing it, so a lifted last chorus reads as "same loop, a whole step
   higher" instead of a different part.

## Tuning the results

- Notes wrong? Try another transcriber. **Basic Pitch** (the default) hears
  chords, so it is right for rhythm parts and wrong for a solo, where it
  invents extra pitches and chops a bend into steps. **CREPE** tracks one
  continuous pitch, so it reads bends, slides and vibrato as techniques —
  best for solos, bass and vocal melodies, wrong for anything chordal.
  **pYIN** is the lightest and hears neither. Choose in Scriptum's Settings
  pane, or with `--backend`; `transcribe` re-reads one song without redoing
  the slow separation. (Solos are re-read monophonically either way.)
- Form off? The knobs are in `FORM` in `config.py` - `min_bars`, `k_range`
  (how many kinds of material to look for), `vocal_threshold`, `solo_density`.
- Chords sound smeared? `detect_chords(..., self_prob=0.7)` for faster changes.
- Tabs in the wrong position? adjust `_position_cost` / `_move_cost` in `tabs.py`.
- Solo too tame? `--temperature 1.4`, or be far more specific in `--prompt`.
- `GEMINI_MODEL=gemini-2.5-flash` for cheaper/faster solo drafts. Run
  `python -m musiccopilot models` to see what your key can reach — if a Gemini 3
  id is listed, it is worth switching to for solo quality.

## Scriptum — the web front end

Everything above, in a browser. Dark, quiet, and built around the two things
you actually do with a song: work out how it goes, and play along with it.

```bash
pip install -r requirements.txt      # now includes fastapi + uvicorn
cd web && npm install && npm run build && cd ..
export GEMINI_API_KEY=...            # optional: solos and tab cleanup
python -m scriptum                   # → http://127.0.0.1:8420
```

`--host 0.0.0.0` makes it reachable from a phone or tablet on the same
network; `--library ~/Music/band` points it at a different folder of songs.
For front-end work, `cd web && npm run dev` runs Vite on :5173 and proxies the
API to :8420, so both halves reload independently.

What is in it:

- **Library** — drag a song in; it uploads and starts analysing, with every
  pipeline stage streamed to the page as it happens.
- **Structure** — the arrangement drawn to scale, one block per part, with a
  play button on each so you can hear a section without hunting for it. Open a
  part for its bar-by-bar chords, the fingerings, and the words sung over it.
- **Tabs & Notes** — any stem, any passage (a named part, `17-24`, a time
  range, or the whole song), as a real fretboard for guitar and bass and as
  engraved sheet music for anything without strings: grand staff, key
  signature, beams, rests, ties, chord symbols. Guitar and bass can be read
  either way. "Clean up" runs the Gemini pass from `--llm-clean` — pick a part
  or a bar range first, it is capped to a passage rather than a whole song.
- **Play along** — several instruments at once, tabs and sheet music side by
  side, scrolling under one cursor, with speed, count-in, loop, and
  drop-your-instrument-out-of-the-mix. Drag the progress bar and every part
  jumps to that moment.
- **Lyrics**, **Chart** — the transcript grouped under its sections, and the
  recreate sheet.
- **Solo** — describe what you want and hear it over the real backing track.
- **Live tab** — point the mic at the room and read what is being played. For
  the practice room: what is the bass player doing?
- **Live key** — the same, but it tells you the key and lights up the notes
  that work on your neck, so you can join in.

The mic for both live panes belongs to the **server**, so run Scriptum on the
laptop that is in the room. The browser only draws what it is sent.

### How it fits together

`scriptum/` is a thin FastAPI layer; no musical decision is made in it. Window
resolution goes through `cli._window`, so `bars 17-24` means the same thing in
the browser as in the terminal, and the analysis cache is the same
`analyzed_songs/<song>/` — a song analysed from the CLI opens in the browser
already done, and vice versa.

Sheet music follows the same rule one level up. `musiccopilot/score.py` decides
the music — which hand a note is on, what value it is written as, how it is
spelled against the key, where the rests fall — and the browser only engraves
what it is handed. Nothing in JavaScript may decide that a note is an eighth;
by the time it gets there, it already is one.

Tabs are drawn from a **grid the Python side computed**: `serialize.layout_json`
reads columns, bar lines, chord positions and cells straight off `TabLayout` /
`StaffLayout`, and the Vue component only maps a column index to an x
position. The column maths is load-bearing (see CLAUDE.md's "Grid columns"),
and a second copy of it in JavaScript would be a second copy to keep in step.

Slow work runs as a job on a worker thread and reports progress over SSE, since
`analyze` on a cold song is minutes and the Gemini calls are tens of seconds.

## Known limits

- Chord detection is template-based: reliable for triads/sevenths, not for
  dense jazz voicings or slash chords. Chart lines are a consensus over the
  bars of a loop, so they survive the noise better than the raw chord track.
- Part names are conventions, not ground truth. A song that does not follow
  verse/chorus convention gets `Section A/B` names and honest chord loops.
- Basic Pitch on a distorted guitar stem picks up harmonics as extra notes;
  riff tabs need a human eye.
- Importing a `.band` assumes each track's audio starts at bar 1: GarageBand
  keeps region positions in an undocumented file this cannot read. Right for a
  practice-room take, wrong for an edited project — export the stems for those.
  Software-instrument tracks leave no audio in the project and need bouncing
  first. A track with several regions keeps the longest and says so.
- The synth is a stylised approximation, not a sampled guitar. Load the exported
  `.mid` into a DAW with a real guitar VST if you want the good sound.
