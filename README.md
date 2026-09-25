# YouTube Pipeline

Automated, near-$0 pipeline: a trending history/education topic → a ~7-minute long-form video
+ 7 Shorts cut from it → one human review gate → YouTube. Fully scheduled.

> **Status:** Content's stages (discover → gate → rank → research → factcheck → script →
> segment → images) and Foundation (db/config/http/storage/notify/quota/orchestrator) are
> built and tested. Media's stages (tts → align → assemble → shorts → metadata's thumbnail
> handoff → upload → review) are not written yet — the pipeline currently runs end-to-end up
> through image fetching and then has nothing to advance it further.

```
weekly discovery + daily advance tick → trend rank → LLM research → fact-check
  → script (+[SHORT] spans) → segment plan → open-access image fetch → Kokoro TTS
  → Whisper word-timing → FFmpeg assemble (slideshow + subs + ducked music)
  → long-form + 7 Shorts → metadata + thumbnail → human review gate
  → YouTube upload → analytics pulled back (48 h, 7 d)
```

`DESIGN.md` has the original, more detailed design (risks, API specifics, background).
**Its §2 (stack) and §8 (build order) are superseded** — see `WORK_FOUNDATION.md` for the
stack actually in use below. `WORK_CONTENT.md` / `WORK_MEDIA.md` are the per-area scope docs.

## Locked stack (current)

SQLite (single file, `data/pipeline.db`, no server) · SQLAlchemy 2.0 · no migrations
(`Base.metadata.create_all()`) · no job queue — one sequential `run_pipeline.py` script ·
Gemini 2.5 Flash (`google-genai`) + Search grounding · Wikimedia Pageviews (discovery) +
Wikimedia Commons / Library of Congress (images) · Kokoro ONNX + faster-whisper (local, no
PyTorch) · FFmpeg (NVENC) for assembly · YouTube Data API v3 + Analytics API v2 · Windows Task
Scheduler for cron, no persistent worker.

## Quick start

```powershell
winget install --id astral-sh.uv --exact
winget install --id Git.Git --exact
winget install --id Python.Python.3.11 --exact
winget install --id Gyan.FFmpeg --exact

uv sync                               # creates .venv from pyproject.toml
copy .env.example .env                # fill in as you get each credential — see below
copy config.example.yaml config.yaml  # edit channel identity/behavior — see below

uv run python scripts/load_config.py config.yaml   # validates config.yaml, writes the channel
                                                      # row (creates data/pipeline.db), dumps
                                                      # config.schema.json
uv run python scripts/doctor.py                     # green/red check: db, .env keys, ffmpeg, disk
```

Credentials checklist (`DESIGN.md §3.1` has the full detail):

1. A **channel** Google account (2FA on) → create the YouTube channel, phone-verify, note
   the Channel ID (Studio → Settings → Channel → Advanced) → `YOUTUBE_CHANNEL_ID`.
2. A **second, separate** Google account (2FA on) for Cloud + Gemini (deliberately not the
   channel account — DESIGN.md #8).
3. On the second account: aistudio.google.com → `GEMINI_API_KEY`.
4. On the second account: Cloud Console → new project → enable **YouTube Data API v3** +
   **YouTube Analytics API** → OAuth consent screen (External, Testing, add the channel
   account as a test user) → Credentials → OAuth client ID → Desktop app → download
   `client_secret.json` → copy `client_id`/`client_secret` into `.env`.
5. `uv run python scripts/get_youtube_token.py client_secret.json` → browser login **as the
   channel account** → approve → paste the printed refresh token into `.env` as
   `YOUTUBE_REFRESH_TOKEN`.
6. Pick a real contact string for `WIKIMEDIA_CONTACT` (Wikimedia 403s without one).
7. Optional: `ALERT_URL` — an ntfy.sh topic URL for failure notifications.

Once `doctor.py` is all green, `uv run python run_pipeline.py` runs one pass (discovery, then
advances every candidate/video one stage). It currently stalls at `synthesizing_voice` until
Media's stages exist — that's expected, not a bug.

## Layout

```
app/                 config, db, http, storage, notify, quota, llm (Gemini wrapper)
app/pipeline/        one module per stage — discover, gate, rank, research, factcheck,
                     script, segment, images done; tts/align/assemble/shorts/thumbnail/
                     upload/review = Media's, not yet written
prompts/             Gemini prompt templates (research, factcheck, script, metadata,
                     sensitivity_check)
scripts/             load_config, doctor, get_youtube_token
tests/                unit + golden-file tests
assets/              curated local media (git-ignored) — music/sfx/fonts/branding/thumbnails
DESIGN.md            original detailed design (background; §2/§8 superseded)
WORK_FOUNDATION.md   Foundation's scope + the current stack definition
WORK_CONTENT.md      Content's scope (discover → images, done)
WORK_MEDIA.md        Media's scope (tts → upload, not started)
config.yaml          single source of truth (git-ignored; see config.example.yaml)
pyproject.toml       dependencies (uv)
run_pipeline.py      sequential orchestrator
```

Private project — no license granted.
