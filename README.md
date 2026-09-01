# MusicCopilot

Drop a song in → stems, chords, notes, lyrics, the song's form (verse, chorus,
solo…) with an audio snippet per part, guitar/bass tabs and engraved sheet
music, a play-along that scrolls under a cursor while the band plays, and
Gemini-generated solos you can actually listen to.

If the band already recorded the parts separately in **GarageBand** or
**BandLab**, import that multitrack instead and skip the guessing entirely.

Most of this lives in **Scriptum**, the web app — that is the part you will
use. The command line does the same things and is documented at the bottom.

---

## Install

Three things have to be on the machine: **uv** (which brings its own Python),
**Node.js** (which builds the web app), and **ffmpeg** (which decodes mp3).
Everything else is installed by uv. It works the same on Windows, macOS and
Linux; only the three lines below differ.

> The Python side pulls in torch and demucs, so budget **~4 GB of disk** and a
> few minutes on a good connection. The first analysis then downloads the
> separation and speech models on demand, so the first song is slower than
> every song after it.

### 1. Prerequisites

**Windows** (PowerShell):

```powershell
winget install --id astral-sh.uv
winget install --id OpenJS.NodeJS.LTS
winget install --id Gyan.FFmpeg
```

Close and reopen PowerShell afterwards so the new `PATH` is picked up.

**macOS** (with [Homebrew](https://brew.sh)):

```bash
brew install uv node ffmpeg
```

**Linux** (Debian/Ubuntu):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install nodejs npm ffmpeg
sudo apt install libportaudio2          # only for the mic/play-along features
```

`libportaudio2` is what `sounddevice` binds to. Windows and macOS wheels ship
their own copy; on Linux it is a system package, and without it everything
except the live panes still works.

### 2. Get the code

```bash
git clone https://github.com/nwulkow/musiccopilot.git
cd MusicCopilot
```

### 3. The Python environment

The ML dependencies (torch, demucs, basic-pitch) do **not** support Python 3.14
— this project is on **3.11**. You do not need to install 3.11 yourself: uv
fetches it.

```bash
uv venv --python 3.11
uv pip install -r requirements.txt
```

That creates `.venv/` in the repo root, which is exactly where `start.sh` looks
for it. You never have to activate it — `uv run` and `start.sh` both find it.

<details>
<summary>Without uv (plain pip)</summary>

Works, but you must supply Python 3.11 yourself:

```bash
python3.11 -m venv .venv               # Windows: py -3.11 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```
</details>

### 4. The web client

Vue is compiled ahead of time, so the client is built once and rebuilt after
any change under `web/`:

```bash
cd web
npm install
npm run build
cd ..
```

On macOS and Linux `./start.sh` does both of these for you when they are
missing or stale, so you can skip this step there.

### 5. Your Gemini key

Only the three Gemini features need it — solo generation, tab clean-up, and
optional listening notes during analysis. Everything else runs locally and the
app is perfectly usable without a key.

Get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and
put it in a file called `.env` in the repo root:

```
GEMINI_API_KEY=your-key-here
```

One line, no quotes, no spaces around the `=`. `.env` is in `.gitignore`, so
your key does not end up in the repository.

**Nothing in the Python code reads `.env` on its own** — it has to be handed to
the process. `./start.sh` does that for you; on Windows, `uv run --env-file .env`
does. Both are shown below, so as long as you start it one of those two ways,
putting the key in `.env` is all there is to it.

---

## Starting it

### macOS and Linux — `./start.sh`

```bash
./start.sh
```

That one command is the whole thing. It:

- checks that `.venv` exists (and tells you the uv command if it does not),
- loads `.env`, so your Gemini key is in the environment,
- runs `npm install` if `web/node_modules` is missing,
- rebuilds the client if anything under `web/` is newer than `web/dist`,
- starts the server and prints the URL to open.

```
  Scriptum
    app + api   http://127.0.0.1:9000
    api docs    http://127.0.0.1:9000/docs
    library     /Users/you/MusicCopilot
    ctrl-c to stop
```

Open that first URL. Ctrl-C stops everything it started.

| flag | what it does |
|---|---|
| `--port 9100` | serve somewhere else (default `9000`, or `$SCRIPTUM_PORT`) |
| `--host 0.0.0.0` | reachable from a phone or tablet on the same network |
| `--dev` | also run Vite on :5173 with the API proxied — for editing `web/` |
| `--reload` | restart the API when Python files change |
| `--help` | the same list, from the script itself |

Anything it does not recognise is passed through to the server, so
`./start.sh --library ~/Music/band` points it at a different folder of songs.

The first time you run it, make it executable if git did not:
`chmod +x start.sh`.

### Windows

`start.sh` is a bash script. Either run it under **Git Bash** or **WSL**, or
run the two steps it automates directly — which is two lines in PowerShell:

```powershell
cd web; npm run build; cd ..          # only after changing anything in web/
uv run --env-file .env python -m scriptum
```

Then open **http://127.0.0.1:8420** (`python -m scriptum` defaults to 8420
where `start.sh` uses 9000; `--port` changes either).

`uv run --env-file .env` is the part that loads your Gemini key. Without
`--env-file` the app still runs, it just reports no key. To reach it from a
phone on the same wifi, add `--host 0.0.0.0`:

```powershell
uv run --env-file .env python -m scriptum --host 0.0.0.0
```

### From a phone or tablet on the same network

Start it with `--host 0.0.0.0` and open `http://<this machine's IP>:9000` on
the other device. On macOS `start.sh` prints that address for you; on Linux
find it with `hostname -I`, on Windows with `ipconfig`.

Worth knowing: **the microphone belongs to the server.** The two live panes
record on the machine running Scriptum, not on the device showing the page, so
run it on the laptop that is actually in the room and use the phone to read.

---

## Using Scriptum

### Library

The front page. Drag an mp3, wav or flac onto it (or press **Add a song**) and
it uploads and starts analysing, with every stage of the pipeline streamed to
the page as it happens — separation, chords, notes, lyrics, form. On a normal
laptop a four-minute song is a few minutes, almost all of it stem separation.

Audio files already sitting in the library folder show up here too, so a song
you analysed from the command line opens in the browser already done. That
folder is wherever you started Scriptum from unless you passed `--library`, and
only the folder itself is read, not what is nested inside it.

**Analyse** / **Re-analyse** re-runs a song; **Delete** removes it.

### Import multitrack

The **Import multitrack** button on the Library page is the other way in, and
the better one when the band recorded to separate tracks. It has three doors:

- **GarageBand** — the projects it can find on this machine, both the ones open
  in GarageBand right now and the ones sitting elsewhere. Click one. (macOS
  only, which is where GarageBand is.)
- **BandLab** — Project → Download → Tracks in the Studio, then drop those
  files, the folder, or a zip of it straight onto the page. This is the one
  door that uploads from your browser, because a BandLab download is what you
  have and there is no API to pull the project from. It works on any OS.
- **Open from file** — a path on the machine running Scriptum: a `.band`
  project, a folder with one audio file per track, or a zip of one. Type it in,
  or use the Browse buttons (a real Finder dialog, so macOS only — on Windows
  and Linux, paste the path).

Whichever door, you get the **mapping** first — which track became `guitar`,
which became `vocals` — with the reason for each row, and a dropdown to correct
any of them before anything is written. Nothing is imported until you press
**Import and analyse**. There is more on all of this
[further down](#importing-from-garageband-and-bandlab).

### A song

Six tabs across the top:

- **Structure** — the arrangement drawn to scale, one block per part, with a
  play button on each so you can hear a section without hunting for it. Open a
  part for its bar-by-bar chords, the fingerings, and the words sung over it.
- **Tabs & Notes** — any stem, any passage (a named part, `17-24`, a time
  range, or the whole song), as a real fretboard for guitar and bass and as
  engraved sheet music for anything without strings: grand staff, key
  signature, beams, rests, ties, chord symbols. Guitar and bass can be read
  either way. **Clean up** runs the Gemini pass that merges transcription
  jitter and fixes octave slips — pick a part or a bar range first, it is
  deliberately capped to a passage rather than a whole song.
- **Play along** — several instruments at once, tabs and sheet music side by
  side, scrolling under one cursor, with speed, count-in, loop, and
  drop-your-instrument-out-of-the-mix. Drag the progress bar and every part
  jumps to that moment.
- **Lyrics** — the transcript, grouped under the sections it is sung in.
- **Chart** — the recreate sheet: one chord loop per part, the fingerings, what
  changes in each repeat, the words, tabs for the solos.
- **Solo** — describe what you want ("slow bluesy, lots of bends, build to a
  scream") and hear it played over the real backing track, which is the song
  minus your instrument's stem.

Beside the song title, **Which track is which** re-labels the tracks of an
imported multitrack after the fact — see below.

### Practice room

- **Live tab** — point the mic at the room and read what is being played. What
  is the bass player doing?
- **Live key** — the same, but it names the key and lights up the notes that
  work on your neck, so you can join in.

### Settings

- **Transcription engine** — which note transcriber runs, with each one greyed
  out and labelled if this install is missing what it needs. Changing it offers
  to re-read a song's notes without redoing the slow separation.
- **Gemini** — whether analysis also buys listening notes (off by default; it
  is billed by the length of the song), and a note on what Clean up costs.

The engine choice is stored in your browser, so each bandmate can pick their
own.

---

## Importing from GarageBand and BandLab

If the band already recorded the parts separately, there is nothing to
separate. `import` writes those tracks in as the stems and everything
downstream — chords, form, tabs, play-along, the web app — runs exactly as it
does on a mix, minus the slowest and least reliable stage. Two guitarists stay
two guitarists: they become `guitar` and `guitar-2`, each with its own notes and
its own tab.

**BandLab** — in the Studio, Project → Download → Tracks (WAV). The tracks come
down one at a time, so collect them into a folder and drop it on the BandLab
door of **Import multitrack** (a zip of the folder works too), or point the CLI
at it. Nothing is guessed.

**GarageBand** — point it at the `.band` project itself. GarageBand has no stem
export (the official route is soloing each track and exporting it, one at a
time), but the project is a folder and the recorded takes are inside it, so this
reads them straight out. It assumes every region starts at bar 1, which is true
of a practice-room take — one pass, everyone playing through — and not true of
an edited project. Bounce the mix in GarageBand too if you want the band's own
fader balance; otherwise the stems are summed.

On current macOS `~/Music/GarageBand` is protected by the system, and the
permission is filed under the app that *launched* Scriptum (your terminal, or
VS Code) rather than under Scriptum — which is why "grant Scriptum access" is
not a thing you can do. Either give that app **Full Disk Access** in System
Settings → Privacy & Security, or drag the project to `~/Downloads` in Finder
and open the copy, which needs no permission at all. The app says which
application to look for when it hits this.

Track names are matched to instruments (in English and German), and the mapping
is shown before anything is written:

```
Bass DI         ->  bass        (matched 'bass')
Drum Kit OH     ->  drums       (matched 'drum')
Gesang          ->  vocals      (matched 'gesang')
Gtr Nik         ->  guitar      (matched 'gtr')
Rhythm Gitarre  ->  guitar-2    (matched 'gitarre'; guitar was taken)
```

Fix any row in the dropdown beside it (or `--map "Rhythm Gitarre=piano"` on the
CLI). Imported stems are never re-separated, `--force` included.

A row can also be corrected **after** the import, which is the case that matters
when the mistake is only visible in the result — a vocal track read as a guitar
looks like an ordinary mapping until the Lyrics tab comes back empty. That is
the **Which track is which** button beside the song title, or:

```bash
python -m musiccopilot tracks song.wav                     # what each track became
python -m musiccopilot tracks song.wav --map "Track3=vocals"
```

The stems are renamed in place (the audio was always right, only the labels on
it were wrong) and only what the labels were load-bearing for is read again — a
change of instrument re-does the chords, notes and lyrics; swapping two
guitarists' numbers re-does only the form.

---

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
| `notes/*.json` | transcribed notes per stem, checked against the audio |
| `lyrics.json` | Whisper transcript of the vocal stem |

Every stage is cached separately, so nothing is ever computed twice. To redo one
of them, delete its file. The browser and the command line share this cache
completely — same songs, same ids, same results.

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
| checking | `clean.py` | CQT of the stem: drop notes with no energy of their own, and overtones of notes underneath them |
| lyrics | `lyrics.py` | Whisper on the isolated vocal stem |
| tabs | `tabs.py` | Viterbi over hand positions, each chord placed as a whole shape |
| score | `score.py` | note values, rests, ties, spelling against the key; VexFlow draws it |
| solos | `gemini.py` | Gemini structured JSON output → notes → tab + MIDI + audio |
| sound | `synth.py` | additive osc on a pitch curve (bends/slides/vibrato) + amp sim |

Scriptum itself is a thin FastAPI layer over exactly this: no musical decision
is made in the web code. A passage means the same thing in the browser as in
the terminal because both resolve it through the same function, and the tab
grid the browser draws is computed in Python and only positioned in JavaScript.

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

- Notes that are not there? Every transcription is now checked against the
  audio it describes before it is cached: a note whose own pitch band never
  rises out of the stem's noise floor is dropped, a stem that is nothing but
  separation residue (the piano in a song with no piano) gets no notes at all,
  and a "note" that is only the octave or twelfth of a chord already sounding
  underneath it is dropped as the overtone it is. Thresholds are `CLEAN` in
  `config.py`. If a real quiet part is going missing, loosen `note_floor_db`;
  if a stem you did play is being called residue, loosen `presence_db`. Either
  change needs the notes re-read — bump `clean.REVISION` or pass `--force`.
- A riff buried under chords? `--voice melody` (Scriptum sends the same
  `voice=` parameter) reads the line out of a stem that holds more than one
  part. It is display-only and changes nothing on disk.
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
- Tabs in the wrong position? adjust `_position_cost` / `_hand_cost` / `_LOW_BIAS`
  in `tabs.py`; `_LOW_BIAS` is what settles a tie towards the nut.
- Solo too tame? `--temperature 1.4`, or be far more specific in `--prompt`.
- `GEMINI_MODEL=gemini-2.5-flash` for cheaper/faster solo drafts. Run
  `python -m musiccopilot models` to see what your key can reach — if a Gemini 3
  id is listed, it is worth switching to for solo quality.

---

## Troubleshooting

**"no venv at .venv"** — you are in the wrong folder, or step 3 has not run.
`uv venv --python 3.11 && uv pip install -r requirements.txt` from the repo root.

**The page loads but every song page is blank, or a link does nothing** — the
client is stale or was never built. `cd web && npm run build`. (`./start.sh`
does this on its own; a browser tab left open across a rebuild reloads itself.)

**"Set GEMINI_API_KEY"** in the Solo or Clean up panes — the key is in `.env`
but was not handed to the process. Start it with `./start.sh`, or with
`uv run --env-file .env python -m scriptum`.

**`NoBackendError` / mp3 will not load** — ffmpeg is not on `PATH`. Install it
(above) and open a new terminal.

**No microphone devices in the live panes** — on Linux, `sudo apt install
libportaudio2`. On macOS, the terminal app needs the Microphone permission in
System Settings → Privacy & Security.

**A `.band` project cannot be read on macOS** — that is the system's file
protection, not Scriptum's. Give your terminal Full Disk Access, or copy the
project to `~/Downloads` in Finder and open the copy.

**Analysis is very slow the first time** — it is downloading the separation and
speech models. It only happens once.

**Any command fails with a one-line red error** — add `--debug` anywhere in the
command for the full traceback.

---

## The command line

Everything the web app does, minus the drawing. `uv run` picks up `.venv` and
`.env` without activating anything, on every platform:

```bash
uv run --env-file .env python -m musiccopilot <command>
```

If you would rather activate the environment (`source .venv/bin/activate`, or
`.venv\Scripts\activate` on Windows), it is just `python -m musiccopilot`, which
is how the examples below are written. Export the key yourself in that case —
`set -a; source .env; set +a` on macOS/Linux.

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

# one stem often holds a riff and a strummed chord at once. --voice melody
# reads the line out of it; --voice backing shows what is under it
python -m musiccopilot tab song.mp3 --part verse --stem guitar --voice melody

# tidy a transcribed passage up with Gemini before printing it (display only -
# it never overwrites the transcription, and it is capped to a passage)
python -m musiccopilot tab song.mp3 --part "guitar solo" --stem guitar --llm-clean

# play along: the passage plays while the tab scrolls under a live cursor.
# --minus-stem drops your instrument out of the mix so you play that part,
# --speed slows playback down without changing pitch, --count-in clicks you in
python -m musiccopilot tab song.mp3 --part "guitar solo" --stem guitar --follow
python -m musiccopilot tab song.mp3 --stem guitar --bars 17-24 \
    --follow --minus-stem --speed 0.75 --count-in 4

# separation gives one "guitar" file however many guitarists played. this is
# who is actually in it — usually settled during analyze, correctable here
python -m musiccopilot voices song.mp3
python -m musiccopilot voices song.mp3 --count 2   # insist on two of them
python -m musiccopilot voices song.mp3 --undo      # ...or on one

# which note transcriber this install can run, and re-read one song's notes
# with a different one (cheap: stems, chords and form are left alone)
python -m musiccopilot transcribers
python -m musiccopilot transcribe song.mp3 --backend crepe --stem guitar
python -m musiccopilot analyze song.mp3 --backend crepe

# cut every part out as its own wav (--stems for one per instrument)
python -m musiccopilot snippets song.mp3 --stems

# play into the mic: live notes, tab and chords as you play
python -m musiccopilot record --instrument guitar

# ask Gemini for a solo over the solo section, hear it over the real backing
# track (the song minus the guitar stem)
python -m musiccopilot solo song.mp3 --prompt "slow bluesy, lots of bends, build to a scream" --play
python -m musiccopilot solo song.mp3 --prompt "fast legato, dorian" --part bridge --over chords

# which Gemini models this key can actually reach
python -m musiccopilot models
```

Positions can be written three ways, so you can use whichever you have to hand:
`--start 62` (seconds), `--start 1:02` (mm:ss), `--start bar17`, `--bars 17-24`,
or skip them entirely with `--part chorus2`. `--debug` anywhere in the command
turns the one-line error message into a traceback.

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
- Transcription is not bit-reproducible: re-reading the same audio moves about
  a tenth of the notes by one 10ms frame. A change is only real if it moves
  more than that.
- The synth is a stylised approximation, not a sampled guitar. Load the exported
  `.mid` into a DAW with a real guitar VST if you want the good sound.
- Everything runs on CPU by default. A four-minute song is a few minutes of
  separation on a modern laptop, and rather longer on an old one.
