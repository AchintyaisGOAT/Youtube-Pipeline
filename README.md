# OurGreatHistory Pipeline: Design & Reference (`testing` branch)

The single reference for this branch. It covers what the pipeline is, every decision and the reason
for it, the stack, the full workflow, and setup. **Update this file when a decision changes.** Don't start
new docs. The decision log (§12) records what changed and when.

> **Branch status:** this is the *target* design for the `testing` branch, adapted from the
> OurGreatHistory (OGH) design. It isn't fully built yet; §13 tracks what is.
> `main` still has the original design (Gemini-only, AI images, YouTube API upload,
> Task Scheduler). This branch is merged into `main` only after it's reviewed and approved.

- [1. Overview](#1-overview)
- [2. Fixed constraints](#2-fixed-constraints)
- [3. Stack](#3-stack)
- [4. Workflow](#4-workflow)
- [5. LLM routing](#5-llm-routing)
- [6. Media rules](#6-media-rules)
- [7. Output: the upload kit](#7-output-the-upload-kit)
- [8. Data & storage](#8-data--storage)
- [9. Repo layout](#9-repo-layout)
- [10. Setup](#10-setup)
- [11. Limits, risks & rejected options](#11-limits-risks--rejected-options)
- [12. Decision log](#12-decision-log)
- [13. Build status](#13-build-status)

---

## 1. Overview

A local tool that turns a history topic into a finished YouTube package:

- one **long-form video** (3–8 min, 1920×1080),
- **7 Shorts** made from it (≤55 s each, 1080×1920),
- plus a thumbnail, subtitles and ready-to-paste metadata.

A human reviews every video, then uploads it to YouTube by hand.

- **Channel:** OurGreatHistory (`@OurGreatHistory`). Confirm the handle is free when creating the channel.
- **Niche:** world history and the history of science/technology, before 2000.
- **Tone:** energetic, fun, a little cheeky. Like a friend telling you the wildest true story they
  know: never a dry lecture, always accurate and sourced.
- **Volume:** ~6 long-form videos and ~42 Shorts per month, one video in progress at a time.

```
discover → gate → rank → research (Wikipedia) → script → check against sources
  → scene plan → images → narration → word timing → assemble long-form → render Shorts
  → metadata + thumbnail → upload kit → human review → manual upload to YouTube
```

---

## 2. Fixed constraints

These drive every choice below. If one changes, revisit the stack.

| Constraint | Value | Consequence |
|---|---|---|
| Machine | Laptop with an Intel Core Ultra 5 125H (14 cores), Intel Arc integrated graphics, 16 GB RAM | No CUDA, so local AI runs on CPU. Video encoding uses Intel Quick Sync (`h264_qsv`). No local LLM or local image generation (too slow). |
| Budget | **$0**: free tiers only | Every cloud service must have a permanent free tier. Rate limits are the constraint, not price. |
| Running | **Manual**: one command when you choose | No scheduler and no background service. Every run is safe to stop and restart. |
| Visuals | Public-domain / open archive images first | AI illustrations only as a fallback when the archives have nothing (§6.1). |
| Upload | **Manual** via YouTube Studio | No YouTube API, no Google Cloud project, no OAuth (§7). |
| Language | English (US) | |

---

## 3. Stack

### 3.1 By layer

| Layer | Choice | Notes |
|---|---|---|
| Language | **Python 3.11**, managed by **uv** (`uv.lock` committed) | uv installs 3.11 itself. The system Python (3.14) isn't used. |
| Config | `config.yaml` (channel and behaviour, committed) + `.env` (keys, git-ignored) | Both validated with pydantic / pydantic-settings on startup. |
| Database | **SQLite** (`data/pipeline.db`) via SQLAlchemy 2.0 | One file, no server. Each video's `status` records how far it got, which makes runs resumable. No migrations: `create_all()`. |
| Orchestration | `ogh run`: one sequential command | Moves every video forward as far as it can, then exits. |
| HTTP | httpx + tenacity (retries with backoff) | |
| Logging | loguru → console + `data/logs/` | |
| LLMs (cloud) | **Gemini** via `google-genai`; **Groq** via the `openai` SDK (OpenAI-compatible endpoint) | Routing per step in §5. Model IDs live in config, never in code. |
| Topics (cloud) | Wikimedia Pageviews API (trending) + Wikipedia "On this day" feed | Free, no key. |
| Research (cloud) | Wikipedia article text + a few linked articles | Free, no key. These articles are the video's only source of facts. |
| Images (cloud) | Wikimedia Commons, Library of Congress, Smithsonian Open Access, then a Gemini image model as fallback | Public domain / CC0 only for archive images. Smithsonian key optional. |
| Narration (local) | **Kokoro** via `kokoro-onnx` on CPU (`onnxruntime`) | Free, offline. |
| Word timing (local) | **faster-whisper** `small.en`, CPU, int8 | Real per-word timestamps for subtitle highlighting and Shorts cut points. |
| Video (local) | **FFmpeg**, called directly (no wrapper library) | Encoder `h264_qsv`, falling back to `libx264`. |
| Images/subs (local) | Pillow (thumbnails), pysubs2 (subtitles) | |
| CLI | One entry point, `ogh` (`app/cli.py`): `run` · `review` · `doctor` | Declared in `pyproject.toml`. No loose scripts. |
| Dev | ruff, pytest | |

### 3.2 External software (not pip)

`uv`, `Git` and `FFmpeg` (Gyan build), installed with `winget` (§10).
Model weights (Kokoro + Whisper, ~500 MB) download on the first run.

### 3.3 Accounts & keys

| Key (`.env`) | Where to get it | Required? |
|---|---|---|
| `GEMINI_API_KEY` | aistudio.google.com | Yes |
| `GROQ_API_KEY` | console.groq.com | Yes |
| `WIKIMEDIA_CONTACT` | You choose it; it must be a **URL** (e.g. the channel or repo page) | Yes. Wikimedia returns 403 without it. |
| `SMITHSONIAN_API_KEY` | api.data.gov/signup | Optional |
| `ALERT_URL` | e.g. an ntfy.sh topic URL | Optional |

You also need one Google account that owns the YouTube channel. No Google Cloud project is needed.

---

## 4. Workflow

### 4.1 Stages

Each stage reads the previous stage's output from the DB and disk, writes its own, and advances the
video's `status`.

- A **temporary** problem (free-tier limit hit, provider overloaded) leaves the status unchanged, so the next run resumes from there.
- A **real error** marks the video `failed` and saves the error. `ogh review` can send it back to retry.

| # | Stage | Where | Tool | Output |
|---|---|---|---|---|
| 1 | **Discover** topics. Runs when no passed topic is waiting; unused passed topics expire after 7 days. A free pre-filter on each article's short description drops meta pages, living people, anything dated 2000+, and entertainment/sport before any LLM sees it. | Cloud | Wikimedia Pageviews (trending) + Wikipedia "On this day" (today + 2 days) | Candidate topics |
| 2 | **Gate**: drop auto-veto and off-niche topics (§4.3), score relevance 0–10; below 6 is vetoed | Cloud | LLM (§5), up to 20 candidates per call | Passed / vetoed, with a relevance score |
| 3 | **Rank** and pick the next topic, only when no other video is in progress | Local | Python score: trend (log pageview rank, or anniversary roundness: 100th > 50th > 25th > 10th) + relevance; titles never repeat | One chosen topic → new video |
| 4 | **Research**: fetch the topic's Wikipedia article plus up to 3 linked articles. Candidates are the article's most-mentioned links (surnames count), minus generic pages; the worker LLM picks the ones that add the most story | Cloud | Wikipedia API + LLM (§5, one small call) | Article texts + their references (the video's source list) |
| 5 | **Script**, 3–8 min, **only from the researched articles**, with 7 `[SHORT]` passages and every direct quotation marked `[QUOTE]` | Cloud | LLM (§5) | Script |
| 6 | **Check**: every claim must appear in the articles. Unsupported claims are removed or rewritten, and each change is recorded for review. A check that breaks the markup or cuts over 40% of the words is treated as failed. | Cloud | A *different* LLM from the writer (§5) | Checked script + list of changes |
| 7 | **Scene plan**: Python splits the script into scenes of about 5–7 s (never splitting a name or a `[SHORT]` edge, and keeping quotes whole), so the narration can't change. The LLM then writes one archive search query per scene. | Local + Cloud | Python + LLM (§5) | Scene list |
| 8 | **Images** for each scene | Cloud | Archives, then AI fallback (§6.1) | Local image files + license records |
| 9 | **Narration** | Local | Kokoro: `bm_george`, with `bf_emma` for quotes | WAV audio |
| 10 | **Word timing** | Local | faster-whisper | A timestamp for every word |
| 11 | **Assemble long-form**: pan/zoom on images, subtitles, music bed, loudness | Local | FFmpeg | `long.mp4` |
| 12 | **Render Shorts**: the 7 marked passages, re-rendered vertically | Local | FFmpeg | `short_01.mp4` … `short_07.mp4` |
| 13 | **Metadata**: title options, description, chapters, tags, thumbnail hook text | Cloud | LLM (§5) | Metadata |
| 14 | **Thumbnail** | Local | Pillow template | `thumbnail.png` |
| 15 | **Upload kit** assembled (§7) | Local | Python | A folder in `data/output/` |
| 16 | **Review gate** | Local | `ogh review` | Approved / sent back / rejected |
| 17 | **Manual upload** in YouTube Studio, then marked uploaded in `ogh review` | You | YouTube Studio | Published video |

### 4.2 Statuses

Topics: `candidate → passed | vetoed → used`. A passed topic becomes a video when rank picks it.

Videos:

```
selected → researched → scripted → checked → segmented → images_ready → narrated
  → aligned → assembled → shorts_ready → packaged → approved → uploaded
                                                  ↘ rejected
```

`packaged` means the upload kit is built and the video is waiting for `ogh review`. Plus
`failed`, with the error saved. `ogh review` can send a video back to any earlier status to
redo it from there.

### 4.3 Topic safety

- **Auto-veto** (skipped, no human involved):
  - events within the last 10 years
  - active elections / party politics
  - ongoing armed conflicts
  - living-person allegations or controversy
  - medical, legal or financial advice
- **Everything else in scope proceeds automatically**, including heavy subjects such as genocide,
  atrocities, religion, or disputed historical framing. The script prompt tones down the cheeky
  voice for tragic subjects, and every video still passes human review before upload.

### 4.4 Your commands

```powershell
uv run ogh run       # advance everything as far as possible (--discover: look for topics now)
uv run ogh review    # review finished videos: approve / reject / send back / pick title / mark uploaded
uv run ogh doctor    # green/red health check of config, keys, DB, FFmpeg + encoder, disk
                     # (--live: also confirm the keys work and every model ID in config exists)
```

---

## 5. LLM routing

All free tiers. Model IDs are set in `config.yaml`, so a model can be swapped without code changes.
**Confirm the current model IDs and limits in AI Studio / the Groq console. They change often.**

| Step | Primary | Fallback | Why |
|---|---|---|---|
| Gate + relevance | Groq `openai/gpt-oss-120b` | `gemini-3.5-flash-lite` | High daily limit, simple classification; all candidates in one call |
| Research | *No LLM*: Wikipedia article text | — | Free, unlimited and trusted; its references become the source list |
| Script | `gemini-3.8-flash` | Groq `openai/gpt-oss-120b` | Best free writing quality; reads all the articles (1M context) |
| Check | `gemini-3.5-flash-lite` | — | Must differ from the writer; reads all the articles, which Groq's 8K tokens/min can't fit |
| Research: pick linked articles | Groq `openai/gpt-oss-120b` | `gemini-3.5-flash-lite` | Small judgement call |
| Scene plan (image queries) | Groq `openai/gpt-oss-120b` | `gemini-3.5-flash-lite` | Structured output |
| Metadata + thumbnail hook | `gemini-3.8-flash` | Groq `openai/gpt-oss-120b` | Titles and hooks benefit from the stronger writer |
| AI illustration (fallback only) | `gemini-3.1-flash-image` | — | Only when no archive image is found (§6.1) |

**Why check at all:** Wikipedia is trusted. The risk is the LLM rewriting it: adding "facts" from its
own memory, inflating numbers, inventing quotes. The check compares the script with the articles,
not the articles with the web.

**Budget:** about 5 text-LLM calls per video (gate, script, check, scene plan, metadata), plus image
calls only for scenes the archives can't cover. At 6 videos a month this is far inside every limit.
When a limit is hit, the video just waits for the next run. Every call is logged in the `llm_call`
table.

**Free-tier facts (tested 2026-09-26):**
- `gemini-2.5-flash` / `-flash-lite`: **404, "no longer available to new users"**.
- Google Search grounding on 3.x Flash models: **429 on the first request**. There's no free quota,
  so no grounded research.
- Plain `gemini-3.8-flash` and `gemini-3.5-flash-lite` work. 3.x Flash allows roughly **20 requests
  per day**; exact numbers are only shown in the AI Studio dashboard.
- Gemini free-tier prompts may be used by Google to improve its models. That's acceptable for public
  history content.
- Groq free tier (`gpt-oss-120b`): 30 requests/min, 1,000 requests/day, **8K tokens/min**, 200K
  tokens/day. Long prompts must wait between calls, and the client handles this. Groq doesn't train
  on API data.
- The image model's free-tier limit is unconfirmed. Check it in AI Studio before relying on the
  fallback.

---

## 6. Media rules

### 6.1 Images

1. **Archives first**, in this order: Wikimedia Commons → Library of Congress → Smithsonian Open Access.
   These must be **public domain / CC0 only**. Each image's source URL, author and license are saved
   and credited in the upload kit.
2. **No match:** retry with broader, topic-level search terms.
3. **Still nothing:** generate an **AI illustration** in a **vintage/period style** (engraving,
   period print, oil painting) so it blends with archival material. It gets the video's topic as
   context, not just the scene's sentence, and is credited as AI-generated.
4. **AI unavailable** (quota or failure): **reuse an image already in this video**, the one used longest
   ago, with a different pan/zoom so it doesn't look repeated.

A video never stalls on a missing image. AI can be switched off entirely in config.

Other rules:
- At most ~80 unique images per video; beyond that, images are reused.
- Downloaded images are cached in `data/cache/` and reused across videos.

### 6.2 Narration

- Kokoro voices: `bm_george` narrates and `bf_emma` reads direct historical quotes. Verify the IDs
  against the kokoro-onnx voice list.
- ~2.6 words/second (measure once and update config); 400 ms pause between paragraphs.

### 6.3 Music

- **YouTube Audio Library only**, filtered to **"attribution not required"** tracks. They're free for
  YouTube use and safe from Content ID claims.
- Not Pixabay or similar: their tracks sometimes get claimed by uploaders who registered them with
  Content ID.
- The library has no API. Download 15–30 tracks once into `assets/music/` and list each in
  `assets/music/manifest.yaml` (file, title, artist, license). If a track needing credit is ever used,
  its credit goes into the description automatically.
- Music sits at −18 LUFS under narration and is lowered further while speech plays. The final mix is
  −14 LUFS. If `assets/music/` is empty, videos render without music.

### 6.4 Video

| | Long-form | Shorts |
|---|---|---|
| Length | 3–8 min (fits the story) | ≤55 s each, 7 per video, cut at sentence ends |
| Resolution | 1920×1080, 30 fps | 1080×1920, 30 fps |
| Images | 5–7 s per scene, varied slow pan + zoom (in/out, left/right, up/down) | Re-rendered from the same scenes, panning across each image; not a crop of the long-form |
| Subtitles | Burned in, bottom-center, 3–4 words at a time, current word highlighted (`#FFD23F`), timed by real word timestamps; size set in real pixels | Same style, larger, plus a title caption |
| Encoder | `h264_qsv`, fallback `libx264`, `yuv420p` pixel format (4:2:2 won't play everywhere) | Same |

Pictures, narration and subtitles share one timeline. Every scene covers its speech *and* the pause
after it, so images never drift out of sync with the voice.

Fonts: `assets/fonts/*.ttf` from Google Fonts (e.g. Inter). Branding: `assets/branding/logo.png`.

### 6.5 Thumbnail

- The most striking image from the video.
- A 2–4 word hook written by the LLM (not just the first words of the title), in bold outlined text.
- A branded frame and logo.
- One thumbnail per video; sending the video back to the thumbnail stage makes a new one.

---

## 7. Output: the upload kit

**Why manual upload** (decided 2026-09-26):
- An unverified Google Cloud project can only upload *Private* videos, so you'd open Studio anyway.
- Lifting that restriction needs a compliance audit.
- An OAuth app in "Testing" mode issues refresh tokens that **expire after ~7 days**, which would mean
  logging in again almost every run. The saved token is also a security risk.

Manual upload costs ~10–15 min per batch and removes all of that. The API step could be added later
without changing the rest of the pipeline.

Each video gets a folder in `data/output/`:

```
data/output/2026-10-03_fall-of-constantinople/
  long.mp4
  long.srt
  short_01.mp4 … short_07.mp4
  short_01.srt … short_07.srt
  thumbnail.png
  upload.md      ← everything to paste into Studio
```

`upload.md` contains:
- the chosen title (you pick it from the candidates in `ogh review`);
- the description, with exact chapters (from real scene timings), sources, image credits (archive
  licenses, and AI-generated scenes marked as such) and music credits;
- tags;
- a Studio checklist: category *Education*, *not made for kids*, subtitle upload, thumbnail,
  suggested schedule (long-form Tue/Fri 15:00, Shorts daily 12:00, America/New_York), and the
  "altered or synthetic content" setting (AI narration voice; whether AI illustrations were used).

After uploading, run `ogh review` and mark the video **uploaded**. Analytics are checked in YouTube
Studio; there's no API.

---

## 8. Data & storage

`data/pipeline.db` is the SQLite database. Main tables:

| Table | Holds |
|---|---|
| `topic` | Candidates, gate result, relevance |
| `video` | Status, research, script, metadata, error |
| `scene` | Text, timing, image, pan/zoom |
| `short` | The 7 passages and their timings |
| `asset` | Image/music file, source, license, attribution |
| `llm_call` | Provider, model, step, tokens, outcome; used to track free-tier limits |
| `llm_cache` | Cached responses, so re-runs during development cost nothing |

Folders:
- `data/work/<video>/`: intermediate files (audio, timings, clips). **Deleted when the video is approved.**
- `data/output/<video>/`: upload kits. **Kept 30 days after you mark the video uploaded, then deleted.**
- `data/cache/`: downloaded images and API responses. Kept, so they can be reused.
- `data/logs/`: run logs.
- `assets/`: your curated files: `music/` (+ `manifest.yaml`), `fonts/`, `branding/`.

**Git-ignored:** `.env`, `data/` and `assets/`. `config.yaml` is committed because it holds no secrets.
Rendering only happens if at least 15 GB of disk is free.

---

## 9. Repo layout

**Rule: a file exists only if it has a job no other file does.** No helper scripts, example copies or
extra docs.

```
app/
  cli.py           the `ogh` command: run · review · doctor
  orchestrator.py  `ogh run`: one pass over every topic and video (outcome rules in §4.1)
  config.py        load + validate config.yaml and .env; data/ paths
  db.py            SQLAlchemy models + session
  llm.py           one interface over Gemini + Groq; routing + fallback per §5
  http.py          shared httpx client with retries
  wikipedia.py     Wikipedia action API: page descriptions, article text, links, references
  ffmpeg.py        FFmpeg/ffprobe commands + encoder choice, shared by assemble + Shorts
  subtitles.py     ASS/SRT building from word timings, shared by assemble + Shorts
  storage.py       data/ paths (work, output, cache) + atomic writes
  quota.py         daily usage counters; RetryLater / QuotaExceeded
  notify.py        optional failure alert to ALERT_URL
  status.py        the status vocabulary in §4.2
  stages/          one module per stage in §4.1, each with the same run(...) interface
prompts/           one template per LLM step in §5
tests/
config.yaml        channel + behaviour settings (committed)
.env.example       key names only (the real .env is git-ignored)
.gitignore
pyproject.toml     dependencies + the `ogh` entry point
uv.lock
README.md          this file, the only doc
```

**Scaling path:**
- a new stage is one file in `app/stages/` plus one status;
- a new LLM provider is one adapter in `llm.py`;
- a new image source is one function in the images stage;
- a second channel is a second config file.

---

## 10. Setup

```powershell
winget install --id astral-sh.uv -e
winget install --id Gyan.FFmpeg -e
# reopen PowerShell, then:
ffmpeg -hide_banner -encoders | Select-String qsv   # expect h264_qsv
uv sync
copy .env.example .env              # fill in keys from §3.3
uv run ogh doctor                   # everything green before the first run
```

Then:
1. Download music into `assets/music/` (§6.3) and a font into `assets/fonts/`.
2. Create the YouTube channel.
3. For the first few runs, watch every video fully in review before uploading.

---

## 11. Limits, risks & rejected options

### Risks

| Risk | Mitigation |
|---|---|
| Free-tier limits change or shrink | Model IDs in config; Groq fallback; the `llm_call` table shows usage; a limit only delays a video, never fails it |
| LLM states a false "fact" | Script written only from the articles; a second model checks every claim; you review it with the sources listed |
| The Wikipedia article itself is wrong | Accepted risk. Prefer well-covered topics; the description credits Wikipedia and its references. |
| Heavy topics handled flippantly (sensitive topics are allowed automatically) | Script prompt drops the cheeky tone for tragic subjects; human review of every video |
| An AI illustration looks like a fake historical photo | Vintage/illustrated style only, never photorealistic; credited as AI-generated; disclosed in Studio |
| The archive has no good image for a scene | Broader search, then AI, then reuse (§6.1) |
| Music copyright claim | YouTube Audio Library no-attribution tracks only |
| YouTube's "inauthentic / mass-produced content" policy | Real research, sourced facts, human review of every video; don't raise volume without raising quality |
| Laptop rendering is slow | Quick Sync hardware encoding; ~6 videos/month is little load; render while plugged in |
| Antivirus locking files mid-render | Add `data/` to Windows Security exclusions if renders fail randomly |

### Rejected: don't reopen without a new reason

| Option | Why not |
|---|---|
| Grok (xAI) API | Not free: a $25 one-time credit; monthly credits need $5 spend + data sharing |
| Gemini Search grounding | Not free for new accounts (tested 2026-09-26) |
| Web fact-checking Wikipedia | Redundant: the risk is the LLM, not the article |
| OpenRouter / Cerebras / Mistral as main LLM | Not needed at this volume; OpenRouter free is 50 req/day; Mistral free needs a training opt-in |
| Local LLM | Too slow on this CPU |
| AI images as the *primary* visual source | Archives first; AI is only a fallback (§6.1) |
| Photorealistic AI images | Could pass as fake historical photos |
| YouTube Data API upload + Analytics API | Private-only lock, audit, 7-day token expiry in Testing mode (§7) |
| Edge TTS | Unofficial service that could break; Kokoro is good and offline |
| Task Scheduler / background services | Manual runs chosen |
| NVENC / CUDA / `onnxruntime-gpu` on this branch | No NVIDIA GPU on this machine; Quick Sync + CPU instead |
| Node.js / n8n / Redis / Docker | Don't fit a sequential Python media pipeline |
| PostgreSQL / job queue | Overkill for one sequential local script |
| MoviePy / ffmpeg-python | Wrappers lag behind FFmpeg; call the CLI directly |
| Pixabay music | Occasional Content ID claims |

---

## 12. Decision log

| Date | Decision |
|---|---|
| 2026-09-26 | Constraints fixed: laptop CPU (no NVIDIA), $0, manual runs, history niche, ~6 long-form + ~42 Shorts/month. (From the OGH design.) |
| 2026-09-26 | Gemini 2.5 unavailable to new users, and grounding isn't free, so research = Wikipedia text only, the script is written only from it, and a different model checks the script against it. The separate web fact-check stage is removed. |
| 2026-09-26 | Voice: Kokoro, local. Music: YouTube Audio Library, no-attribution tracks. Upload: manual via Studio from a local upload kit; no YouTube API. |
| 2026-09-27 | `testing` branch rebuilt to this design, for this machine. `main` keeps the original design until the branch is reviewed and merged. |
| 2026-09-27 | Orchestrator: temporary errors (quota, rate limits) leave the status unchanged for the next run; one run moves each video as far as it can; datetimes stored as UTC. |
| 2026-09-27 | Topics come from trending Wikipedia (filtered to history) **and** "On this day". Rank = trend + relevance + not done before. One video in progress at a time. |
| 2026-09-27 | Sensitive subjects (atrocity, religion, disputed framing) are allowed automatically; only the auto-veto list blocks topics. |
| 2026-09-27 | Research adds up to 3 linked Wikipedia articles to the main one. LLM routing: Gemini + Groq, per §5. |
| 2026-09-27 | Images: archives first; broader terms; then an AI illustration in vintage/period style; then reuse. Scenes are 5–7 s with varied pan/zoom. |
| 2026-09-27 | Narration: `bm_george` + `bf_emma` for quotes; faster-whisper for real word timing; 3–4-word subtitles with word highlight. |
| 2026-09-27 | Shorts re-rendered vertically from the scenes (not cropped). Thumbnail = best image + LLM hook. |
| 2026-09-27 | `ogh review`: approve / reject / send back to a stage / pick title / mark uploaded. Cleanup: work files deleted on approval, kit 30 days after upload. |
| 2026-09-27 | Structure: `ogh` CLI, `app/stages/`, committed `config.yaml`, this README as the only doc. Python stays 3.11. |
| 2026-09-27 | LLM router: stages ask for a role (writer/checker/worker/image), never a model. If any model in the chain is only rate-limited the video waits for the next run rather than failing. Cache keyed by role, so fallback answers are reused. Local caps only for Groq (1,000 req / 200K tokens per UTC day); Gemini's own 429s do the rest. |
| 2026-09-27 | Topics: free description pre-filter before the gate; gate batched (20/call) and told history is broad (crimes, mysteries, people, inventions count if pre-2000) after a live run vetoed Lizzie Borden; pass needs relevance ≥ 6; passed topics expire after 7 days so the pool stays fresh; trend on a log scale so trending and anniversaries compete fairly. |
| 2026-09-27 | Content: linked articles picked from the most-mentioned links by the worker LLM (lead-section links were too generic). The script marks quotes `[QUOTE]` for the quote voice. The scene text split is deterministic Python (the LLM only writes image queries), so the checked narration can't drift; scenes stretch to at most 1.25× the max rather than leave a fragment under a second long. Tests can't reach the network (tests/conftest.py). |
| 2026-09-27 | `awaiting_review` merged into `packaged` (same meaning). Encoder output forced to limited-range 4:2:0 (`-color_range tv`): archival JPEGs are full-range and QSV otherwise tags output `yuvj420p`. |

---

## 13. Build status

| Step | Scope | Status |
|---|---|---|
| G1 | Retry-later orchestration, multi-stage runs, UTC datetimes | ✅ Done (some parts reworked in S1/S3) |
| S0 | This README | ✅ Done |
| S1 | Restructure: `ogh` CLI, `app/stages/`, committed `config.yaml`, remove old docs/scripts, YouTube API, analytics, OAuth; Quick Sync encoder; README statuses/tables | ✅ Done — the pipeline pauses at `scripted` until the check stage lands in S4 |
| S2 | LLM routing (Gemini + Groq), model IDs in config, `llm_call` table | ✅ Done — verified live against both providers |
| S3 | Discover (both sources), gate + relevance, rank with one video at a time | ✅ Done — verified live (1 Groq call gated 19 topics) |
| S4 | Research (article + linked), script, check, scene plan | ✅ Done — verified live on Lizzie Borden (~40 s, 4 LLM calls) |
| S5 | Images: archive chain → broader → AI → reuse, licenses | ⬜ |
| S6 | Narration (2 voices), Whisper timing, assembly (sync, subtitles, motion, music, Quick Sync) | ⬜ |
| S7 | Shorts, metadata, thumbnail, upload kit | ⬜ |
| S8 | `ogh review`, cleanup, `ogh doctor` | ⬜ |
