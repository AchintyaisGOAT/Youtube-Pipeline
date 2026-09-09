# YouTube Pipeline — Design & Setup

Single reference for the whole project: what it is, the frozen stack, every input, install
steps, and known risks. Update this file rather than spawning new docs.

- [1. Overview](#1-overview)
- [2. Locked stack](#2-locked-stack)
- [3. Inputs](#3-inputs)
- [4. Install (Windows 11, native)](#4-install-windows-11-native)
- [5. Data model](#5-data-model)
- [6. Risks & improvements](#6-risks--improvements)
- [7. Scalability](#7-scalability)
- [8. Build order & conventions](#8-build-order--conventions)

---

## 1. Overview

Automated, near-$0 pipeline: a trending history/education topic → a ~7-minute long-form video
+ 7 Shorts cut from it → one human review gate → YouTube. Fully scheduled.

```
weekly discovery + daily advance tick → trend rank → LLM research → fact-check
  → script (+[SHORT] spans) → segment plan → open-access image fetch → Kokoro TTS
  → Whisper word-timing → FFmpeg assemble (slideshow + subs + ducked music)
  → long-form + 7 Shorts → metadata + thumbnail → human review gate
  → YouTube upload → analytics pulled back (48 h, 7 d)
```

**Fixed choices:** Windows 11 · 1× NVIDIA GPU 8–12 GB · Python 3.11 · $0 running cost ·
English (US) · auto trend discovery · fully scheduled · ~6 long-form + 42 Shorts / month ·
native installs (no Docker).

---

## 2. Locked stack

Nothing here does a job another component already covers. Rejected alternatives at the end of
this section — don't re-open them.

### 2.1 By layer

| Layer | Components | Job |
|---|---|---|
| **OS / machine** | Windows 11, NVIDIA Studio driver, FFmpeg build w/ NVENC | Host + GPU acceleration + hardware H.264 encode. No CUDA Toolkit — runtime libs come as pip wheels. |
| **Language / dev** | Python 3.11, **uv** (+ committed `uv.lock`), Git, ruff, pytest | One language; fully-pinned reproducible installs; format/lint; tests. |
| **Config / secrets** | `config.yaml` (source of truth), pydantic-settings, `.env`, `.gitignore` | Validated-on-startup config; secrets and paths only in `.env`. |
| **Data + queue** | **PostgreSQL 16**, SQLAlchemy 2.0, Alembic, psycopg 3, **procrastinate**, pgAdmin 4 | Single source of truth + job queue on the same Postgres (retries, backoff, cron, task locks via `LISTEN/NOTIFY`). No Redis. |
| **Orchestration** | pipeline module (project code), tenacity, aiolimiter, loguru | Each stage-task enqueues the next; in-task retry for flaky calls; rate limiting; logging. |
| **Cloud APIs** | Gemini 2.5 Flash (`google-genai`) + Search grounding; YouTube Data API v3; YouTube Analytics API v2; pytrends (best-effort) | Research/script/fact-check/metadata; upload + trend signal; post-publish analytics. |
| **Image APIs** | Wikimedia Commons, Library of Congress, Smithsonian (free key), Met; `httpx` + `hishel` cache | Public-domain / CC0 imagery only. |
| **Local AI** | **kokoro-onnx[gpu]** + soundfile; **faster-whisper** `small.en`; CTranslate2; huggingface-hub | TTS narration; word-level timestamps for subs/cuts. GPU via ONNX/CT2 — **no PyTorch**. CPU fallback fine at this volume. *Install note:* faster-whisper pulls CPU `onnxruntime` for its VAD; after `uv sync` confirm `onnxruntime.get_available_providers()` lists `CUDAExecutionProvider`, else uninstall the bare `onnxruntime`. |
| **Media assembly** | FFmpeg + FFprobe (libass, NVENC); internal subprocess command-builder; Pillow; pysubs2 | Slideshow + Ken Burns + transitions + audio mix + loudnorm + burn subs + export + Shorts crop/cut. No third-party FFmpeg wrapper. |
| **Review + publish** | FastAPI + Uvicorn + Jinja2; YouTube Data API v3 | Local dashboard: review queue, metadata editor, status board. Then upload. |
| **Unattended** | procrastinate worker + Uvicorn dashboard, run as **NSSM** Windows services; **fsspec** storage abstraction | Auto-restart on boot/crash; media paths are `file://` now, `s3://`/`b2://` later with no code change. |

### 2.2 Full dependency list (`pyproject.toml`, pinned by `uv.lock`)

Runtime: `pydantic pydantic-settings pyyaml · sqlalchemy alembic psycopg[binary]
procrastinate · httpx hishel tenacity aiolimiter loguru apprise · fsspec · google-genai
google-api-python-client google-auth-oauthlib google-auth-httplib2 · pytrends · kokoro-onnx
soundfile onnxruntime-gpu faster-whisper huggingface_hub · pillow pysubs2 · fastapi
uvicorn[standard] jinja2 python-multipart`
Dev: `ruff pytest pytest-asyncio`
External (not pip): `uv · Git · Python 3.11 · FFmpeg (Gyan build) · PostgreSQL 16 (+pgAdmin) ·
NVIDIA Studio driver · NSSM`

### 2.3 Rejected — do not re-open

| Considered | Rejected because |
|---|---|
| Celery / Dramatiq | Need a dedicated broker + ops surface; procrastinate reuses Postgres. |
| Prefect / Dagster / Temporal | Each wants its own server/service; our pipeline is near-linear, state lives in `video.status`. |
| Redis / Memurai | Extra service; Memurai free edition is dev-licensed. Postgres `LISTEN/NOTIFY` + aiolimiter cover it. Re-addable later non-disruptively. |
| PyTorch stack | Heaviest, most breakage-prone dep (driver/CUDA/cuDNN matching). ONNX + CT2 accelerate the two models we run. |
| SQLite | No real concurrent writers; weak migration story. Postgres is free and scales past anything here. |
| MoviePy / ffmpeg-python | Wrappers that trail FFmpeg releases. The CLI is a stable contract — call it directly. |
| Openverse / Flickr / stock | CC-BY attribution tracking + licensing ambiguity; risk to monetization. PD/CC0-only sources cover history. |
| Local LLM (Llama/Qwen) | You chose Gemini; a local LLM contends for VRAM with TTS/Whisper. Additive later if cost forces it. |
| AI image generation | You chose archives-only. Later it's a new stage feeding the same `asset` table, not a stack change. |
| Whisper large-v3 / WhisperX | Overkill on clean synthetic speech; WhisperX drags PyTorch back. `small.en` + config upgrade path. |

---

## 3. Inputs

### 3.1 Cloud accounts & keys (all free) — create in this order

| # | Input (env var) | Where | Notes / limits |
|---|---|---|---|
| 1 | **Channel Google account** | accounts.google.com | Dedicated, 2FA on. |
| 2 | **YouTube channel** | youtube.com → Create channel | Phone-verify (raises upload limits). Channel ID: Studio → Settings → Channel → Advanced → `YOUTUBE_CHANNEL_ID`. |
| 3 | **Second Google account** (Cloud + Gemini) | accounts.google.com | Deliberately separate from the channel — risk #8. |
| 4 | **Google Cloud project** | console.cloud.google.com (acct #3) | Name `yt-pipeline`. No billing needed. |
| 5 | `GEMINI_API_KEY` | aistudio.google.com (acct #3) | Gemini 2.5 Flash free tier is roughly low-tens RPM / low-hundreds req/day — **verify current numbers in AI Studio**, they change. Grounding has a smaller separate daily free cap. |
| 6 | `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | Cloud Console → Credentials → OAuth client ID → **Desktop app** → `client_secret.json` | Enable **YouTube Data API v3** + **YouTube Analytics API** first. |
| 7 | `YOUTUBE_REFRESH_TOKEN` | `uv run python scripts/get_youtube_token.py` (browser login) | Scopes: `youtube.upload` (insert) **+ `youtube.force-ssl`** (needed to set thumbnail, add to playlist, update metadata) **+ `yt-analytics.readonly`**. `youtube.upload` alone cannot do post-upload edits. |
| 8 | `SMITHSONIAN_API_KEY` | api.data.gov/signup | Free, instant. Shared limit 1000 req/hr → treated as optional. |
| 9 | `WIKIMEDIA_CONTACT` | you choose an email/URL string | **Required** in the User-Agent or Wikimedia returns 403. |

**YouTube realities:** (a) until Google audits the Cloud project, API uploads stay **Private**
and can't be published via API — pipeline uploads private/scheduled, you publish in Studio;
(b) quota 10,000 units/day ≈ **6 uploads/day** — `api_quota_ledger` enforces a daily budget
and defers; (c) AI voice **must** be disclosed — upload step sets the "synthetic content" flag.

### 3.2 Local software

`uv` · `Git` · `Python 3.11` · `FFmpeg` (Gyan build) · `PostgreSQL 16` (+pgAdmin) ·
`NVIDIA Studio driver` · `NSSM` · VS Code (have it). All via `winget` — see §4.
Python packages: `uv sync` from `pyproject.toml`. Model weights (~1.5 GB: Kokoro ONNX,
faster-whisper `small.en`, CUDA wheels) auto-download on first run. Keep ~30 GB free.

### 3.3 Curated asset libraries (you fill once; indexed into `asset` table)

| Folder | Contents | Sources (license-safe only) |
|---|---|---|
| `assets/music/` | 15–30 background tracks | **YouTube Audio Library** exports + verified **CC0** only. No other "royalty-free". |
| `assets/sfx/` | whooshes, risers, page-turns | Freesound filtered **CC0**, Kenney, Mixkit |
| `assets/fonts/` | 1 subtitle + 1 title `.ttf` | Google Fonts (OFL) — e.g. Inter, Bitter |
| `assets/branding/` | `logo.png` (alpha), optional intro/outro/endscreen | Canva free |
| `assets/thumbnails/` | 1–2 Pillow templates | — |

### 3.4 One-time manual config

`config.yaml` is the **only** input; `scripts/load_config.py` writes it into the `channel` row
with a `config_version`; the app reads config **only from the DB**. See `config.example.yaml`
for the full annotated schema. Key sections: channel identity + tone + **topic blocklist**;
video defaults (7 min, 7 Shorts ≤55 s, 1080p/1080×1920, −14 LUFS); Kokoro voice + measured
words/sec; subtitle style; discovery signal (`wikimedia_pageviews` primary); publish defaults
(category Education, synthetic-content flag on, API visibility `private`); ops (retention days,
free-disk floor, backup target, alert threshold); model revision pins; `test_mode: true`.

### 3.5 Recurring manual input — the review gate (per video)

Watch long-form + 7 Shorts → **verify on-screen claims against listed sources** → approve /
reject / re-render → edit title/description/tags/chapters → approve thumbnail → confirm image
licenses → confirm schedule. Analytics pull back automatically at 48 h and 7 d. Run the first
5–10 videos in `--dry-run` then `--visibility unlisted`.

### 3.6 `.env` keys

See `.env.example`. Git-ignored: `.env`, `config.yaml`, `client_secret.json`, `token.json`,
`assets/`, `data/` (which holds `work/`, `output/`, `cache/`, `logs/`). **`token.json` grants
upload rights — never let it into a backup or cloud-synced folder** (risk #19).

---

## 4. Install (Windows 11, native)

Grey boxes = **PowerShell**. Verify each step before moving on.

### 4.0 Windows prep
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
# admin PowerShell — enable long paths:
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```
After install, add `D:\Youtube_Pipeline\data` and `...\assets` to Windows Security →
Exclusions (AV locking files mid-render breaks FFmpeg — risk #30).

### 4.1 Base tools
```powershell
winget install --id astral-sh.uv -e
winget install --id Git.Git -e
winget install --id Python.Python.3.11 -e
winget install --id Gyan.FFmpeg -e
winget install --id PostgreSQL.PostgreSQL.16 -e
winget install --id NSSM.NSSM -e
```
Reopen PowerShell. Verify: `uv --version`, `git --version`, `python --version` (→ 3.11.x),
`ffmpeg -version` + `ffmpeg -hide_banner -encoders | Select-String nvenc`, `nvidia-smi`.
NVIDIA driver: install/update the latest **Studio Driver** via GeForce Experience (no CUDA
Toolkit).

### 4.2 PostgreSQL setup
Installer choices: keep Server + pgAdmin 4 + CLI tools; set & record the `postgres` password;
port 5432. Then in pgAdmin: create login role `ytpipe` (Can login, with password), create
database `ytpipe` owned by `ytpipe`. Verify:
```powershell
psql -U ytpipe -d ytpipe -h localhost -c "select version();"
```
`DATABASE_URL = postgresql+psycopg://ytpipe:PASSWORD@localhost:5432/ytpipe`

### 4.3 Project
```powershell
cd D:\Youtube_Pipeline
uv sync                      # reads .python-version (3.11), builds .venv, installs pinned deps
uv run python -c "import onnxruntime as ort; print(ort.get_available_providers())"  # expect CUDAExecutionProvider
mkdir data\work, data\output, data\cache, data\logs, `
      assets\music, assets\sfx, assets\fonts, assets\branding, assets\thumbnails
```
Prefer `uv run <cmd>` everywhere below instead of activating the venv. Media lives under
`STORAGE_BASE` (`data/`); `assets/` is separate read-only input.

### 4.4 Google / YouTube
1. Two Google accounts (§3.1 #1 and #3), 2FA on both.
2. Channel account → create YouTube channel → phone-verify → note Channel ID.
3. Second account → aistudio.google.com → create `GEMINI_API_KEY`.
4. Second account → Cloud Console → new project `yt-pipeline` → enable **YouTube Data API v3**
   + **YouTube Analytics API** → OAuth consent screen (External, Testing, add the *channel*
   account as a test user) → Credentials → OAuth client ID → **Desktop app** → download as
   `client_secret.json`. Copy `client_id`/`client_secret` into `.env`.
5. `python scripts\get_youtube_token.py` → browser login as the channel account → approve →
   copy the printed refresh token into `.env` (`token.json` also written).
6. api.data.gov/signup → `SMITHSONIAN_API_KEY` into `.env`.

### 4.5 Config + DB init  *(needs the build-phase `app/` + `scripts/` code)*
```powershell
copy .env.example .env ; notepad .env          # fill everything; real WIKIMEDIA_CONTACT; pick one ALERT_URL
copy config.example.yaml config.yaml ; notepad config.yaml
uv run alembic upgrade head
uv run procrastinate --app=app.queue.app schema --apply   # --app is required; keep separate from Alembic
uv run python scripts\load_config.py config.yaml
```

### 4.6 Smoke tests
`uv run python scripts\doctor.py` runs all of these and prints one green/red table:
`check_db` · `check_gemini` · `check_youtube` · `check_trends` · `check_images` · `check_tts`
· `check_whisper` · `check_ffmpeg` · `check_alert`. All green → ready to build.

### 4.7 First runs (safe)
```powershell
python scripts\run_pipeline.py --dry-run
python scripts\run_pipeline.py --visibility unlisted
```

### 4.8 Run unattended (NSSM services, admin PowerShell)
```powershell
nssm install YtPipeWorker    "D:\Youtube_Pipeline\.venv\Scripts\python.exe" "-m" "procrastinate" "--app=app.queue.app" "worker"
nssm set     YtPipeWorker    AppDirectory "D:\Youtube_Pipeline"
nssm set     YtPipeWorker    AppStdout "D:\Youtube_Pipeline\data\logs\worker.log"
nssm set     YtPipeWorker    AppStderr "D:\Youtube_Pipeline\data\logs\worker.log"
nssm set     YtPipeWorker    AppExit Default Restart
nssm start   YtPipeWorker
nssm install YtPipeDashboard "D:\Youtube_Pipeline\.venv\Scripts\python.exe" "-m" "uvicorn" "app.web:app" "--host" "127.0.0.1" "--port" "8765"
nssm set     YtPipeDashboard AppDirectory "D:\Youtube_Pipeline"
nssm set     YtPipeDashboard AppExit Default Restart
nssm start   YtPipeDashboard
```
Dashboard → http://127.0.0.1:8765.

### 4.9 Backups (set up now — risk #9)
Daily Scheduled Task running PowerShell (not cmd — `%DATE%` is literal in PowerShell):
```powershell
$stamp = Get-Date -Format yyyy-MM-dd
pg_dump -U ytpipe -h localhost -Fc ytpipe -f "E:\backups\ytpipe_$stamp.dump"
```
Target a **different physical drive**; optionally `rclone copy <STORAGE_BASE>/output remote:bucket`.
Keep 14 daily + 8 weekly; test a restore into a scratch DB quarterly.

### 4.10 Troubleshooting

| Symptom | Fix |
|---|---|
| `winget` not found | Install "App Installer" from Microsoft Store |
| `ffmpeg` not recognized after install | Reboot (PATH refresh) |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| onnxruntime shows only `CPUExecutionProvider` | Update NVIDIA Studio driver; runs on CPU meanwhile |
| `psql: could not connect` | `services.msc` → start `postgresql-x64-16` |
| Alembic vs procrastinate table conflict | Apply `procrastinate schema --apply` separately; `include_object` filter in `env.py` |
| FFmpeg "Permission denied" mid-render | AV exclusions for `data\` and `assets\` |
| Wikimedia 403 | `WIKIMEDIA_CONTACT` not a real contact string |
| Gemini `RESOURCE_EXHAUSTED` | Daily free cap; retries next day or slow the schedule |
| Upload OK but video Private, won't publish | Expected pre-audit — publish in Studio; submit the audit form later |
| Path too long | Confirm §4.0 reg key applied, reboot |

---

## 5. Data model

Multi-channel-ready from day one — `channel_id` FK on every table.

| Table | Purpose |
|---|---|
| `channel` | One row per channel: config snapshot + `config_version` + OAuth identity. FK target everywhere. |
| `candidate` | Discovered topic ideas + fit score + rationale. |
| `video` | Long-form unit: `channel_id`, `candidate_id`, `status`, script, metadata JSON, timings, paths. |
| `short` | `video_id`, index, `start_s`, `end_s`, `status`, `youtube_id`. |
| `segment` | `video_id`, index, narration text, `image_query`, `image_asset_id`, `start_s`, `end_s`. |
| `asset` | `kind` (image/music/sfx), `source`, `source_id`, `license`, `rights_url`, `attribution`, `uri`, meta. |
| `render` | Per render attempt: `status`, `output_uri`, log, duration, `{ffmpeg_version, kokoro_rev, whisper_rev, git_sha}`. |
| `upload` | `target_type`, `target_id`, `client_token`, `youtube_id`, `visibility`, `published_at`, `quota_units`. |
| `analytics_snapshot` | `youtube_id`, `captured_at`, views, watch time, AVD, retention JSON. |
| `api_quota_ledger` | `(api, day) → units/tokens used`. Checked before every paid/limited call; over-budget work defers. |
| `topic_performance` | Per topic/tag/length/thumb-style outcomes; read by the trend ranker. |
| `heartbeat` | `last_successful_discovery`, `last_successful_publish` — a periodic task alerts if stale. |
| `procrastinate_*` | Queue tables (own schema; managed by procrastinate migrations). |

**Rules:** every stage idempotent (temp path + atomic rename + "already done?" check);
workers stateless (all state in Postgres); no blobs in the DB (fsspec URIs only).

---

## 6. Risks & improvements

Tags: **[P0]** fix before first public upload · **[P1]** before scaling / full auto · **[P2]**
polish. IDs are stable and referenced elsewhere.

### Pre-launch checklist (P0)
`#1` real editorial review, not rubber stamp · `#2` synthetic-content flag on upload ·
`#3` grounded fact-check + reviewer verifies claims · `#4` US-PD/CC0 image filter + store
license · `#5` music = YT Audio Library + CC0 only · `#8` split Google accounts + 2FA ·
`#9`/`#42` nightly `pg_dump` + media copy to another drive, test restore · `#11` failure
alerts (apprise) + staleness heartbeat · `#19` `token.json` out of all backups, min OAuth
scopes · `#23` retention job + free-disk precheck · `#30` long-path key + AV exclusions ·
`#32` `--dry-run` / unlisted first 5–10 · `#44` topic blocklist + sensitivity gate ·
`#45` on-screen sources + description source block.

### A. Legal / policy / account
- **#1 [P0] Inauthentic-content policy.** Automated AI explainers on a schedule are what
  YouTube's 2025 spam rules target. Human gate must add real editorial judgement; distinct
  angle + real info value per video; never near-duplicates; "6/month" is a ceiling.
- **#2 [P0] AI disclosure.** Synthetic voice → set "altered/synthetic content" in Studio via
  the upload call; note it in the description.
- **#3 [P0] LLM factual errors.** `factcheck.md` verifies each claim with Search grounding,
  returns verdict + source; unsourced claims cut; on-screen citations; reviewer spot-checks;
  avoid contested history (see #44).
- **#4 [P0] Image licensing.** "Open access" ≠ PD everywhere; Wikimedia PD tags sometimes
  wrong; LoC "no known restrictions" ≠ PD. Filter to explicit US-PD/CC0; store
  `source/source_id/license/rights_url`; keep the review checkbox.
- **#5 [P0] Music Content ID.** `assets/music/` = YT Audio Library + verified CC0 only; store
  license + attribution; description builder appends CC-BY credits.
- **#6 [P1] Model licenses/provenance.** Confirm Kokoro + Whisper licenses allow commercial
  use and aren't clones of real people; record model + license + revision.
- **#7 [P2] pytrends ToS.** Technically scraping; low risk, kept non-critical and
  failure-tolerant; primary signal is the official Wikimedia Pageviews API.
- **#8 [P0] Account SPOF.** Channel on account A, Cloud+Gemini on B; 2FA + recovery on both;
  offline copy of `client_secret.json`.

### B. Data safety & reliability
- **#9 [P0] Single disk.** DB + all media on one Windows disk. Nightly `pg_dump -Fc` + media
  copy to a second drive/cloud; documented, tested restore (#42).
- **#10 [P1] Poison jobs.** On max-retry set `video.status='failed'` + alert + dashboard
  one-click retry/skip; weekly "nothing published in N days" check.
- **#11 [P0] Blind failures.** `apprise` → email/Telegram/ntfy on failure; heartbeat row
  checked by a periodic task; dashboard `/status` (#40).
- **#12 [P1] FFmpeg fragility.** Render in validated stages with intermediate files
  (`slideshow → +narration → +music(ducked) → +subs → final`), each `ffprobe`-checked. Pin
  the FFmpeg build; record version per render.

### C. Media correctness
- **#13 [P1] "Ken Burns on stills" look** reads as low-effort and hurts retention. Vary
  visual grammar: detail crops, two-ups, animated maps, timeline motion graphics, and
  interleave **PD moving footage** (LoC, Prelinger/archive.org, NASA); subtle consistent
  grade.
- **#14 [P1] Whisper drift on dates/proper nouns.** We know the text → forced alignment: pass
  known text as `initial_prompt`/`hotwords`; if recognized-word count vs script mismatch >
  `mismatch_fallback_ratio`, use proportional per-sentence timing. Consider `stable-ts`
  (keeps faster-whisper backend).
- **#15 [P1] Short lengths only known post-TTS.** Calibrate script word counts to Kokoro's
  measured words/sec; after TTS, hard-gate against the configured `max_seconds` (a style
  choice — YouTube's Shorts ceiling is 180 s) — auto-trim to a sentence boundary or
  regenerate the span.
- **#16 [P1] Double-upload on retry.** Write an `upload` row `uploading` + `client_token`
  *before* the call; on retry, first search the channel for that token; procrastinate `lock`
  serializes uploads per channel.

### D. Config / schema / ops
- **#17 [P1] Config drift.** `config.yaml` is the only input → loaded into `channel` row with
  `config_version` → app reads DB only → dashboard shows active version.
- **#18 [P1] Alembic vs procrastinate.** Apply procrastinate schema separately; Alembic
  `include_object` filter ignores `procrastinate_*`; consider a dedicated Postgres schema.
- **#19 [P0] Secret exposure.** Least-scope OAuth (`youtube.upload` + `youtube.force-ssl` +
  `yt-analytics.readonly` — `force-ssl` is required for thumbnail/playlist/metadata writes);
  `token.json` + `.env` out of every backup and cloud-synced folder; optional DPAPI
  encryption of `token.json`.
- **#20 [P1] In-process rate limiter** breaks with a 2nd worker. Stay single-worker until
  needed; then move the limiter to a Postgres token bucket (small change, documented trigger).

### E. External API constraints
- **#21 [P1] No HTTP cache / politeness.** `hishel` on-disk cache in front of `httpx`;
  `User-Agent` from `WIKIMEDIA_CONTACT` (or Wikimedia 403s); per-host concurrency caps +
  backoff; cache image-search results on the segment.
- **#22 [P1] api.data.gov 1000 req/hr shared.** Treat Smithsonian as optional; backoff on
  429; Wikimedia + Met + LoC carry the load.
- **#23 [P0] Unbounded disk growth** (~2–5 GB/video). Delete intermediates after a validated
  render; keep finals+Shorts `media_retention_days` then move masters to backup + prune;
  free-space precheck that alerts and pauses instead of failing mid-encode.

### F. Discovery / metadata / feedback
- **#26 [P1] Wrong discovery signal.** YouTube "mostPopular/Education" surfaces broad viral
  content. Primary = **Wikimedia Pageviews API** (trending + "most viewed" + "on this day");
  secondary = Reddit r/history / r/AskHistorians top, "year in search", pytrends rising.
  Gemini ranks candidates against scope + past performance (#28).
- **#27 [P1] LLM usage creep + grounding free cap.** Extend `api_quota_ledger` to Gemini
  (tokens + grounded-call counts per video); batch claims into fewer grounded calls; cache
  research per topic; alert at 80% of any daily budget.
- **#28 [P1] Analytics unused.** `topic_performance` table + a monthly Gemini "what worked"
  summary that updates `trend_rank.md` context; ranker demotes underperformers, favors
  proven patterns (#46).
- **#38 [P1] Metadata quality.** Generate 3–5 title candidates scored on length / curiosity
  gap / front-loaded keyword; description with chapter timestamps + sources + attributions;
  tags from a real search signal; hold final title for the reviewer.

### G. Product & quality
- **#24 [P1] No thumbnail generation.** Pillow template: subject cut-out + 3–4 word headline
  + brand frame; 2 variants per video for YouTube Test & Compare (#39).
- **#25 [P2] Chapters / cards / end screens.** Chapters from segment boundaries in the
  description; cards via API; **end screens can't be set via API** — manual review-step task.
- **#29 [P1] Testing gap.** Golden-file tests for SRT/ASS gen, quota math, Shorts cut math; a
  10-second synthetic end-to-end render in CI; `ffprobe` assertions on every real output.
- **#30 [P0] Windows fragility.** Long paths; AV exclusions; run NSSM services as your user;
  no spaces/unicode in generated filenames.
- **#31 [P1] Shutdown mid-render.** `*.tmp` + atomic rename; checkpoint stage completion;
  procrastinate graceful shutdown; startup sweep of stuck `in_progress` + `*.tmp`.
- **#32 [P0] No staging mode.** `--dry-run` + `--visibility unlisted` + `test_mode`; first
  5–10 videos unlisted.
- **#33 [P1] Generic single-pass scripts.** Outline → draft → self-critique → revise chain;
  house style guide in prompt; hard "hook in first 15 s"; retention-shaped structure;
  cliché/filler check.
- **#34 [P1] Monotone TTS for 7 min.** Vary pace at section breaks; 300–500 ms paragraph
  pauses (control Kokoro chunking); optional 2nd voice for quotes; light compression + de-ess.
- **#35 [P2] Static music level.** Sidechain-duck under narration; change track at chapter
  boundaries; music bed −16…−20 LUFS under a −14 master.
- **#36 [P2] Subtitles.** Burn styled captions **and** upload a sidecar `.srt` (accessibility
  + SEO).
- **#37 [P1] Near-identical Shorts** can trip "repetitious content" and cannibalize. Each gets
  its own hook, its own vertical composition (not just a center-crop), unique
  caption/title/description; spaced over days; skip Shorts for segments that don't stand
  alone.
- **#39 [P2] Thumbnail A/B.** Ship two; let Test & Compare pick.
- **#40 [P1] Observability page.** Dashboard `/status`: last discovery/publish, videos per
  state, failed jobs, quota used today (YouTube + Gemini), free disk, GPU temp.
- **#41 [P2] Cost/usage view.** Per-video tokens + API units + render minutes, trended.
- **#42 [P1] Backup mechanics.** `pg_dump -Fc` nightly to a 2nd drive; `rclone copy output/`
  to a free bucket (B2 10 GB / R2); keep 14 daily + 8 weekly; quarterly test restore.
- **#43 [P1] Reproducibility.** Commit `uv.lock`; pin FFmpeg build; pin model revision hashes
  in `config.yaml`; record tool versions per `render` row.
- **#44 [P0] Content-safety gate.** Two-tier `config.yaml` lists — `auto_veto` (candidate
  skipped, no human) and `manual_review` (human yes/no before scripting). `sensitivity_check.md`
  classifies each candidate as pass / manual_review / veto; anything uncertain → manual_review.
- **#45 [P0] Legal cushion.** On-screen citations; description source + attribution block;
  "educational commentary" framing; a review step that genuinely watches the whole video —
  quality control and policy defense in one.
- **#46 [P2] Topic performance memory.** Never re-run a clear loser; revisit winners with new
  angles.
- **#47 [P2] Localization-ready.** Keep script / TTS / subs decoupled so a second-language
  channel is additive.

### Genuinely fine as-is
Postgres as DB + queue · Gemini 2.5 Flash brain · FFmpeg renderer (risk is *how* it's
invoked, #12) · ONNX + CT2 with no PyTorch · single machine for 10× this volume (with #9/#42)
· fsspec storage abstraction · `channel_id` FK everywhere.

---

## 7. Scalability

| Concern | Now | ~10× (60 long-form + 420 Shorts/mo) | Ceiling |
|---|---|---|---|
| **Compute** | ~1.6 videos/day; a render ≈ 4–8 min with NVENC | ~5–6 renders/day — GPU still mostly idle. Raise procrastinate concurrency, optional 2nd worker same box. | ~30–40 long-form/day → a second stateless worker on another box, same Postgres. |
| **Storage** | Local disk via fsspec; masters pruned after N days | Point fsspec at B2 / S3 — **config change, no code**. | Never forces a rewrite. |
| **YouTube quota** | 10k units/day ≈ 6 uploads/day; ledger defers over-budget; Shorts spread over days | Submit the free quota-increase request; ledger logic unchanged. | Hard external cap; handled by deferral + increase. |
| **Gemini limits** | Free tier RPM/day; aiolimiter + retries absorb | Paid tier (cents/script); batch grounded fact-checks | Swap model string; SDK unchanged. |
| **Multi-channel** | One `channel` row | Insert more rows + assets/prompts; everything is per-channel | No migration — FK already everywhere. |
| **Single-machine failure** | Nightly `pg_dump` + media backup | Same; fsspec already cloud → only Postgres is local | Move Postgres to a managed instance (connection-string change). |
| **Human review** | ~6 sessions/month | Real work; dashboard batch-approve; unattended build continues regardless | Operations limit, not a stack limit. |

---

## 8. Build order & conventions

Decisions for when `app/` code starts. New tooling files (`justfile`,
`.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `config.schema.json`) are added in the
step that first needs them, not before.

### 8.1 Conventions (apply from the first module)
- **Env:** `uv` only; `uv run <cmd>` everywhere (no manual venv activation). `.python-version`
  pins 3.11.
- **Config flow:** a pydantic `Settings` / `ChannelConfig` model **is** the schema.
  `scripts/load_config.py` validates `config.yaml`, writes it to the `channel` row with a
  bumped `config_version`, and dumps `config.schema.json` (`model_json_schema()`) for editor
  validation. Runtime code reads config **only from the DB**, never the file.
- **Status:** one `Status` `StrEnum` in `app/status.py`, used by DB columns, the dashboard,
  and the CLI. No bare status strings anywhere.
- **HTTP:** one factory in `app/http.py` — httpx client + `hishel` disk cache + `User-Agent`
  from `WIKIMEDIA_CONTACT` + timeouts + a shared `tenacity` retry preset. Every API module
  imports it; no ad-hoc clients.
- **Storage:** all file I/O through one `app/storage.py` helper over `fsspec`, rooted at
  `STORAGE_BASE` (`work/ output/ cache/ logs/` beneath it). `assets/` is read-only input.
  Write to `*.tmp` + atomic rename; never a bare path.
- **Logging:** `loguru` — JSON sink to `data/logs/`, pretty console sink. Secrets filtered
  out. The dashboard tails the JSON log.
- **Secrets:** `pydantic-settings` from `.env`; never logged, never in a `render`/`upload`
  row.

### 8.2 Efficiency measures (bake in, don't retrofit)
- **LLM cache:** `llm_cache` table keyed by `sha256(model + prompt + inputs)` → response.
  Dev re-runs cost zero tokens. `api_quota_ledger` also records Gemini tokens + grounded-call
  counts per video.
- **Resident models:** the worker loads Kokoro + faster-whisper **once** at startup
  (module-level singletons), not per task.
- **Render:** `encoder: auto` probes `h264_nvenc` once, falls back to `libx264`;
  `render_concurrency` from config (default 1); staged intermediates, each `ffprobe`-checked
  (#12).
- **Scheduling:** procrastinate periodic tasks — `discover` weekly, `advance` daily; all
  times resolved in `config.timezone`.
- **Idempotency:** every stage checks "already done?" in the DB before working; jobs and
  `*.tmp` stuck `in_progress` are swept on worker startup (#31).

### 8.3 Suggested module / build sequence
1. `app/config.py` + `app/db.py` (SQLAlchemy models, all tables from §5) + Alembic init +
   `scripts/load_config.py`.
2. `app/status.py`, `app/http.py`, `app/storage.py`, `app/queue.py` (procrastinate app),
   `app/notify.py` (apprise).
3. `scripts/get_youtube_token.py`, `scripts/doctor.py` + the nine `check_*` probes.
4. `tests/` with golden fixtures committed now — `sample_script.md` → expected `sample.srt`
   + expected Shorts cut list; `ffprobe` assertions helper.
5. `.pre-commit-config.yaml` (ruff, ruff-format, gitleaks) + `.github/workflows/ci.yml`
   (ruff + pytest, no secrets needed for this tier).
6. `justfile`: `setup sync smoke run worker dash test fmt lint`.
7. Stages, in pipeline order: `discover → rank → research → factcheck → script → segment →
   images → tts → align → assemble → shorts → metadata → thumbnail → review → upload →
   analytics`. Each is one procrastinate task that enqueues the next.
8. `app/web.py` review dashboard last (it only reads state the stages produce).
