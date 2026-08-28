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
python -m musiccopilot tab song.mp3 --stem piano --bars 17-24

# play along: the passage plays while the tab scrolls under a live cursor.
# --minus-stem drops your instrument out of the mix so you play that part,
# --speed slows playback down without changing pitch, --count-in clicks you in
python -m musiccopilot tab song.mp3 --part "guitar solo" --stem guitar --follow
python -m musiccopilot tab song.mp3 --stem guitar --bars 17-24 \
    --follow --minus-stem --speed 0.75 --count-in 4

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
| tempo/beats | `analysis.py` | librosa beat tracking + onset-energy downbeat phase |
| key | `analysis.py` | Krumhansl–Kessler profile correlation |
| chords | `analysis.py` | beat-synced CQT chroma → 97 templates → Viterbi smoothing |
| form | `form.py` | recurrence-matrix spectral clustering, snapped to bars, named by pop convention |
| structure | `analysis.py` | agglomerative segmentation + KMeans labelling (A/B/C) |
| patterns | `analysis.py` | repeated n-chord loop mining, riff-density windows |
| chart | `chart.py` | one chord loop per role, plus only what differs in each repeat |
| notes | `notes.py` | Basic Pitch (polyphonic), pYIN fallback (mono) |
| lyrics | `lyrics.py` | Whisper on the isolated vocal stem |
| tabs | `tabs.py` | Viterbi over fret positions minimising hand travel |
| solos | `gemini.py` | Gemini structured JSON output → notes → tab + MIDI + audio |
| sound | `synth.py` | additive osc on a pitch curve (bends/slides/vibrato) + amp sim |

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
- **Tabs & Notes** — any stem, any passage (a named part, `17-24`, or a time
  range), as a real fretboard for guitar and bass and as a staff for anything
  without strings. "Clean up" runs the Gemini pass from `--llm-clean`.
- **Play along** — several instruments at once, scrolling under one cursor,
  with speed, count-in, loop, and drop-your-instrument-out-of-the-mix.
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
- The synth is a stylised approximation, not a sampled guitar. Load the exported
  `.mid` into a DAW with a real guitar VST if you want the good sound.
