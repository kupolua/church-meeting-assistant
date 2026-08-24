# Meetings directory convention

Each pastoral council meeting gets its own subdirectory under this folder
(`data/tenants/<slug>/meetings/` — one subtree per church)
named with the meeting date in ISO format: `YYYY-MM-DD`.

```
data/tenants/<slug>/meetings/
├── README.md             (this file — overall convention)
├── 2026-06-08/           (example: one meeting per folder)
│   ├── README.md         (meeting-specific: what's here, how to reproduce)
│   ├── audio.m4a         (source recording)
│   ├── transcript.json   (Whisper output)
│   ├── diarization.rttm  (pyannote output, + any hand-added speakers)
│   ├── diarization.pyannote.rttm  (pyannote's own output, kept once — only
│   │                               present once a speaker was added by hand)
│   ├── embeddings.pkl    (cached pyannote speaker embeddings)
│   ├── speakers.json     (SPEAKER_XX → canonical name map + _meta)
│   ├── annotated.md      (transcript + speaker labels merged)
│   ├── chunks/           (per-chunk Gemma analysis output)
│   ├── chunked.md        (merged chunks)
│   └── polished.md       (final protocol, ready to share)
└── 2026-06-15/           (future meetings — same structure)
```

## Why per-meeting folders

Previously all artifacts lived flat in `data/` with prefixes like
`audio1512988623_*.m4a/.rttm/.json`. Two problems:

- Hard to see which files belong together
- Filename like `audio1512988623` carries no date — required mental lookup

Per-folder structure groups everything for one meeting and uses the
meeting date as the natural identifier.

## Date format

Always `YYYY-MM-DD` (ISO 8601, sorts chronologically by ls/find).
Examples: `2026-06-08`, `2026-02-23`.

If two meetings happen on the same day (rare), suffix with `-N`:
`2026-06-08-1`, `2026-06-08-2`.

## File naming inside a meeting folder

Inside `<meetings>/YYYY-MM-DD/` files use **standard names** (no date
prefix — the folder already encodes the date):

| File             | Producer                  | Notes                              |
| ---------------- | ------------------------- | ---------------------------------- |
| `audio.m4a`      | (you, the operator)       | Or `.wav`, `.mp3` — any format     |
| `transcript.json`| `transcribe.py`           | Whisper output                     |
| `diarization.rttm` | `match_speakers.py`     | pyannote turns; rebuilt when a speaker is added by hand |
| `diarization.pyannote.rttm` | `manual_speakers.py` | pyannote's untouched original; every rebuild starts here |
| `embeddings.pkl` | `match_speakers.py`       | Cached speaker embeddings (256-d)  |
| `speakers.json`  | `match_speakers.py`       | Auto-generated, may need review; `_meta.manual_speakers` holds hand-added ones |
| `annotated.md`   | `merge_transcript.py`     | Whisper × diarization merged       |
| `chunks/`        | `chunked_analyze.py`      | Per-chunk Gemma output             |
| `chunked.md`     | `chunked_analyze.py`      | Merged chunks                      |
| `polished.md`    | `polish_protocol.py`      | Final protocol                     |

## Running the pipeline on a new meeting

Normally you'd use the wrapper, which resolves these paths for you and takes
the church as an argument:

```bash
uv run python -m church_assistant.new_meeting \
    --audio /path/to/recording.m4a --date 2026-06-15 --tenant-slug default
```

The manual sequence below is the same steps by hand:

```bash
# Which church, and where its artifacts live. The wrapper and the web app
# resolve these through shared/tenant_paths.py; by hand you set them once.
TENANT=data/tenants/default
MEETINGS=$TENANT/meetings

# Set the date once for convenience
MEETING=2026-06-15
mkdir -p $MEETINGS/$MEETING

# 1. Drop the audio in place (any name, but conventionally audio.m4a)
cp /path/to/your_recording.m4a $MEETINGS/$MEETING/audio.m4a

# 2. Speaker diarization + matching (long, ~realtime on M1 CPU)
uv run python -m church_assistant.match_speakers \
    --audio $MEETINGS/$MEETING/audio.m4a \
    --output $MEETINGS/$MEETING/speakers.json \
    --rttm $MEETINGS/$MEETING/diarization.rttm

# (the embeddings.pkl cache is named after the audio file by match_speakers,
#  so it lands at $MEETINGS/$MEETING/audio_embeddings.pkl)

# 3. Whisper transcription (can run in parallel with step 2 in another shell)
uv run python -m church_assistant.transcribe $MEETINGS/$MEETING/audio.m4a
# Whisper writes $MEETINGS/$MEETING/audio_transcript.json by default
# (also derived from the audio filename)

# 4. Review speakers.json — clean up [REVIEW] tags, name any "no match" speakers
sed -i '' 's/ \[REVIEW\]//g' $MEETINGS/$MEETING/speakers.json
# Manually edit the file to rename SPEAKER_XX → person for any "no match" lines

# 5. Merge transcript with diarization
uv run python -m church_assistant.merge_transcript \
    --transcript $MEETINGS/$MEETING/audio_transcript.json \
    --rttm $MEETINGS/$MEETING/diarization.rttm \
    --speakers $MEETINGS/$MEETING/speakers.json \
    --output $MEETINGS/$MEETING/annotated.md

# 6. Chunked analysis with Gemma (~25-30 min for 2h audio, ~10 min for 1h)
uv run python -m church_assistant.chunked_analyze \
    --transcript $MEETINGS/$MEETING/annotated.md \
    --output-dir $MEETINGS/$MEETING/chunks \
    --final-output $MEETINGS/$MEETING/chunked.md

# 7. Final polish
uv run python -m church_assistant.polish_protocol \
    --chunks-dir $MEETINGS/$MEETING/chunks \
    --output $MEETINGS/$MEETING/polished.md \
    --rttm $MEETINGS/$MEETING/diarization.rttm \
    --speakers-map $MEETINGS/$MEETING/speakers.json \
    --date 15/06/2026

# 8. Read polished.md, decide if ready to share
```

## Naming quirks (legacy)

The current `transcribe.py` and `match_speakers.py` derive output filenames
from the audio stem:

- `<meetings>/2026-06-15/audio.m4a` → `audio_transcript.json`, `audio_embeddings.pkl`

That's why the convention table lists `transcript.json` (clean name) but the
actual file may be `audio_transcript.json` until those scripts are updated to
use the parent folder's natural names.

**TODO (future work):** update transcribe.py and match_speakers.py to write
clean names (`transcript.json`, `embeddings.pkl`) when output paths already
specify a per-meeting folder. For now, just live with the prefix.

## Voice profiles (global)

Voice profiles stay in `$TENANT/voice_profiles/` (not per-meeting). They are
the team's persistent voice signatures, shared across meetings.

```
$TENANT/voice_profiles/
├── Богдан Терещенко.npy
├── Вячеслав Коновалов.npy
├── Володимир Вальченко.npy
├── ...
```

When a new participant appears at a meeting, use `add_voice_profile.py`
to extract their embedding from that meeting's embeddings.pkl and save
it to the global profiles directory.

## Backup of legacy flat files

Old flat-layout files in `data/` (e.g. `audio1512988623.m4a`,
`audio1438994435_polished.md`) are kept as backup for 1-2 weeks while
the new structure is validated. They can be deleted once everyone is
confident the new convention works.
