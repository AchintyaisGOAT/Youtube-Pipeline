# Tech Stack — LOCKED

This is the frozen technology decision record. Every tool here has a job nothing else in the
stack already does. Rejected alternatives and the reasoning are recorded at the bottom so the
decisions don't get re-opened later.

**Target:** Windows 11 · 1× NVIDIA GPU (8–12 GB) · Python 3.11 · $0 running cost ·
English (US) · history/education explainers · auto trend discovery · fully scheduled ·
~7 min long-form + 7 Shorts cut from it · ~6 long-form + 42 Shorts per month.

**Scale headroom designed in:** ~10× this volume on the same single machine with no
re-architecture; multi-channel and cloud-storage migration are non-breaking changes.

---

## What changed from the first draft (and why)

| Removed | Replaced by | Reason |
|---|---|---|
| Redis + **Memurai** | Postgres `LISTEN/NOTIFY` (via procrastinate) + `aiolimiter` | One less server to install/run/back up. Memurai's free edition is **licensed for development only** — a landmine for a channel meant to earn later. Nothing here needs a second datastore at this scale. Re-adding Redis later (if you ever go multi-machine) is additive, not a rewrite. |
| **RQ** (task queue) | **procrastinate** (Postgres-backed queue) | RQ is Redis-only and has no native scheduling or task-dependency support. procrastinate runs on the Postgres you already have, with retries, backoff, cron, and task locks built in. |
| **APScheduler** | procrastinate periodic tasks | The scheduler is now just a decorator on a task; no separate process. |
| **PyTorch (CUDA)** | **onnxruntime-gpu** + **CTranslate2** | Torch was the heaviest (~2.5 GB) and most fragile dependency (driver/CUDA/cuDNN version-matching). The only two local models we run (Kokoro, Whisper) have first-class ONNX / CTranslate2 builds. CPU fallback is viable at this volume. |
| Whisper **large-v3** | faster-whisper **`small.en`** (configurable up) | We run it on *clean synthetic speech whose exact text we already know* — this is near-forced-alignment, not hard transcription. A small model gives excellent word timings for a fraction of the VRAM and time. |
| **ffmpeg-python** | thin internal subprocess command-builder | ffmpeg-python is effectively unmaintained. The FFmpeg CLI has been stable for 20 years — calling it directly is the most future-proof option. |
| **Openverse** image API | (dropped) | Aggregates CC-BY/Flickr content with attribution-tracking overhead and licensing ambiguity. Wikimedia + LoC + Smithsonian + Met are all clean public-domain / CC0 and cover history deeply. Keeps the channel monetization-safe. |
| **python-dotenv** | pydantic-settings native `.env` loading | Redundant. |

| Added | Job |
|---|---|
| **uv** + committed `uv.lock` | Reproducible, fully-pinned installs. This is what actually makes "nothing changes later" true. |
| **fsspec** | Storage abstraction. Media paths are `file://` today; becoming `s3://` / Backblaze `b2://` later is a config change, no code change. |
| **aiolimiter** | In-process token-bucket rate limiting for Gemini / YouTube (replaces Redis counters). |
| **NSSM** | Runs the worker and dashboard as auto-restarting Windows services (survives reboot, no login needed). |

Net result: **3 things run on the box** (PostgreSQL service + worker service + dashboard
service) instead of 5, one fewer server product installed, and no PyTorch.

---

## The locked stack, by layer

### L0 — Machine & OS
| Component | Job |
|---|---|
| Windows 11 | Host. |
| **NVIDIA GPU driver** (Studio) | GPU acceleration for Kokoro (ONNX) and faster-whisper (CTranslate2). Driver only — no CUDA Toolkit install; runtime libs come as pip wheels. |
| **FFmpeg build with NVENC** | Hardware H.264 encoding (`h264_nvenc`) keeps renders fast. |

### L1 — Language & dev environment
| Component | Job |
|---|---|
| **Python 3.11** | The one language. Pinned — some AI libs lag on 3.12+. |
| **uv** | Creates the venv, resolves and installs deps, maintains `uv.lock`. |
| **`uv.lock`** (committed) | Every transitive dependency pinned by hash. Rebuilds are byte-identical. |
| **Git + GitHub** | Code version control + off-machine backup. |
| **ruff** | Formatter + linter (one tool). |
| **pytest** (+ pytest-asyncio) | Tests for timing math, Shorts cut points, quota logic. |

### L2 — Configuration & secrets
| Component | Job |
|---|---|
| **`config.yaml`** (per-channel rows mirror it in DB) | All tunable behaviour: lengths, pacing, voice, subtitle style, schedule slots, prompt file paths. Has a `config_version` field. |
| **pydantic-settings** | Loads `config.yaml` + `.env`, validates on startup — bad/missing values fail loudly and immediately. |
| **`.env`** | Secrets + machine paths only. Git-ignored. |
| **`.gitignore`** | Excludes `.env`, `client_secret.json`, `token.json`, `assets/`, `work/`, `output/`. |

### L3 — Data & job queue
| Component | Job |
|---|---|
| **PostgreSQL 16** | Single source of truth. State of every video/Short, scripts, segments, asset records, renders, uploads, analytics, and the API-quota ledger. |
| **SQLAlchemy 2.0** | ORM — application code and your manual maintenance scripts. |
| **Alembic** | Versioned schema migrations. |
| **psycopg 3** (`psycopg[binary]`) | Postgres driver. |
| **procrastinate** | Postgres-backed job queue: one job per pipeline stage, with retries + exponential backoff, cron-style periodic tasks (the daily discovery trigger), and per-key task locks (e.g. serialise uploads). Uses `LISTEN/NOTIFY` for low-latency pickup. |
| **pgAdmin 4** | GUI for eyeballing / hand-editing rows during review. (Installs with Postgres.) |

### L4 — Orchestration
| Component | Job |
|---|---|
| **Pipeline module** (project code) | Thin layer: each stage-task, on success, enqueues the next stage. The DAG edges live here; per-video state lives in `video.status`. No separate orchestrator process. |
| **tenacity** | Fine-grained retry/backoff for individual flaky calls *inside* a task (e.g. one image download), so a whole 7-min render isn't retried over one blip. |
| **aiolimiter** | Token-bucket limiters for Gemini RPM/day and YouTube units. |
| **loguru** | Logging to rotating files + console. |

### L5 — Cloud APIs
| Component | Job |
|---|---|
| **Gemini 2.5 Flash** (`google-genai`) | Trend ranking, research, 7-min script (with `[SHORT]` spans + `[IMAGE: query]` cues), metadata (title A/B, description, tags, chapters). |
| **Gemini + Google Search grounding** | Fact-check stage — verifies each claim against live search before it reaches the script. |
| **YouTube Data API v3** (`google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2`) | **Primary** trend signal (`chart=mostPopular`, category *Education*, region US) + competition check; resumable upload of long-form and Shorts; metadata / thumbnail / visibility / schedule. |
| **YouTube Analytics API v2** | Post-publish pull of views, watch time, average view duration, retention at 48 h / 7 d → back into Postgres to feed the trend-ranking prompt. |
| **pytrends** | *Optional, non-critical* enrichment (rising queries). Wrapped so any failure is logged and skipped — the pipeline never depends on it. |

### L6 — Media source APIs (public-domain / CC0 only)
| Component | Job |
|---|---|
| **Wikimedia Commons API** | Primary imagery — PD photos, portraits, maps, artwork. No key. |
| **Library of Congress JSON API** | Historical photos, posters, documents. No key. |
| **Smithsonian Open Access API** | ~4M CC0 images. Free `api.data.gov` key. |
| **Met Museum Collection API** | PD artworks, high-res. No key. |
| **httpx** | The one HTTP client for L5 + L6 (sync + async). |

Every fetched image is stored with `source`, `source_id`, `license`, and `attribution` in the
`asset` table — even for PD, for on-screen credit and description lines.

### L7 — Local AI models (GPU, free, offline)
| Component | Job |
|---|---|
| **kokoro-onnx** (+ **soundfile**) | Text-to-speech. Each script segment → narration WAV. 1–2 British documentary voices. |
| **faster-whisper** (`small.en` default) | Runs on the narration to produce **word-level timestamps** for subtitles and image cut points. |
| **onnxruntime-gpu** | Runtime for kokoro-onnx. |
| **CTranslate2** | Runtime for faster-whisper (installed as its dependency). |
| **CUDA 12 runtime wheels** (`nvidia-cudnn-cu12`, …) | Pulled in by the two above — no system CUDA install. |
| **huggingface_hub** | Downloads + caches model weights on first run. |

If GPU ONNX/cuDNN setup ever misbehaves, both models fall back to CPU — at ~2 videos/day
that's a few extra minutes per video, not a blocker.

### L8 — Audio & video assembly (local, free)
| Component | Job |
|---|---|
| **FFmpeg + FFprobe** (with **libass**, **NVENC**) | The whole render: image slideshow, Ken-Burns pan/zoom, transitions, mix narration + music + SFX, loudness normalise to −14 LUFS, burn styled subtitles, export 1920×1080 long-form, then crop to 1080×1920 and slice the 7 Shorts at scripted timestamps. |
| **Internal `ffmpeg` command-builder** (project code, subprocess) | Constructs and runs FFmpeg commands from Python. No third-party wrapper. |
| **Pillow** | Resize/crop each source image to 1920×1080; title cards. |
| **pysubs2** | Build ASS/SRT (line-wrapping, 2-line max, per-word highlight) from the Whisper timings. |
| `assets/music/`, `assets/sfx/`, `assets/fonts/` | Curated CC0/PD/OFL local libraries, indexed in Postgres by mood/length. |

### L9 — Review & publish
| Component | Job |
|---|---|
| **FastAPI** + **Uvicorn** + **Jinja2** (+ python-multipart) | One local web app = review queue (watch long-form + 7 Shorts, approve / reject / re-render with a note), metadata editor, pipeline status board, and config editor. Runs only on your machine. |
| **YouTube Data API v3** | On approval: upload + apply metadata + schedule; store returned video IDs and quota cost. |

### L10 — Running unattended
| Component | Job |
|---|---|
| **procrastinate worker** (1 process) | Executes stage jobs and fires the periodic discovery task. |
| **Uvicorn dashboard** (1 process) | The L9 app. |
| **NSSM** | Registers both as Windows services: auto-start at boot, auto-restart on crash, no login required. |
| **fsspec** | All media I/O goes through it. Local disk today; swap the base URL for S3 / Backblaze B2 later with zero code change. |

---

## Data model (locked shape)

Multi-channel-ready from day one — `channel_id` FK on everything even though there's one channel.

| Table | Purpose |
|---|---|
| `channel` | One row per channel. Holds its config snapshot + OAuth identity. FK target everywhere below. |
| `candidate` | Topic ideas from discovery, with fit score + rationale. |
| `video` | The long-form unit: `channel_id`, `candidate_id`, `status`, script, metadata JSON, timings, paths. |
| `short` | `video_id`, index, `start_s`, `end_s`, `status`, `youtube_id`. |
| `segment` | `video_id`, index, narration text, `image_query`, `image_asset_id`, `start_s`, `end_s`. |
| `asset` | `kind` (image/music/sfx), `source`, `source_id`, `license`, `attribution`, `uri`, metadata. |
| `render` | Per long-form / per Short render attempt: `status`, `output_uri`, log, duration. |
| `upload` | `target_type`, `target_id`, `youtube_id`, `visibility`, `published_at`, `quota_units`. |
| `analytics_snapshot` | `youtube_id`, `captured_at`, views, watch time, AVD, retention JSON. |
| `api_quota_ledger` | `(api, day) → units_used`. The uploader checks this before every call and defers when the daily budget is spent. |
| `procrastinate_*` | Queue tables (managed by procrastinate's own migrations). |

Design rules that keep it scalable:
- **Every stage is idempotent** — write to a temp path, atomic-rename, and check "already done"
  in the DB before working. Re-running any job is always safe.
- **Workers are stateless** — all state is in Postgres, so adding a second worker (or moving to
  a bigger box) is lift-and-shift.
- **No secrets or large blobs in the DB** — blobs go through fsspec storage; the DB holds URIs.

---

## Scalability analysis (per concern)

| Concern | Now | At ~10× (60 long-form + 420 Shorts/mo) | Ceiling / when the stack would change |
|---|---|---|---|
| **Compute throughput** | ~1.6 videos/day. One 7-min render ≈ 4–8 min with NVENC. | ~5–6 renders/day + TTS/Whisper — still hours of idle GPU/day. Bump procrastinate concurrency, optionally a 2nd worker on the same box. | Only past ~30–40 long-form/day would you need a second machine — and that's just another stateless worker pointed at the same Postgres. |
| **Storage** | Local disk via fsspec. Masters pruned after N days (config). | Point fsspec at Backblaze B2 / S3 — **config change, no code change**. | Never forces a rewrite. |
| **YouTube API quota** | 10,000 units/day ≈ 6 uploads/day. `api_quota_ledger` enforces; over-budget uploads defer to next day. Scheduler spreads the 7 Shorts across days. | Submit the free quota-increase request. Ledger logic unchanged. | Hard external cap; handled by deferral + increase, not by tech choice. |
| **Gemini limits** | Free tier RPM/day; aiolimiter + task retries absorb it. | May need the paid tier (still cents per script). Grounding cost cliff after the free daily allotment — batch fact-checks. | Swap the model string; SDK unchanged. |
| **Multi-channel** | One `channel` row. | Insert more `channel` rows + their assets/prompts. Discovery, prompts, schedule, OAuth are all per-channel. | No migration — the FK is already everywhere. |
| **Single-machine failure** | Nightly `pg_dump` + media backup. | Same, plus fsspec already in cloud → only Postgres is local. | Move Postgres to a managed instance (connection string change) if uptime ever matters. |
| **Human review** | ~6 sessions/month. | Becomes real work; dashboard supports batch approve. Unattended build continues regardless. | This is an operations limit, not a stack limit. |

---

## Rejected alternatives (do not re-open)

| Considered | Rejected because |
|---|---|
| **Celery / Dramatiq** | Need a dedicated broker + more ops surface; workflow chaining is clumsy. procrastinate reuses Postgres and covers retries + cron + locks. |
| **Prefect / Dagster / Temporal** | Each wants its own server/service and a heavier mental model. Our pipeline is near-linear with one fan-out; per-video state in `video.status` + a dashboard query gives the same visibility. |
| **Redis / Memurai** | Extra service; Memurai's free edition is dev-licensed. Postgres `LISTEN/NOTIFY` + `aiolimiter` cover dispatch latency and rate-limiting. Re-addable later, non-disruptively, only if genuinely multi-machine. |
| **PyTorch stack** | Heaviest, most breakage-prone dependency (driver/CUDA/cuDNN matching). onnxruntime-gpu + CTranslate2 accelerate the only two models we run; CPU fallback is fine at this volume. |
| **SQLite** | No real concurrent writers; weak migration/maintenance story. Postgres is free and scales past anything this project will do. |
| **MongoDB / a cloud DB** | Data is relational; $0 target; local Postgres is plenty. |
| **MoviePy / ffmpeg-python** | Wrappers that trail FFmpeg releases and add failure modes. The FFmpeg CLI is a stable contract — call it directly. |
| **Openverse / Flickr / stock sites** | CC-BY attribution tracking + licensing ambiguity; risk to monetization. PD/CC0-only sources (Wikimedia, LoC, Smithsonian, Met) cover history well. |
| **Local LLM (Llama/Qwen via Ollama/vLLM)** | You chose Gemini. A local LLM would also contend with TTS/Whisper for VRAM. Revisit only if Gemini cost/quota ever forces it — it's an additive change. |
| **AI image generation (local SDXL / paid API)** | You chose archives-only. Adding it later is a new stage feeding the same `asset` table — not a stack change. |
| **Whisper large-v3 / WhisperX** | Overkill on clean synthetic speech; WhisperX drags PyTorch back in. `faster-whisper small.en` is enough, upgradable via config. |

---

## Full dependency list (managed by `uv`, pinned in `uv.lock`)

**Runtime**
```
pydantic  pydantic-settings  pyyaml
sqlalchemy  alembic  psycopg[binary]
procrastinate
httpx  tenacity  aiolimiter  loguru
fsspec
google-genai
google-api-python-client  google-auth-oauthlib  google-auth-httplib2
pytrends
kokoro-onnx  soundfile  onnxruntime-gpu
faster-whisper
huggingface_hub
pillow  pysubs2
fastapi  uvicorn[standard]  jinja2  python-multipart
```
**Dev**
```
ruff  pytest  pytest-asyncio
```
**External (not pip)**
```
uv                 – installer / env / lockfile
Git for Windows    – VCS
Python 3.11        – runtime
FFmpeg (Gyan build, libass + NVENC)  – media
PostgreSQL 16 (+ pgAdmin 4)          – database
NVIDIA Studio driver                 – GPU
NSSM               – run worker + dashboard as Windows services
```
