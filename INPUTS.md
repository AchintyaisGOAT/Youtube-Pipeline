# Project Inputs — Final (locked stack)

**Your setup:** Windows 11 · NVIDIA GPU 8–12 GB · Python 3.11 · strictly $0 · English (US) ·
History/educational explainers · auto-discover trends · fully scheduled · ~7 min long-form +
7 Shorts cut from it · ~6 long-form + 42 Shorts per month · native installs (no Docker) ·
new to Google Cloud / YouTube.

Stack is frozen — see [STACK.md](STACK.md). Known risks & improvements — see [REVIEW.md](REVIEW.md).

---

## A. Cloud accounts & API keys you must create (all free)

Create them in this order. Section C of [INSTALL.md](INSTALL.md) has click-by-click steps.

| # | Input (env var) | Where to get it | Free? | Notes / limits |
|---|---|---|---|---|
| 1 | **Channel Google account** | accounts.google.com | Free | Dedicated, not personal. 2FA on. |
| 2 | **YouTube channel** | youtube.com → Create channel | Free | Do phone verification (raises upload limits). |
| 3 | **Second Google account for Cloud/Gemini** | accounts.google.com | Free | *Deliberately separate from the channel account* — see [REVIEW.md](REVIEW.md) risk #8. |
| 4 | **Google Cloud project** | console.cloud.google.com (on account #3) | Free | Name it `yt-pipeline`. No billing needed for what we use. |
| 5 | `GEMINI_API_KEY` | aistudio.google.com (account #3) → Get API key | Free tier | Gemini 2.5 Flash: ~15 RPM, 250 req/day, 250K TPM. Search-grounding has a separate, smaller free daily cap. |
| 6 | `YOUTUBE_CLIENT_ID` + `YOUTUBE_CLIENT_SECRET` | Cloud Console → Credentials → OAuth client ID → **Desktop app** → downloads `client_secret.json` | Free | Enable **YouTube Data API v3** + **YouTube Analytics API** first. |
| 7 | `YOUTUBE_REFRESH_TOKEN` | Generated once by `scripts/get_youtube_token.py` (browser login) | Free | **Request minimum scopes only:** `youtube.upload`, `youtube.readonly`, `yt-analytics.readonly`. Not full `youtube`. |
| 8 | `YOUTUBE_CHANNEL_ID` | YouTube Studio → Settings → Channel → Advanced | Free | — |
| 9 | `SMITHSONIAN_API_KEY` | api.data.gov/signup | Free, instant | Shared limit **1000 req/hour**. Treated as an optional image source. |
| 10 | `WIKIMEDIA_CONTACT` | *You choose* — an email/URL string | — | **Required** in the User-Agent header or Wikimedia blocks the requests. e.g. `yt-pipeline (you@example.com)`. |

**Nothing is paid.** No image-generation API, no SerpAPI, no cloud server, no Redis product.

### ⚠️ YouTube API realities (plan for these — details in REVIEW.md)
1. **Unaudited project → uploads locked to Private.** Until Google completes the one-time
   "YouTube API Services compliance audit", every API upload stays private and can't be made
   public via API. Workaround: pipeline uploads private/scheduled, you publish in Studio.
2. **Upload quota 10,000 units/day ≈ 6 uploads/day.** The `api_quota_ledger` table enforces a
   daily budget and defers over-budget uploads to the next day; the scheduler spreads the 7
   Shorts across days. Free quota-increase request available before you scale.
3. **AI-content disclosure is mandatory.** AI voice = you must set the "altered or synthetic
   content" flag. The upload step sets it automatically.

---

## B. Software to install locally (all free, native Windows — see INSTALL.md)

| Input | Purpose | Install |
|---|---|---|
| **uv** | Python env + dependency resolver + lockfile | `winget install astral-sh.uv` |
| **Git for Windows** | Version control / code backup | `winget install Git.Git` |
| **Python 3.11** (exact) | Runs the whole pipeline | `winget install Python.Python.3.11` (uv can also fetch it) |
| **FFmpeg + FFprobe** (libass + NVENC) | All audio/video work, subtitle burn-in, Shorts crop | `winget install Gyan.FFmpeg` |
| **PostgreSQL 16** (+ **pgAdmin 4**) | Database **and** job queue (via procrastinate) | `winget install PostgreSQL.PostgreSQL.16` |
| **NVIDIA Studio driver** (latest) | GPU for Kokoro (ONNX) + Whisper (CTranslate2) | GeForce Experience / nvidia.com. **No CUDA Toolkit.** |
| **NSSM** | Run worker + dashboard as auto-restarting Windows services | `winget install NSSM.NSSM` |
| **VS Code** | Editor (you have it) | — |

> **Removed vs earlier draft:** Redis/Memurai, PyTorch, ffmpeg-python — no longer installed.

### Python packages (installed by `uv`, pinned in `uv.lock`)

**Runtime**
```
pydantic  pydantic-settings  pyyaml
sqlalchemy  alembic  psycopg[binary]
procrastinate
httpx  hishel  tenacity  aiolimiter  loguru
fsspec
google-genai
google-api-python-client  google-auth-oauthlib  google-auth-httplib2
pytrends
kokoro-onnx  soundfile  onnxruntime-gpu
faster-whisper
huggingface_hub
pillow  pysubs2
fastapi  uvicorn[standard]  jinja2  python-multipart
apprise
```
**Dev**
```
ruff  pytest  pytest-asyncio
```
(`hishel` = HTTP cache for httpx; `apprise` = one-line notifications to email/Telegram/ntfy —
see REVIEW.md #11.)

### AI model weights (auto-download on first run, then cached — no manual step)

| Model | Size | From | Pin |
|---|---|---|---|
| Kokoro ONNX + voice packs | ~350 MB | HF `hexgrad/Kokoro-82M` (ONNX) | pin the revision hash |
| faster-whisper `small.en` | ~0.5 GB | HF `Systran/faster-whisper-small.en` | pin the revision hash |
| onnxruntime-gpu CUDA/cuDNN wheels | ~0.5 GB | pip | in `uv.lock` |

One-time download ≈ 1.5 GB. Keep ~30 GB free for weights + per-run media (see REVIEW.md #23).

---

## C. Local asset libraries you provide once (curated folders)

Indexed into the `asset` table with mood/length/tags on first scan.

| Folder | Contents | Free, license-safe sources |
|---|---|---|
| `assets/music/` | 15–30 background tracks, varied mood/length | **YouTube Audio Library only** (Studio → Audio Library, filter "no attribution required"), or verified **CC0** from Free Music Archive. *No random "royalty-free" downloads* — Content ID risk (REVIEW.md #5). |
| `assets/sfx/` | Whooshes, risers, page-turns, soft stingers | Freesound filtered to **CC0**, Kenney.nl, Mixkit |
| `assets/fonts/` | 1 subtitle font + 1 title font (`.ttf`) | Google Fonts (OFL, embedding OK) — e.g. *Inter*, *Bitter*, *Libre Franklin* |
| `assets/branding/` | `logo.png` (alpha), optional `intro.mp4`, `outro.mp4`, `endscreen.png` | Make in Canva free, or I can generate simple ones |
| `assets/thumbnails/` | 1–2 thumbnail template files | Pillow-driven template (REVIEW.md #24) |

CC0 preferred everywhere so descriptions stay clean. Any CC-BY asset → the pipeline appends
the required credit line to the description automatically.

---

## D. Manual inputs — one-time configuration

`config.yaml` is the **single source of truth**. On startup it's loaded into the `channel`
row with a `config_version`; the app only ever reads the DB (REVIEW.md #17). Defaults will be
provided for every field.

**Channel identity**
- Name, one-line description, audience
- Tone of voice (e.g. "authoritative, accessible, dry wit, never clickbait")
- In-scope topics (e.g. "world history pre-2000, history of science/technology")
- **Hard topic blocklist** (e.g. active politics, ongoing conflicts, recent tragedies,
  contested/atrocity history, medical/legal claims) — REVIEW.md #44

**Video defaults**
- Long-form target: **7 min** (~1,000–1,150 narration words — calibrate to Kokoro's measured
  words/sec, REVIEW.md #15)
- Shorts: **7 per long-form**, ≤ 55 s each, chosen at script time as `[SHORT]…[/SHORT]` spans
- Long-form 1920×1080@30, Shorts 1080×1920@30
- Loudness −14 LUFS integrated; music ducked under narration (sidechain), not static
- Pacing ~2.5–3.5 s/image; vary shot types (full / detail crop / map / side-by-side)

**Voice (Kokoro)**
- Primary: British male documentary (`bm_george` / `bm_lewis`)
- Optional secondary for quotes: British female (`bf_emma`)
- Speaking rate, inter-paragraph pause length (300–500 ms)

**Subtitles**
- Font, size, bottom-center + safe margin, 2-line max, current-word highlight color, karaoke on
- Burn in **and** upload a sidecar `.srt` (REVIEW.md #36)

**Prompt templates** (`prompts/`)
- `trend_rank.md`, `research.md`, `factcheck.md`, `script_outline.md`, `script_draft.md`,
  `script_revise.md` (outline→draft→self-critique→revise, REVIEW.md #33), `metadata.md`,
  `sensitivity_check.md`

**Publish defaults**
- Category *Education*, English, "not made for kids", altered/synthetic-content flag = on
- API visibility = **Private** (audit note) → publish in Studio
- Playlist for long-form; schedule slots (e.g. long-form Tue/Fri 15:00, Shorts daily 12:00)

**Ops**
- Daily discovery run time (e.g. 06:00)
- Target long-form/month (the "6")
- Notification channel for failures: email SMTP creds **or** a Telegram bot token **or** an
  ntfy.sh topic (`apprise` supports all three) — REVIEW.md #11
- Media retention: keep finals + Shorts N days, delete intermediates on success (REVIEW.md #23)
- Backup target for `pg_dump` + `output/` masters (external drive path or free cloud bucket)

---

## E. Manual inputs — recurring (the review gate, per video)

Every video pauses here until you act (dashboard or DB row):

1. Watch the long-form + all 7 Shorts
2. **Actually verify the on-screen factual claims against the listed sources** — this is also
   your policy defense (REVIEW.md #3, #45)
3. Approve / reject / re-render (with a note)
4. Edit title / description / tags / chapters
5. Approve or replace the thumbnail
6. Confirm image licenses + attributions
7. Confirm / adjust schedule
8. After upload: nothing — analytics pull back automatically at 48 h and 7 d

First 5–10 videos: run in **dry-run / unlisted-test mode** before touching the live channel
(REVIEW.md #32).

---

## F. Secrets file (`.env`) — final list

```dotenv
# --- Gemini (Google account #3) ---
GEMINI_API_KEY=

# --- YouTube (from client_secret.json + token script; channel account) ---
YOUTUBE_CLIENT_ID=
YOUTUBE_CLIENT_SECRET=
YOUTUBE_REFRESH_TOKEN=
YOUTUBE_CHANNEL_ID=

# --- Image sources ---
SMITHSONIAN_API_KEY=
WIKIMEDIA_CONTACT=yt-pipeline (you@example.com)

# --- Local infra (procrastinate shares this DB; no Redis) ---
DATABASE_URL=postgresql+psycopg://ytpipe:YOURPASSWORD@localhost:5432/ytpipe

# --- Storage (fsspec URL — local now, s3:///b2:// later, no code change) ---
STORAGE_BASE=file:///D:/Youtube_Pipeline/data

# --- Paths ---
ASSETS_DIR=D:/Youtube_Pipeline/assets
WORK_DIR=D:/Youtube_Pipeline/work
OUTPUT_DIR=D:/Youtube_Pipeline/output

# --- Failure notifications (pick one; apprise URL) ---
ALERT_URL=ntfy://ntfy.sh/your-private-topic-name
```

Git-ignored: `.env`, `client_secret.json`, `token.json`, `assets/`, `work/`, `output/`,
`data/`. **`token.json` grants upload rights to your channel — never let it into any backup
or cloud-synced folder** (REVIEW.md #19).
