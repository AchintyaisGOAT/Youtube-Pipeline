# Media Pipeline (TTS → Render → Upload) — Context for AI Assistant

Paste this whole file into your AI coding assistant at the start of a session. It contains
full project context plus your specific scope. You do not need to read the other two
WORK_*.md files — this one is self-contained.

## 1. What this project is

Automated, near-$0 pipeline: a trending history/education topic → a ~7-minute long-form
YouTube video + 7 Shorts cut from it → one human review gate → YouTube upload. Runs on a
schedule (weekly topic discovery, daily "advance" tick), single Windows 11 machine, one GPU.

```
[them] discover → gate → rank → research → fact-check → script → segment → images
[YOU]  tts → align → assemble → shorts
[them] metadata
[YOU]  thumbnail → human review (CLI) → upload
[them] analytics
```

`DESIGN.md` in the repo root has the original, more elaborate design. **Ignore its §2 (stack)
and §8 — those describe a Postgres + job-queue + web-dashboard architecture we deliberately
simplified away — specifically: the review gate is a CLI script, not a FastAPI dashboard.**
Treat DESIGN.md §3 (API keys/inputs), §4 (install steps for FFmpeg/NVENC), and §6 (risks —
especially #12 FFmpeg fragility, #13 visual variety, #14 Whisper drift, #15 Shorts length,
#16 double-upload, #19 OAuth scopes, #37 Shorts uniqueness) as still-valid background directly
relevant to your work.

## 2. Simplified stack (only the parts relevant to you)

| Layer | Choice |
|---|---|
| DB | SQLite, one file, via SQLAlchemy (`app/db.py`, owned by Foundation) |
| TTS | Kokoro-onnx (local, GPU via ONNX, no PyTorch) |
| Alignment | faster-whisper `small.en` (local, CTranslate2) |
| Render | FFmpeg/FFprobe CLI directly — no wrapper library (no moviepy/ffmpeg-python) |
| Thumbnail | Pillow, one template |
| Upload | YouTube Data API v3 (`google-api-python-client`) |
| Review | **A CLI script, not a web dashboard** — list pending videos, open in default player, prompt approve/reject/edit in the terminal |
| No job queue | Your `run()` functions are plain sequential functions called by `run_pipeline.py` (Foundation-owned) |

## 3. Full data model

Implemented in `app/db.py` (Foundation-owned — do not edit it; if you need a new column, ask
Foundation). This is the contract you code against.

| Table | Key columns | Written by | Read by |
|---|---|---|---|
| `channel` | id, handle, config (JSON), config_version | Foundation | everyone |
| `candidate` | id, channel_id, source, title, summary, score, status | Content | Content |
| `video` | id, channel_id, candidate_id, status, title, script, research(json), video_metadata(json), duration_s, thumbnail_uri, error | Content (research→images columns) + **you** (duration_s, thumbnail_uri, status through publish) | everyone |
| `segment` | id, video_id, idx, text, image_query, image_asset_id, start_s, end_s, in_short_span | Content (text/image_query/image_asset_id/in_short_span) + **you** (start_s/end_s from alignment) | **you** read `in_short_span` + `text`, never the raw `[SHORT]` markup |
| `short` | id, video_id, idx, start_s, end_s, title, caption, status, youtube_id | **you** | **you** |
| `asset` | id, kind, source, source_id, license, rights_url, attribution, uri | Content (image fetch) | **you** (`assemble` reads `uri`) |
| `render` | id, video_id, short_id, kind, stage, status, output_uri, tool_versions | **you** | **you** |
| `upload` | id, target_type, target_id, client_token, status, youtube_id, visibility, published_at, quota_units | **you** | Content (analytics needs `youtube_id`) |
| `analytics_snapshot` | id, youtube_id, captured_at, views, watch_time_minutes, retention | Content | Content |
| `api_quota_ledger` | api, day, units_used, tokens_used | via `app.quota.check_and_increment()` (Foundation helper) — call it before every YouTube upload call | shared |

## 4. Status enum you drive (`app/status.py`, already implemented — reuse, don't redesign)

```
... (Content runs researching → ... → fetching_images) ...
synthesizing_voice → aligning → assembling → cutting_shorts → (hands to Content for generating_metadata)
generating_thumbnail → awaiting_review → approved | rejected → uploading → published | failed
```

## 5. Shared conventions (Foundation-defined, you must follow)

Every stage module lives at `app/pipeline/<stage>.py` and exposes:

```python
def run(session: Session, video_id: uuid.UUID) -> None:
    """Idempotent: return early if row.status isn't the status this stage expects.
    Do the work, set row.status to the next Status on success. Let exceptions propagate —
    never catch-and-swallow; the orchestrator handles failure + alerting."""
```

Rules:
- No `session.commit()` inside your stage functions — the orchestrator commits once per stage.
- File paths only via `app.storage.work_dir(video_id)` / `output_dir(video_id)` — never a
  hardcoded `data/...` string. Intermediate render stages (slideshow → +narration → +music →
  +subs → final) each go to their own `*.tmp` file, atomically renamed, and each gets an
  `ffprobe` sanity check before the next stage starts (DESIGN.md #12).
- Load Kokoro + faster-whisper **once** as module-level singletons at process start, not per
  video (DESIGN.md eff-measure).
- `encoder: auto` in channel config means: probe for `h264_nvenc` once, fall back to
  `libx264` if unavailable.
- Every YouTube API call goes through `app.quota.check_and_increment(session, "youtube",
  units)` — over-budget calls defer (raise `QuotaExceeded`; orchestrator retries later, not a
  hard failure).
- Never call FFmpeg or the YouTube API directly without going through your own thin wrapper
  functions — keeps retry/logging consistent.

## 6. Your scope (Media)

**Files you own** (nobody else touches these):
```
app/pipeline/tts.py        — Kokoro narrates the full video.script → audio file under
                              work_dir(video_id); sets video.duration_s (measured, not
                              estimated)
app/pipeline/align.py      — faster-whisper word-level timing against the audio + known
                              script text (pass as initial_prompt/hotwords, DESIGN.md #14);
                              fills segment.start_s/end_s for every segment; merges
                              contiguous in_short_span=True segments into `short` rows with
                              computed start_s/end_s
app/pipeline/assemble.py   — FFmpeg: slideshow from segment images (Ken Burns + some visual
                              variety per DESIGN.md #13) + narration + ducked music bed +
                              burned subtitles (pysubs2-generated ASS, karaoke style per
                              config) → final long-form file; writes a `render` row per stage
app/pipeline/shorts.py     — FFmpeg: for each `short` row, cut+crop to vertical 1080x1920,
                              own hook/caption per short (not a plain center-crop,
                              DESIGN.md #37); hard-gate against config's max_seconds, trim to
                              sentence boundary if over; writes `render` rows
app/pipeline/thumbnail.py  — Pillow template: subject cut-out + 3-4 word headline + brand
                              frame → video.thumbnail_uri
review.py                  — CLI script (repo root or scripts/): list videos with
                              status=awaiting_review, open long-form + shorts in default
                              player, print metadata for the reviewer to accept or edit
                              inline, confirm image licenses, then set status=approved or
                              rejected
app/pipeline/upload.py     — write an `upload` row with status=uploading + client_token
                              *before* calling the YouTube API (DESIGN.md #16 — dedup on
                              retry by searching for that token first); set the
                              "synthetic content" disclosure flag; upload video then each
                              short; api_visibility from config (private/unlisted/public)
tests/test_media_pipeline.py — ffprobe assertions on a synthetic short render; golden SRT
                              fixture for the subtitle generator
```

## 7. Definition of done

- `tts.py` produces a real audio file and a `duration_s` that matches `ffprobe`'s measurement
  within a small tolerance.
- `align.py` timings are checked against the mismatch-fallback ratio (DESIGN.md #14): if
  recognized-word count vs. script mismatch exceeds the configured threshold, fall back to
  proportional per-sentence timing instead of raw Whisper output.
- `assemble.py` produces a final file that passes an `ffprobe` check (correct resolution, fps,
  loudness in range) — verify manually with a 30-second test script before wiring to the full
  pipeline.
- `shorts.py` never produces a Short over `config.video.shorts.max_seconds`; each Short has a
  distinct caption/title, not a generic reused one.
- `review.py` runs end-to-end from the terminal: list → open video → accept edits → status
  change persisted to SQLite.
- `upload.py` is safe to run twice on the same video without creating a duplicate YouTube
  upload (test by simulating a crash after the API call but before the status update).

## 8. Handoff from Content

Before your first stage (`tts.py`) can run, Content must have left:
- `video.script` populated (with `[SHORT]` spans already resolved into `segment.in_short_span`
  — you never parse `[SHORT]` markup yourself, only read the boolean column)
- `segment` rows: `text`, `image_query`, `image_asset_id`, `in_short_span` all populated
- `asset` rows for every referenced `image_asset_id`, each with a real `uri` on disk
- `video.status == fetching_images` (done), which the orchestrator turns into
  `synthesizing_voice` — your entry point

You do not touch `app/pipeline/discover.py`, `gate.py`, `rank.py`, `research.py`,
`factcheck.py`, `script.py`, `segment.py`, `images.py`, `metadata.py`, or `analytics.py` —
those are Content's.
