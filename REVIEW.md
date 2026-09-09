# Architecture Review — Risks, Problems & Improvements

Full pass over the pipeline as specified in [STACK.md](STACK.md), [INPUTS.md](INPUTS.md) and
your notes. Every item is tagged:

- **[P0]** — fix or have a plan before the first *public* upload. Legal, account, data-loss, or
  correctness landmines.
- **[P1]** — address before scaling past ~10 videos or turning on full unattended scheduling.
- **[P2]** — quality / polish / efficiency; do when the core is stable.

Numbers are stable IDs referenced from the other docs.

---

## Priority checklist (start here)

**Before the first public upload — [P0]:**
- [ ] #1 Read YouTube's inauthentic-content / spam policy; make the human-review step a real
      editorial gate, not a rubber stamp
- [ ] #2 Wire the "altered or synthetic content" disclosure flag into the upload call
- [ ] #3 Fact-check step + reviewer actually verifies on-screen claims against sources
- [ ] #4 US-public-domain-only image filter; store the exact license tag per image
- [ ] #5 Music/SFX restricted to YouTube Audio Library + verified CC0
- [ ] #8 Split Google accounts (channel vs Cloud/Gemini); 2FA on both
- [ ] #9 / #42 Automated nightly `pg_dump` + media copy to a *different drive*; test a restore
- [ ] #11 Failure notifications (apprise → email/Telegram/ntfy) + a pipeline heartbeat
- [ ] #19 `token.json` excluded from every backup / cloud-synced folder; minimum OAuth scopes
- [ ] #23 Media retention job + free-disk precheck before each render
- [ ] #30 Long-path reg key + antivirus exclusions for `work/ output/ data/`
- [ ] #32 `--dry-run` and `--visibility unlisted` modes; first 5–10 videos unlisted
- [ ] #44 Topic blocklist + a Gemini sensitivity-check gate before scripting
- [ ] #45 On-screen sources, description source block, "educational commentary" framing

**Before scaling / full auto — [P1]:** #10, #12, #13, #14, #15, #16, #17, #18, #20, #21, #22,
#24, #26, #27, #28, #29, #31, #33, #34, #37, #38, #40, #43

---

## A. Legal, policy & account risk

### #1 — YouTube "inauthentic / repetitious content" policy  **[P0]**
Fully automated, AI-narrated, stock-image explainers produced on a schedule are exactly what
YouTube's 2025 inauthentic-content and spam policies target. Consequences range from
demonetization to channel termination.
**Mitigation:** the human review gate must add real editorial judgement (rewrite weak
sections, cut filler, verify facts, reject generic output); each video needs a distinct angle
and genuine information value; never publish near-duplicate videos; keep upload cadence
human-plausible. Treat "6 long-form/month" as a ceiling, not a quota to force.

### #2 — Mandatory AI-content disclosure  **[P0]**
An AI/synthetic voice must be disclosed in YouTube Studio ("altered or synthetic content").
**Mitigation:** the upload step always sets this flag; add a one-line "narration is
AI-generated" note in the description template.

### #3 — LLM factual errors on a history channel  **[P0]**
Gemini will occasionally invent dates, misattribute quotes, or blur events. A history channel
lives or dies on accuracy, and errors on sensitive topics invite misinformation strikes.
Grounded fact-checking reduces but does not eliminate this.
**Mitigation:** `factcheck.md` verifies every claim with Google Search grounding and returns a
per-claim verdict + source URL; claims that can't be sourced are cut or softened; on-screen
citations + a description source list; the reviewer spot-checks the riskiest claims; avoid
contested history entirely (see #44).

### #4 — Image licensing is not as clean as "open access" implies  **[P0]**
"Public domain in the US" ≠ PD worldwide. Wikimedia PD tags are sometimes wrong. LoC's "no
known copyright restrictions" is *not* a PD guarantee. Photographs of in-copyright 3-D works,
and mid-20th-century photos, are grey areas.
**Mitigation:** filter to explicit US-PD / CC0 tags only; persist `source`, `source_id`, the
exact `license` string, and `rights_url` on every `asset`; keep the review step's
image-approval checkbox; prefer Met + Wikimedia PD-Art + Smithsonian CC0 over ambiguous tags.

### #5 — "Royalty-free" music/SFX is a Content ID trap  **[P0]**
Random "royalty free" downloads frequently carry Content ID claims.
**Mitigation:** `assets/music/` = **YouTube Audio Library** exports (claim-free by
definition) and verified **CC0** only. Store the license + required attribution per track;
the description builder appends CC-BY credits automatically. No exceptions to the source list.

### #6 — Local model licenses & training-data provenance  **[P1]**
Confirm Kokoro's license permits commercial use and that its voice models aren't clones of
identifiable real people; same check for the Whisper model. Record the model + license +
revision hash you settle on.

### #7 — pytrends violates Google's ToS  **[P2]**
Low risk at this volume, but it's technically scraping. Already marked non-critical and
failure-tolerant; the primary discovery signal is the official Wikimedia Pageviews API (#26).

### #8 — One Google account = total single point of failure  **[P0]**
If the account holding the channel also holds the Cloud project + Gemini key and it gets
flagged, you lose everything at once.
**Mitigation:** channel on account A, Cloud project + Gemini on account B; 2FA + recovery
phone/email on both; keep an offline copy of `client_secret.json`.

---

## B. Data safety & reliability

### #9 — Single machine, single disk  **[P0]**
Code is on GitHub, but the database and all media sit on one Windows disk. A disk failure or
ransomware event loses the DB and every master.
**Mitigation:** nightly `pg_dump -Fc` + `rclone`/robocopy of `output/` masters to a second
physical drive **and/or** a free cloud bucket; document and test the restore (#42).

### #10 — No dead-letter handling for poison jobs  **[P1]**
procrastinate retries with backoff, then the job just sits failed. A permanently failing
video (bad topic, API change) silently stops the line.
**Mitigation:** on max-retry, set `video.status = 'failed'` with the error, fire an alert
(#11), and surface failed videos on the dashboard for one-click retry/skip. A weekly
"nothing published in N days" check.

### #11 — Unattended failures are invisible  **[P0]**
"Fully scheduled" means a 3 a.m. worker crash or an all-day Gemini outage is discovered only
when you notice no new videos.
**Mitigation:** `apprise` (`ALERT_URL`) sends failures to email / Telegram / ntfy;
a heartbeat row (`last_successful_discovery`, `last_successful_publish`) checked by a periodic
task that alerts if stale; the dashboard `/status` page (#40).

### #12 — FFmpeg is the fragile heart and its errors are opaque  **[P1]**
One mega filtergraph (Ken Burns + xfade + subtitle burn + audio mix + loudnorm) fails with
unhelpful messages and is near-impossible to debug.
**Mitigation:** render in validated stages with intermediate files —
`slideshow.mp4` → `+narration` → `+music(ducked)` → `+subs` → `final` — each checked with
`ffprobe` (duration within tolerance, has video+audio, right resolution, loudness in range).
Pin the exact FFmpeg build; record its version on each `render` row.

---

## C. Media pipeline correctness

### #13 — The "Ken Burns on stills" look reads as low-effort  **[P1]**
170 pan/zoom stills for 7 minutes is the visual signature of channels YouTube is
down-ranking, and it hurts retention.
**Mitigation:** vary the visual grammar — full frames, detail crops, two-up comparisons,
animated maps, date/timeline motion graphics, and **public-domain moving footage** (Library
of Congress moving image, Prelinger / archive.org, NASA) interleaved with stills; a subtle
consistent grade (grain + vignette) for cohesion.

### #14 — Whisper drifts on exactly what history scripts contain  **[P1]**
Numbers, years, and proper nouns are where free transcription mis-segments — and the script
is full of them. But we already *know* the exact words.
**Mitigation:** treat it as forced alignment, not transcription: pass the known text as
`initial_prompt`/`hotwords`; after alignment, compare recognized-word count to script-word
count and, if the mismatch exceeds a threshold, fall back to proportional per-sentence
timing. Consider `stable-ts` (keeps the faster-whisper backend) for tighter word boundaries.

### #15 — Short/segment lengths only exist after TTS  **[P1]**
The script marks a `[SHORT]` span, but its real duration is unknown until Kokoro renders it —
a "≤55 s" Short can land at 62 s.
**Mitigation:** measure Kokoro's words/sec once and have the script engine target calibrated
word counts per span; after TTS, measure actual durations and hard-gate — auto-trim to a
sentence boundary or regenerate the span. Never publish a Short over the platform limit.

### #16 — Upload retries can double-publish  **[P1]**
If the upload succeeds but the worker dies before writing `youtube_id`, the retry uploads
again.
**Mitigation:** write an `upload` row in state `uploading` *before* the call with a
deterministic client token; on retry, first query the channel for a video carrying that token
(embed a UUID in the description or use `youtube.search`); only upload if absent. procrastinate
task `lock` to serialize uploads per channel.

---

## D. Config, schema & ops mechanics

### #17 — Two config sources will drift  **[P1]**
`config.yaml` and the `channel` DB row can disagree.
**Mitigation:** `config.yaml` is the only input; `scripts/load_config.py` writes it into the
`channel` row with an incrementing `config_version`; the app reads config **only** from the
DB; the dashboard shows the active version.

### #18 — Alembic and procrastinate both own schema  **[P1]**
`alembic upgrade` autogenerate can try to "clean up" `procrastinate_*` tables.
**Mitigation:** apply procrastinate's schema separately (`procrastinate schema --apply`); tell
Alembic to ignore those tables (`include_object` filter in `env.py`); never autogenerate
across them. Consider putting queue tables in their own Postgres schema.

### #19 — Plaintext secrets, and `token.json` is account-takeover-grade  **[P0]**
The refresh token grants upload rights to your channel.
**Mitigation:** minimum OAuth scopes (`youtube.upload`, `youtube.readonly`,
`yt-analytics.readonly` — not full `youtube`); `token.json` and `.env` excluded from every
backup set and never in a cloud-synced folder; optionally DPAPI-encrypt `token.json` at rest.

### #20 — In-process rate limiter breaks with a second worker  **[P1]**
`aiolimiter` is per-process; two workers can jointly exceed Gemini/YouTube limits.
**Mitigation:** keep to one worker until genuinely needed; when adding a second, move the
limiter to a Postgres-backed token bucket (a tiny `rate_bucket` table with `SELECT … FOR
UPDATE`) — documented trigger, small change.

---

## E. External API constraints

### #21 — No HTTP cache / politeness on archive APIs  **[P1]**
Re-running research re-hammers Wikimedia et al. Wikimedia **requires** a descriptive
`User-Agent` with contact info or it returns 403.
**Mitigation:** `hishel` on-disk cache in front of `httpx`; set `User-Agent` from
`WIKIMEDIA_CONTACT`; per-host concurrency caps and polite backoff; cache image search results
on the `candidate`/`segment` so a re-render doesn't re-fetch.

### #22 — Smithsonian / api.data.gov = 1000 requests/hour, shared  **[P1]**
A bad run can exhaust it.
**Mitigation:** treat Smithsonian as optional (like pytrends); exponential backoff on 429;
Wikimedia + Met + LoC carry the load.

### #23 — Unbounded disk growth  **[P0]**
Per long-form ≈ 2–5 GB with intermediates; nothing deletes it.
**Mitigation:** delete intermediates immediately after a validated render; keep finals +
Shorts N days (config) then move masters to the backup target and prune locally; a
free-space precheck before every render that alerts and pauses instead of failing mid-encode.

---

## F. Discovery, metadata & the feedback loop

### #26 — "mostPopular / Education" is the wrong discovery signal for this niche  **[P1]**
YouTube's mostPopular surfaces broad viral content, not history sub-niches.
**Mitigation:** primary signal = **Wikimedia Pageviews API** (official, free) — trending and
"most viewed" articles, plus "on this day" anniversaries; secondary = Reddit r/history /
r/AskHistorians top, "year in search", pytrends rising (best-effort). Gemini ranks candidates
against the channel's scope + past performance (#28).

### #27 — LLM usage creep, and grounding has a hard free cap  **[P1]**
Research + fact-check + outline + draft + revise + metadata is ~8–12 calls/video, and
grounded fact-checks drop to paid after a small daily free allotment.
**Mitigation:** extend `api_quota_ledger` to Gemini (tokens + grounded-call counts per
video); batch claims into fewer grounded calls; cache research per topic; alert at 80 % of
any daily budget.

### #28 — Analytics pulled but never used  **[P1]**
Collecting retention data is pointless without a loop.
**Mitigation:** a `topic_performance` table keyed by topic/tag/length/thumbnail-style; a
monthly Gemini "what worked / what didn't" summary that updates the `trend_rank.md` context;
the ranker demotes patterns that underperform and favors proven ones (#46).

### #38 — Metadata quality  **[P1]**
Generic titles/tags/descriptions cap reach.
**Mitigation:** generate 3–5 title candidates scored on length, curiosity gap, front-loaded
keyword; description with chapter timestamps + source list + attributions; tags derived from
an actual search signal, not guesses; hold the final title choice for the reviewer.

---

## G. Product & quality improvements

### #24 — No thumbnail generation  **[P1]**
CTR is decided by the thumbnail; there's no plan for one.
**Mitigation:** a Pillow template system — key image cut-out + 3–4 word headline + consistent
brand frame; produce 2 variants per video for YouTube's built-in Test & Compare (#39).

### #25 — No chapters / cards / end screens  **[P2]**
Chapters can be set from the script's segment boundaries via the description. Cards can be set
via API. End screens **cannot** be set via API — flag as a manual review-step task.

### #29 — A media pipeline is hard to test  **[P1]**
**Mitigation:** golden-file tests for SRT/ASS generation, quota-ledger math, and Shorts
cut-point math; a 10-second synthetic end-to-end render in CI; `ffprobe` assertions on every
real output (duration, has audio, resolution, integrated loudness).

### #30 — Windows-specific fragility  **[P0]**
Long-path limit, antivirus locking files mid-render, service-account permissions.
**Mitigation:** enable Win32 long paths; AV exclusions for `work/ output/ data/`; run NSSM
services as your user; avoid spaces/unicode in generated filenames.

### #31 — Worker shutdown mid-render  **[P1]**
Stopping the service during an encode can leave a corrupt file and a half-done job.
**Mitigation:** write to `*.tmp` and atomic-rename on success; checkpoint stage completion in
the DB; procrastinate `graceful` shutdown; on startup, sweep and reset `*.tmp` + jobs stuck
`in_progress`.

### #32 — No staging mode  **[P0]**
First real run would hit the live channel.
**Mitigation:** `--dry-run` (everything but upload) and `--visibility unlisted`; a config
`test_mode` that also skips the AI-disclosure-required public flags; first 5–10 videos
unlisted for review.

### #33 — Single-pass scripts are generic  **[P1]**
**Mitigation:** outline → draft → self-critique → revise chain; a house style guide in the
prompt; hard "hook in the first 15 seconds" requirement; retention-shaped structure (open a
loop early, pace the payoffs); a max-cliché / max-filler check.

### #34 — 7 minutes of one flat TTS voice tires listeners  **[P1]**
**Mitigation:** vary pace at section breaks; 300–500 ms pauses at paragraph boundaries (you
control Kokoro's chunking); optional second voice for quoted material; light compression +
de-ess on the narration bus.

### #35 — Static music level  **[P2]**
**Mitigation:** sidechain-duck music under narration in FFmpeg (`sidechaincompress`); change
track at chapter boundaries; -16 to -20 LUFS music bed under a -14 LUFS master.

### #36 — Subtitles: do both  **[P2]**
Burn styled captions **and** upload a sidecar `.srt` via the API (accessibility + SEO).

### #37 — Seven near-identical Shorts can trip "repetitious content" and cannibalize  **[P1]**
**Mitigation:** each Short gets its own hook line, its own vertical composition (not just a
center-crop), unique caption/title/description; space them over days, not all at once; skip
Shorts for segments that don't stand alone.

### #39 — Thumbnail A/B  **[P2]**
Ship two thumbnails per video; let YouTube's Test & Compare pick.

### #40 — Observability page  **[P1]**
Dashboard `/status`: last discovery/publish times, count of videos per state, failed jobs,
quota used today (YouTube + Gemini), free disk, GPU temp. This is your window into an
otherwise headless system.

### #41 — Cost/usage view  **[P2]**
Per-video tokens + API units + render minutes, trended — so the marginal cost of scaling is
visible before you commit.

### #42 — Backup mechanics (the "how" for #9)  **[P1]**
`pg_dump -Fc` nightly via Task Scheduler to a second drive; `rclone copy output/` to a
free-tier bucket (Backblaze B2 10 GB free, or Cloudflare R2); keep 14 daily + 8 weekly;
quarterly test restore into a scratch database.

### #43 — Reproducibility  **[P1]**
Commit `uv.lock`; pin the FFmpeg build version; pin model revision hashes in `config.yaml`;
record `{ffmpeg_version, kokoro_rev, whisper_rev, pipeline_git_sha}` on every `render` row so
any past video can be rebuilt.

### #44 — Content-safety gate  **[P0]**
**Mitigation:** a `config.yaml` topic blocklist (active politics, ongoing conflicts, recent
tragedies, atrocity/genocide history, medical/legal advice, living-person controversy);
`sensitivity_check.md` runs before scripting and can veto a candidate; borderline topics route
to manual approval instead of auto-proceed.

### #45 — Legal cushion  **[P0]**
On-screen source citations; a description block listing every source + image attribution; an
"educational commentary" framing line; and a review step that genuinely watches the whole
video. The meaningful human gate is simultaneously your quality control and your policy
defense.

### #46 — Topic performance memory  **[P2]**
Persist per-topic outcomes; never re-run a clear loser; deliberately revisit winners with new
angles.

### #47 — Keep text and voice decoupled for later localization  **[P2]**
Script, TTS, and subtitles are already separable — keep it that way so a second-language
channel is additive (swap voice model + a translation step), not a rewrite.

---

## What is genuinely fine as-is

- **Postgres as DB + queue** (via procrastinate) — right call, scales far past this.
- **Gemini 2.5 Flash** for the brain — free tier covers the volume; swapping models later is a
  string change.
- **FFmpeg** as the renderer — irreplaceable; the risk is *how* it's invoked (#12), not the
  choice.
- **onnxruntime + CTranslate2, no PyTorch** — removes the biggest dependency-fragility source.
- **Single machine** — adequate for 10× this volume; the mitigations above (#9, #42) cover the
  failure mode without new infrastructure.
- **fsspec storage abstraction** — the cloud-migration path is already de-risked.
- **`channel_id` FK everywhere** — multi-channel is a data change, not a schema migration.
