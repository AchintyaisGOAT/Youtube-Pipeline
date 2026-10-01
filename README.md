# PantherTellsHistory Pipeline: Design & Reference

The single reference for this repo. It covers what the pipeline is, every decision and the reason
for it, the stack, the full workflow, and setup. **Update this file when a decision changes.** Don't start
new docs. The decision log (§12) records what changed and when.

> **Status:** `main` is the only branch. The design was adapted from the OGH design (the channel
> was then called OurGreatHistory). It isn't fully built yet; §13 tracks what is.

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

- one **long-form video** (3–8 min, as long as its images can carry; 1920×1080),
- **3–7 Shorts** made from it (≤55 s each, 1080×1920),
- plus a thumbnail, subtitles and ready-to-paste metadata.

A human reviews every video, then uploads it to YouTube by hand.

- **Channel:** PantherTellsHistory (`@PantherTellsHistory`).
- **Niche:** world history and the history of science/technology, before 2000.
- **Tone:** energetic, fun, a little cheeky. Like a friend telling you the wildest true story they
  know: never a dry lecture, always accurate and sourced.
- **Topics are chosen by pictures:** the archives decide what can be shown, so they decide what
  gets made. Topics are measured by how many usable public-domain images exist for them, and made
  in **batches of 5** (`discovery.batch_size`).
- **Every picture shows what the narrator is talking about.** Images are gathered and checked
  first; the script is then written around them. No filler, no AI images.

```
discover → gate → survey (count free images) → rank (batch of 5) → per video:
  research (Wikipedia) → pictures (gather + check) → script written around the pictures
  → check against sources → scenes (cut at the image marks) → narration → word timing
  → assemble long-form → metadata → thumbnail → Shorts → upload kit
  → human review → manual upload to YouTube
```

---

## 2. Fixed constraints

These drive every choice below. If one changes, revisit the stack.

| Constraint | Value | Consequence |
|---|---|---|
| Machine | Laptop with an Intel Core Ultra 5 125H (14 cores), Intel Arc integrated graphics, 16 GB RAM | No CUDA, so local AI runs on CPU. Video encoding uses Intel Quick Sync (`h264_qsv`). No local LLM or local image generation (too slow). |
| Budget | **$0**: free tiers only | Every cloud service must have a permanent free tier. Rate limits are the constraint, not price. |
| Running | **Manual**: one command when you choose | No scheduler and no background service. Every run is safe to stop and restart. |
| Visuals | Public-domain / CC0 archive images only | Gathered and checked before the script, which is written around them (§6.1). No AI images. |
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
| Images (cloud) | Wikimedia Commons, Smithsonian Open Access, then a Gemini image model as fallback | Public domain / CC0 only for archive images. Smithsonian needs a (free) key. |
| Narration (local) | **Kokoro** via `kokoro-onnx` on CPU (`onnxruntime`) | Free, offline. |
| Word timing (local) | **faster-whisper** `small.en`, CPU, int8 | Real per-word timestamps for subtitle highlighting and Shorts cut points. |
| Video (local) | **FFmpeg**, called directly (no wrapper library) | Encoder `h264_qsv`, falling back to `libx264`. |
| Images/subs (local) | Pillow (thumbnails), pysubs2 (subtitles) | |
| CLI | One entry point, `ogh` (`app/cli.py`): `run` · `review` · `doctor` | Declared in `pyproject.toml`. No loose scripts. |
| Dev | ruff, pytest | |

### 3.2 External software (not pip)

`uv`, `Git` and `FFmpeg` (Gyan build), installed with `winget` (§10).
Model weights (Kokoro ~350 MB + Whisper `small.en` ~480 MB) download into `data/models/` on the first run.

### 3.3 Accounts & keys

| Key (`.env`) | Where to get it | Required? |
|---|---|---|
| `GEMINI_API_KEY` | aistudio.google.com | Yes |
| `GROQ_API_KEY` | console.groq.com | Yes |
| `WIKIMEDIA_CONTACT` | You choose it; it must be a **URL** (e.g. the channel or repo page) | Yes. Wikimedia returns 403 without it. |
| `SMITHSONIAN_API_KEY` | api.data.gov/signup | Optional (free): Smithsonian images |
| `EUROPEANA_API_KEY` | pro.europeana.eu/page/get-api | Optional (free): Europeana images |
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
| 1 | **Discover** topics. Runs when no passed topic is waiting; unused passed topics expire after 7 days. Three sources: the **catalog** (subjects linked from Wikipedia's history and true-crime list pages, `discovery.catalog_pages`, a date-seeded sample of 40), trending and "On this day". A free pre-filter on each article's short description drops meta pages, living people, anything dated 2000+, entertainment/sport, and (for the catalog) places, periods and concepts. | Cloud | Wikipedia list pages + Wikimedia Pageviews + "On this day" | Candidate topics |
| 2 | **Gate**: drop auto-veto and off-niche topics (§4.3), score relevance 0–10; below 6 is vetoed | Cloud | LLM (§5), up to 20 candidates per call | Passed / vetoed, with a relevance score |
| 3 | **Survey** the passed topics' free images: a quick count for all (the article's own images + Commons hits; under `discovery.min_images` = vetoed), then — only when the next batch needs topics — the full gather + check (step 6) for the `survey_top` most promising, plus last month's pageviews | Cloud | Archives (§6.1) + LLM check | Usable-image count + checked inventory per topic |
| 4 | **Rank**: when no video is being built and fewer than `max_waiting_review` wait on you, start a **batch** of `batch_size` videos from the best-scoring topics. Score = images ×2 (linear up to 60) + interest ×1 (pageviews, log) + relevance ×0.5 + trend ×0.5; under `min_images` never qualifies; titles never repeat | Local | Python | A batch of new videos |
| 5 | **Research**: fetch the topic's Wikipedia article plus up to 3 linked articles. Candidates are the article's most-mentioned links (surnames count), minus generic pages; the worker LLM picks the ones that add the most story | Cloud | Wikipedia API + LLM (§5, one small call) | Article texts + their references (the video's source list) |
| 6 | **Pictures**: the image inventory, before any writing (§6.1): the survey's checked images plus the linked articles' images, each checked by the LLM (keep only what shows something specific from the story, with a one-line "shows"), downloaded, measured, near-duplicates removed | Cloud | Archives + LLM (§5) | Numbered images with what each shows |
| 7 | **Script**, **only from the researched articles**, written **around the images**: every passage starts with `[IMG n]`, the image on screen while it's read. Length follows the images (~9 s each, each used at most twice, 3–8 min); ~1 `[SHORT]` passage per minute (3–7); every direct quotation marked `[QUOTE]`. A draft with the wrong length, Short count or image marks gets one retry | Cloud | LLM (§5) | Script |
| 8 | **Check**: every claim must appear in the articles. Unsupported claims are removed or rewritten, and each change is recorded for review. A change that breaks the markup or adds, drops or moves an image mark is rejected; a check that cuts over 40% of the words is treated as failed. | Cloud | A *different* LLM from the writer (§5) | Checked script + list of changes |
| 8b | **Scenes**: Python cuts the script at every image mark and `[SHORT]` edge, then into scenes of about 5–7 s (never splitting a name, keeping quotes whole), so the narration can't change. Each scene shows its passage's image; a long passage shows it again with another camera motion. The LLM plans sound effects and on-screen text (§6.5). | Local + Cloud | Python + LLM (§5) | Scene list with images |
| 9 | **Narration** | Local | Kokoro: `bm_george`, with `bf_emma` for quotes | WAV audio |
| 10 | **Word timing** | Local | faster-whisper | A timestamp for every word |
| 11 | **Assemble long-form**: pan/zoom on images, subtitles, music bed, loudness | Local | FFmpeg | `long.mp4` |
| 12 | **Metadata**: 5 titles ranked best first (the first is used), the description's hook + summary, tags, thumbnail hook + image, each Short's title/caption/description. Code adds the exact parts (chapters, sources, credits, footer) | Cloud | LLM (§5), one call | Metadata |
| 13 | **Thumbnail** | Local | Pillow template (§6.6) | `thumbnail.png` |
| 14 | **Render Shorts**: the marked passages, re-rendered vertically, each ending on the thumbnail card (§6.4) | Local | FFmpeg + Kokoro | `short_01.mp4` … `short_07.mp4` |
| 15 | **Upload kit** assembled (§7) | Local | Python | A folder in `data/output/` |
| 16 | **Review gate** | Local | `ogh review` | Approved / sent back / rejected |
| 17 | **Manual upload** in YouTube Studio, then marked uploaded in `ogh review` | You | YouTube Studio | Published video |

### 4.2 Statuses

Topics: `candidate → passed | vetoed → used`. The survey vetoes topics with too few free images;
a passed topic becomes a video when rank puts it in a batch.

Videos:

```
selected → researched → pictured → scripted → checked → segmented → narrated
  → aligned → assembled → shorts_ready → packaged → approved → uploaded
                                                  ↘ rejected
```

`packaged` means the upload kit is built and the video is waiting for `ogh review`. Plus
`failed`, with the error saved. `ogh review` can send a packaged or failed video back to redo any
stage: research, pictures, script, check, scenes, narration, timing, render, shorts or kit.
Everything that stage and the later ones made is cleared (`app/sendback.py`); downloaded images
and LLM answers are kept, so a redo only pays for what actually changes. Redoing the pictures
also redoes the script, whose image marks number the old inventory.

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
uv run ogh review    # packaged videos: approve / reject / send back to a stage / switch title
                     # failed videos: send back / reject;  approved videos: mark uploaded
uv run ogh assets    # after adding music/sound effects: write their manifest.yaml files
uv run ogh doctor    # health check: config, keys, DB, FFmpeg + encoder, disk, Wikimedia contact,
                     # voice/Whisper models + font, music licenses, today's usage per model
                     # (--live: every model ID exists + one tiny request per provider)
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
| Check | `gemini-3.5-flash` | — | Must differ from the writer; reads all the articles (~11K tokens), which Groq's 8K tokens/min can't fit. Chosen by test (§12) |
| Research: pick linked articles | Groq `openai/gpt-oss-120b` | `gemini-3.5-flash-lite` | Small judgement call |
| Scene plan (image queries) | Groq `openai/gpt-oss-120b` | `gemini-3.5-flash-lite` | Structured output |
| Metadata + thumbnail hook | `gemini-3.8-flash` | Groq `openai/gpt-oss-120b` | Titles and hooks benefit from the stronger writer |
| Image check (survey + pictures) | Groq `openai/gpt-oss-120b` | `gemini-3.5-flash-lite` | Reads titles/descriptions, keeps what shows the story; ~70 images per call |

**Why check at all:** Wikipedia is trusted. The risk is the LLM rewriting it: adding "facts" from its
own memory, inflating numbers, inventing quotes. The check compares the script with the articles,
not the articles with the web.

**Budget:** about 5 text-LLM calls per video (gate, script, check, scene plan, metadata), plus image
calls only for scenes the archives can't cover. At 6 videos a month this is far inside every limit.
When a limit is hit, the video just waits for the next run. Every call is logged in the `llm_call`
table.

**Staying free and never stalling on a limit:**
- Limits are **daily**, never monthly: Gemini's reset at **midnight Pacific** (12:30 PM IST, 1:30 PM
  in winter), Groq's per UTC day. `quota.py` counts each provider's own day.
- Gemini's per-model requests/day go in `llm.daily_requests` (copy the RPD column from
  aistudio.google.com/rate-limit); a capped model is skipped for the day without a request.
- A **402 "prepayment credits are depleted"** (seen 2026-09-30 on every model of a free-tier project)
  skips Gemini for the rest of the run and sends an alert; a daily-quota 429 is recognised at once
  instead of being retried for minutes. Either way the video **waits**, it never fails.
- Out of AI image quota mid-video: with `images.wait_for_ai_quota` (default) the scenes still
  without a picture wait for tomorrow's quota; off, they reuse images.
- `ogh doctor` shows today's usage per model against its cap; `--live` sends one tiny request per
  provider, the only way to see a refused project (402) or a spent quota.

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

**Only real archive images, and only ones that show something from the story.** All are public
domain / CC0, at least 600 px on the long side, with source, author and license saved and credited
in the upload kit. Real photographs of dead bodies and site furniture (logos, flags, seals, icons)
are never used. No AI images: every Gemini image model is 0 requests/day on the free tier
(AI Studio → Rate limit, 2026-09-30).

**Archives** (`app/archives.py`), all searched together:

| Archive | Key |
|---|---|
| The researched Wikipedia articles' own images | – |
| Wikimedia Commons | – |
| Openverse (Flickr Commons, museums, Commons… in one search) | – |
| The Met, Art Institute of Chicago, Cleveland Museum of Art | – |
| Smithsonian Open Access | `SMITHSONIAN_API_KEY` (free) |
| Europeana (public-domain mark / CC0 items only) | `EUROPEANA_API_KEY` (free) |

Not sources: the Library of Congress (its API blocks non-browser clients) and the US National
Archives catalog (needs a key, returns a web page without one).

**How a video gets its pictures** (steps 3 and 6):
1. **Gather:** each article's own images plus a search of every archive per article. A result must
   be about its query (a person's name whole: "John Wilkes Booth", not any Booth).
2. **Check:** one free LLM call reads every image's title and description and keeps only what
   shows a person, place, object, document or the event of this story, writing what each shows
   ("Ford's Theatre exterior, Washington, 1865"). Not someone who shares a name, not period filler.
3. **Download** the kept images, measure them, and drop near-duplicates (a tiny perceptual hash).
4. **The script is written around them** (step 7); each image is on screen while the narration
   talks about it, and returns at most twice (never back to back), with a new camera motion.

How many images a topic has varies ~50×: tested 2026-09-30, Lincoln's assassination 56, Jack the
Ripper 33, the Titanic 33, H. H. Holmes 30, Ted Bundy 20, Lizzie Borden 10. That's why topics are
chosen by it (step 3–4).

### 6.2 Narration

- Kokoro voices: `bm_george` narrates and `bf_emma` reads direct historical quotes. Verify the IDs
  against the kokoro-onnx voice list.
- ~2.6 words/second (measure once and update config); 400 ms pause between paragraphs.

### 6.3 Music

- **YouTube Audio Library only**, filtered to **"attribution not required"** tracks. They're free for
  YouTube use and safe from Content ID claims.
- Not Pixabay or similar: their tracks sometimes get claimed by uploaders who registered them with
  Content ID.
- The library has no API. Download 15–30 tracks once into `assets/music/`, then run
  `uv run ogh assets`: it writes `assets/music/manifest.yaml` (file, title, artist, license, credit)
  for you, taking title and artist from the file's tags. A new track's `license` starts **empty**,
  and a track with no license is never used: fill it in (e.g. "YouTube Audio Library — no
  attribution required"). Kevin MacLeod tracks (incompetech, CC BY 4.0) are recognised from their
  tags and get their license and required credit automatically.
- The description always names the track used; if its license asks for credit, the exact credit
  text is used.
- Rejected sources: Pixabay (below), and Fesliyan Studios' free tier, which forbids monetized
  videos and gets Content ID claims.
- Music sits at −18 LUFS under narration and is lowered further while speech plays. The final mix is
  −14 LUFS. If `assets/music/` is empty, videos render without music.

### 6.4 Video

| | Long-form | Shorts |
|---|---|---|
| Length | 3–8 min (fits the story) | ≤55 s each including the ending, 7 per video; a passage too long is cut at its last full sentence that fits |
| Resolution | 1920×1080, 30 fps | 1080×1920, 30 fps |
| Images | ~5–7 s per scene, varied slow pan + zoom (in/out, left/right, up/down; a reused image never repeats its last motion); portrait images sit on a blurred copy of themselves instead of being cropped | Re-rendered from the same scenes, not a crop of the long-form. **Every image is shown whole**: fitted in a box 92.5% of the frame's width, on a blurred, darkened copy of itself, with a slow zoom of at most 4% (so no edge ever leaves the screen). The caption and subtitles sit in the blurred bands above and below |
| Subtitles | Burned in, bottom-center, up to 4 words at a time (never across a sentence end or quote), current word highlighted (`#FFD23F`), quotes in italics, timed by Whisper word timestamps; 64 px | Same style, 84 px, plus the LLM's 2–6 word caption above the picture |
| Encoder | `h264_qsv`, fallback `libx264`, `yuv420p` pixel format (4:2:2 won't play everywhere) | Same |

Pictures, narration and subtitles share one timeline. Every scene covers its speech *and* the pause
after it, so images never drift out of sync with the voice.

Font: **Arial** (`subtitles.font_file`), its heaviest installed weight (Arial Black) for thumbnails.
Colour: `#FFD23F` for highlights, frames and accents. No logo anywhere: YouTube shows the
channel's picture next to every video and Short.

**The end of every Short** (~4 s): the video's thumbnail in the middle of the screen in a glowing
`#FFD23F` frame, "WATCH THE FULL STORY" above it, while the narrator says one line sending the
viewer to the full video, e.g. "Want the whole story? Tap the video linked below." Four lines
rotate so the Shorts don't all end alike. "Linked below" is literal: in Studio each Short gets the
long-form as its **Related video**, which YouTube shows as a tappable link under the Short.

### 6.5 Sound effects & on-screen text

Both are planned in the same scene-plan LLM call as the image queries (§4.1 step 7), then
checked by code before anything is rendered.

**Sound effects** come from your own library, the same routine as music:
1. In YouTube Studio → Audio Library → **Sound effects**, download 30–50 effects (whooshes and
   impacts matter most; add ambience like crowd, bells, thunder, typewriter, paper, footsteps).
2. Put them in `assets/sfx/` and run `uv run ogh assets`. It writes `assets/sfx/manifest.yaml`,
   guessing each effect's tag from its file name ("Whoosh Swish.mp3" → `whoosh`) and listing any it
   couldn't tag, for you to fill in. Tags: whoosh, impact, riser, crowd, typewriter, clock, thunder,
   rain, bell, door, footsteps, paper, camera, gunshot, horse, fire, water, wind, train, sword.
3. The scene plan may only use tags your library has; ambient effects (bell, crowd, thunder,
   door, paper…) only where the scene's narration mentions that thing. Every chapter card also
   gets a whoosh and every number callout an impact. At most 30% of scenes get an effect, never
   two in a row. Each plays as its scene starts (a whoosh a beat early, so it lands on the cut),
   trimmed to the scene and faded, at −8 dB under the narration (`effects.*` in config).

**On-screen text**, in the upper part of the frame, clear of the subtitles, fading in and out:
- **label**: a new place or date, top-left on a dark box, e.g. "FALL RIVER, MASSACHUSETTS · 1892".
- **number**: one striking figure, large, top-centre, in the highlight colour, e.g. "$300,000".
- **chapter**: a 2–5 word title card at the start of each part of the story, e.g. "THE TRIAL" (max 6).

At most 25% of scenes get a label or number. **No overlay may state what the video can't back
up:** every number must appear in the scene's narration or the researched articles, and every word
of a label in the narration, the articles or the title. Anything else is dropped.

### 6.6 Thumbnail

- The most striking image from the video: the LLM picks it from the video's images (archive images
  before AI ones, wide and large first), filled edge to edge and darkened toward the text.
- A 2–4 word hook written by the LLM that adds to the title rather than repeating it, in Arial
  Black, white with a black outline, one word in `#FFD23F`, bottom-left, as large as fits in two lines.
- A thin `#FFD23F` frame. No logo (see §6.4).
- One thumbnail per video; sending the video back to the thumbnail stage makes a new one. It is
  made before the Shorts, which end on it.

---

## 7. Output: the upload kit

**Why manual upload** (decided 2026-09-26):
- An unverified Google Cloud project can only upload *Private* videos, so you'd open Studio anyway.
- Lifting that restriction needs a compliance audit.
- An OAuth app in "Testing" mode issues refresh tokens that **expire after ~7 days**, which would mean
  logging in again almost every run. The saved token is also a security risk.

Manual upload costs ~10–15 min per batch and removes all of that. The API step could be added later
without changing the rest of the pipeline.

Each video gets a folder in `data/output/`, named by its suggested publish date. The renders are
moved into it (not copied), so nothing is stored twice:

```
data/output/2026-10-02_fall-of-constantinople/
  long.mp4
  long.srt
  short_01.mp4 … short_07.mp4
  short_01.srt … short_07.srt
  thumbnail.png
  upload.md      ← everything to paste into Studio, step by step
  credits.md     ← full source + image-credit list
```

`upload.md` contains, in upload order:
- **Long-form:** the title (the best-ranked of 5; the others are listed, and `ogh review` can
  switch), the description, thumbnail, audience (*not made for kids*), the "altered or synthetic
  content" answer with the reason (AI narrator voice; how many AI illustrations), tags, category,
  subtitle upload, and the scheduled time.
- **Each Short:** its file, title, description, the **Related video** to pick (the long-form), and
  its scheduled time.

**Suggested schedule** (`publish.schedule`, times in `timezone`): the long-form takes the next free
Tue/Fri 15:00 slot after every other packaged video's; its Shorts follow at 12:00 daily, starting
after the long-form is out and after the Shorts already queued. Re-packaging keeps the dates.

**Description layout** (built by code around the LLM's hook and summary; follows current YouTube
guidance: only ~150 characters show before "more", chapters need 3+ entries from 0:00, the first 3
hashtags show above the title, 5,000 characters max):

```
<hook: 1–2 lines naming the topic>

<summary: 2–3 sentences with the key people, places, dates>

▶ Subscribe for more true stories: https://www.youtube.com/@PantherTellsHistory?sub_confirmation=1

⏱ Chapters            ← from the chapter cards' real times; left out if fewer than 3
📚 Sources (Wikipedia) ← the researched articles
🖼 Image credits       ← one line per archive image; one summary line if the list would pass 5,000
🎵 Music               ← only if a track needs credit

<publish.description_footer: comment prompt, upload schedule, how the video is made, corrections>

#<Topic> #History #PantherTellsHistory
```

A Short's description is its 1–2 sentence tease, `publish.shorts_footer` and
`#<Topic> #History #PantherTellsHistory #Shorts`.

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
- `data/work/<video>/`: intermediate files (audio, timings, clips; ~400 MB). **Deleted when the
  video is approved** (or rejected).
- `data/output/<date>_<topic>/`: upload kits. **Deleted `ops.keep_output_days` (30) after you mark
  the video uploaded** (or reject it).
- `data/cache/`: downloaded images. Kept for reuse; images no scene uses and unused LLM answers are
  deleted after `ops.keep_cache_days` (90); files no record points to are deleted at once.
- `data/models/`: Kokoro and Whisper weights, downloaded once.
- `data/logs/run_<date>.log`: every `ogh run`, kept 30 days.
- `assets/`: your curated files: `music/` and `sfx/`, each with its `manifest.yaml`.

Cleanup (`app/cleanup.py`) runs at the start of every `ogh run`.

**Git-ignored:** `.env`, `data/`, and the audio in `assets/` — each `assets/*/manifest.yaml` **is**
committed, because it holds every track's license and required credit. `config.yaml` is committed
because it holds no secrets.
Rendering only happens if at least 15 GB of disk is free.

---

## 9. Repo layout

**Rule: a file exists only if it has a job no other file does.** No helper scripts, example copies or
extra docs.

```
app/
  cli.py           the `ogh` command: run · review · doctor · assets
  orchestrator.py  `ogh run`: one pass over every topic and video (outcome rules in §4.1)
  config.py        load + validate config.yaml and .env; data/ paths
  db.py            SQLAlchemy models + session
  llm.py           one interface over Gemini + Groq; routing + fallback per §5
  http.py          shared httpx client with retries
  wikipedia.py     Wikipedia action API: page descriptions, article text, links, references
  ffmpeg.py        FFmpeg/ffprobe commands + encoder choice, shared by assemble + Shorts
  subtitles.py     ASS/SRT building from word timings, shared by assemble + Shorts
  storage.py       data/ paths (work, output, cache, models) + atomic writes
  quota.py         daily caps per provider/model; RetryLater / QuotaExceeded / ProviderBlocked
  archives.py      the public-domain image archives (§6.1), one search function each
  sendback.py      send a video back to a stage, clearing what later stages made (§4.2)
  cleanup.py       deletes work files, old kits, unused cache (§8); runs with every `ogh run`
  notify.py        optional failure alert to ALERT_URL
  status.py        the status vocabulary in §4.2
  assets.py        music + sound-effect manifests (`ogh assets`), track/effect picking
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
- a new image source is one function in `archives.py`;
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
1. Download music into `assets/music/` (§6.3) and sound effects into `assets/sfx/` (§6.5), then
   run `uv run ogh assets`.
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
| An archive image shows the wrong person or place (shared names) | Names must match whole; the LLM image check reads each description; you review every video |
| A topic has too few images | Topics are chosen by their image count (survey + rank); the script's length follows the images, so a thin topic makes a shorter video instead of a padded one |
| Music copyright claim | Licensed tracks only (YouTube Audio Library, Kevin MacLeod CC BY with credit); a track with no license is never used |
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
| Library of Congress API | Since 2026-09 it answers every non-browser client with a Cloudflare challenge (403). Getting past it would mean evading bot protection. Much of its public-domain photography is on Commons. |
| Photorealistic AI images | Could pass as fake historical photos |
| YouTube Data API upload + Analytics API | Private-only lock, audit, 7-day token expiry in Testing mode (§7) |
| Edge TTS | Unofficial service that could break; Kokoro is good and offline |
| Task Scheduler / background services | Manual runs chosen |
| NVENC / CUDA / `onnxruntime-gpu` on this branch | No NVIDIA GPU on this machine; Quick Sync + CPU instead |
| Node.js / n8n / Redis / Docker | Don't fit a sequential Python media pipeline |
| PostgreSQL / job queue | Overkill for one sequential local script |
| MoviePy / ffmpeg-python | Wrappers lag behind FFmpeg; call the CLI directly |
| Pixabay music | Occasional Content ID claims |
| Switching to horror / movie-recap / sports-recap niches for more images | Their stills and footage are copyrighted (Content ID, strikes); public-domain imagery is overwhelmingly historical (2026-09-30) |
| Gemini image models on the free tier | 0 requests/day for every image model (AI Studio rate-limit page, 2026-09-30) |
| US National Archives catalog API | Needs a key; returns a web page without one. Much of it is on Commons. |

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
| 2026-09-27 | Check returns only the sentences it changes ({original, replacement, reason}); code applies them, so nothing changes unrecorded, and edits that can't be found, change nothing, or break markup are rejected. Checker model chosen by test: 4 false facts planted in a real script (wrong year, inflated number, wrong person, invented sentence). 3.5-flash-lite, 3.5-flash and 3.7-flash all caught 4/4, but only **3.5-flash** also caught the writer's real embellishments (e.g. "Lizzie bought a mansion" when Wikipedia says the sisters moved in), verified against the article. Trade-off: stricter edits, slightly flatter tone, ~50 s per check. |
| 2026-09-27 | Images: Library of Congress dropped (Cloudflare 403). Live runs led to a title relevance check (Commons search matched theatre plans to "Andrew Borden on sofa"), exact-phrase topic queries, a 600 px floor (800 lost the real period portraits), a graphic filter (a murder victim's crime-scene photo was picked), and a cap of 20 AI illustrations per video to protect the image quota. The AI style asks for one full-frame scene with no border, panels or text. |
| 2026-09-27 | Narration is synthesised per scene and each scene's time is measured from the audio, pause included, so the pictures can't drift from the voice; Emma reads `[QUOTE]` text. Whisper only places words inside a scene (the script's own words are displayed), so a mismatch can't spread. The subtitle canvas matches the video resolution, so sizes are real pixels. Clips render in parallel, then one final pass does subtitles + music ducking + loudness. |
| 2026-09-27 | After the first render felt flat: sound effects (from your YouTube Audio Library downloads, tagged via `ogh assets`) and on-screen text (place/date labels, number callouts, chapter cards), planned in the existing scene-plan call. Code enforces sparsity (effects ≤ 30% of scenes, never consecutive; labels/numbers ≤ 25%; ≤ 6 chapters) and rejects any overlay whose numbers or names the narration/sources don't contain. Scene length stays 5–7 s. |
| 2026-09-27 | `awaiting_review` merged into `packaged` (same meaning). Encoder output forced to limited-range 4:2:0 (`-color_range tv`): archival JPEGs are full-range and QSV otherwise tags output `yuvj420p`. |
| 2026-09-28 | `testing` merged into `main` (PR #1) and deleted. From 2026-09-30, `main` is the only branch; all work happens on it. |
| 2026-09-30 | Channel renamed OurGreatHistory → **PantherTellsHistory** (`@PantherTellsHistory`) to match the YouTube account. The `ogh` command keeps its name. |
| 2026-09-30 | Images, after the S7 test run showed the wrong man (Sir George Robinson for lawyer George D. Robinson), a relative for Andrew Borden, letters as filler and 41 of 67 scenes reusing images in blind rotation: results must match every name in the scene query (initials included), letters/records only when asked for, topic-level results must share a word with the scene, reuse picks the best-fitting image, and the AI cap is 70, so every scene can have its own picture (the image model has its own quota; past it, scenes reuse). Music: a track with no license is never used (a default once mislabelled a CC BY track), and the description always names the track. |
| 2026-09-30 | Bundle A (free tier, never stall): Gemini's 402 blocks Gemini for the run and alerts; daily-quota 429s aren't retried; per-model daily caps in config, counted from midnight Pacific; the checker/writer then wait instead of failing the video; images wait for tomorrow's AI quota instead of repeating pictures; `doctor` shows usage and probes both providers. Groq's free limits confirmed from its headers: 1,000 requests/day, 8K tokens/min. |
| 2026-09-30 | Bundle B: `ogh review` sends a packaged or failed video back to any stage (images: chosen scenes only), clearing exactly what later stages made — no more hand-editing the database. |
| 2026-09-30 | Bundle C: images come from a pooled search (Wikipedia article images + Commons, Openverse, Met, Art Institute of Chicago, Cleveland, Smithsonian, Europeana) ranked per scene by one Groq call; an image may serve 3 scenes, never back to back; violent images are never reused on calm scenes; AI style forbids gore. AI illustrations off by default: every Gemini image model is 0/day on the free tier. Tested on Lizzie Borden: ~15–20 relevant archive images exist for 67 scenes. |
| 2026-09-30 | Bundle D: the script must fit the 3–8 min range and have exactly 7 non-touching Short passages, else one retry with the problem named, then fail. Shorts end at a scene edge, never mid-word. Gate accepts relevance sent as text. |
| 2026-09-30 | Bundles E–G: downloads retry only real network errors (not 404/403); `ogh run` keeps Windows awake and logs to `data/logs/`; `ogh review` switches titles and marks uploads; cleanup of work files, kits, cache and stray files; `doctor` checks media, music licenses and the Wikimedia contact; asset manifests committed; lint at 120 columns and green. |
| 2026-09-30 | **Bundle H — pictures decide.** Topics are chosen by how many usable free images they have (catalog source from Wikipedia's history/true-crime lists; survey; batches of 5; score images ×2 + interest + relevance + trend). Each video's images are gathered and checked *before* the script, which is written around them with `[IMG n]` marks; its length follows the images (3–8 min, 3–7 Shorts). Removed: per-scene image search, pool ranking, period filler, blind reuse, AI illustrations (0/day free). Measured: images per topic vary ~50× (Lincoln 56 … Lizzie Borden 10). |
| 2026-09-30 | Images decision: stay in the history niche (public-domain imagery is overwhelmingly historical; recap genres use copyrighted stills). Next: search many free archives at once and rank the pool (bundle C). |
| 2026-09-30 | S7. **Order:** metadata → thumbnail → Shorts (the Shorts end on the thumbnail and use the metadata's captions), all in the Shorts stage; the package stage builds the kit. **Shorts** show every image whole (fitted box on its own blurred copy, zoom ≤ 4%) instead of panning a crop; each ends on a ~4 s thumbnail card with a spoken line pointing to the Related-video link, so passages are now 15–45 s. **Metadata:** one call; the best of 5 titles is picked automatically; code, not the LLM, writes chapters (real times), sources, credits, the fixed footer and hashtags. **Thumbnail:** no logo (the channel picture already shows), Arial Black, `#FFD23F` accent word + frame. **Kit:** files moved, not copied; dated by the suggested publish slot. **Cleanup:** tests now write only to a temp folder (they had left 150 empty folders in `data/output/`). |

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
| S5 | Images: archive chain → broader → AI → reuse, licenses | ✅ Done — verified live on Lizzie Borden (55 scenes in ~50 s) |
| S6 | Narration (2 voices), Whisper timing, assembly (sync, subtitles, motion, music, Quick Sync) | ✅ Done — verified live: 7:54 Lizzie Borden video; narration 2.7 min, timing ~9 min (incl. one-time model download), render 92 s; −14.5 LUFS, locked sync |
| S6b | Sound effects + on-screen text (labels, numbers, chapter cards), `ogh assets` | ✅ Done — see §12 |
| S7 | Shorts, metadata, thumbnail, upload kit | ✅ Done — see §12 (2026-09-30) |
| S8 | `ogh review` (send back, title, uploaded), cleanup, `data/logs/`, `ogh doctor` | ✅ Done — bundles A–G (§12, 2026-09-30) |
| H | Image-led topics and scripts: catalog source, survey, batches of 5, pictures before the script, `[IMG n]` marks, no AI images | ✅ Built and tested offline; live up to the script (Gemini's 402 blocks writing until a new free-tier project key) |
