# Content Pipeline (Research → Script → Metadata) — Context for AI Assistant

Paste this whole file into your AI coding assistant at the start of a session. It contains
full project context plus your specific scope. You do not need to read the other two
WORK_*.md files — this one is self-contained.

## 1. What this project is

Automated, near-$0 pipeline: a trending history/education topic → a ~7-minute long-form
YouTube video + 7 Shorts cut from it → one human review gate → YouTube upload. Runs on a
schedule (weekly topic discovery, daily "advance" tick), single Windows 11 machine, one GPU.

```
[YOU] discover → gate → rank → research → fact-check → script → segment → images
[them] tts → align → assemble → shorts
[YOU] metadata
[them] thumbnail → human review → upload
[YOU] analytics
```

`DESIGN.md` in the repo root has the original, more elaborate design. **Ignore its §2 (stack)
and §8 — those describe a Postgres + job-queue + web-dashboard architecture we deliberately
simplified away.** Treat DESIGN.md §3 (API keys/inputs) and §6 (risks — especially #3
fact-checking, #26 discovery signal, #38 metadata quality, #44 content-safety gate) as
still-valid background specifically relevant to your work.

## 2. Simplified stack (only the parts relevant to you)

| Layer | Choice |
|---|---|
| DB | SQLite, one file, via SQLAlchemy (`app/db.py`, owned by Foundation) |
| LLM | Gemini 2.5 Flash via `google-genai`, with Search grounding for fact-checking |
| Discovery signal | Wikimedia Pageviews API only (dropped `pytrends` — unofficial/ToS-grey, low value) |
| Images | Wikimedia Commons + Library of Congress only for now (dropped Smithsonian/Met — extra integration for marginal gain, add back later if needed) |
| HTTP | `httpx` client from `app.http` (Foundation-owned factory) + `tenacity` retry |
| Config | Channel behavior via `get_channel_config(session)` (Foundation-owned helper) — **never read config.yaml directly** |
| No job queue, no web dashboard | Your `run()` functions are plain sequential functions called by `run_pipeline.py` (Foundation-owned) |

## 3. Full data model

Implemented in `app/db.py` (Foundation-owned — do not edit it; if you need a new column, ask
Foundation). This is the contract you code against.

| Table | Key columns | Written by | Read by |
|---|---|---|---|
| `channel` | id, handle, config (JSON), config_version | Foundation | everyone |
| `candidate` | id, channel_id, source, title, summary, score, status | **you** | **you** |
| `video` | id, channel_id, candidate_id, status, title, script, research(json), video_metadata(json), duration_s, thumbnail_uri, error | **you** (research→images columns) + Media (duration_s, thumbnail_uri, status through publish) | everyone |
| `segment` | id, video_id, idx, text, image_query, image_asset_id, start_s, end_s, in_short_span | **you** (text/image_query/image_asset_id/in_short_span) + Media (start_s/end_s) | Media reads `in_short_span` + `text` |
| `short` | id, video_id, idx, start_s, end_s, title, caption, status, youtube_id | Media | Media |
| `asset` | id, kind, source, source_id, license, rights_url, attribution, uri | **you** (image fetch stage) | Media (`assemble` reads `uri`) |
| `render` | id, video_id, short_id, kind, stage, status, output_uri, tool_versions | Media | Media |
| `upload` | id, target_type, target_id, client_token, status, youtube_id, visibility, published_at, quota_units | Media | **you** (analytics needs `youtube_id`) |
| `analytics_snapshot` | id, youtube_id, captured_at, views, watch_time_minutes, retention | **you** | **you** |
| `api_quota_ledger` | api, day, units_used, tokens_used | via `app.quota.check_and_increment()` (Foundation helper) — call it before every Gemini call | shared |
| `topic_performance` | channel_id, topic_key, tag, score | **you** | **you** |
| `llm_cache` | key(sha256), model, response(json), hits | **you** (wrap every Gemini call through this cache) | **you** |

## 4. Status enum you drive (`app/status.py`, already implemented — reuse, don't redesign)

```
candidate_new → candidate_vetoed | candidate_manual_review → candidate_approved | candidate_rejected
(video row created here, status = researching)
researching → fact_checking → scripting → segmenting → fetching_images → (hands off to Media)
... (Media runs synthesizing_voice → aligning → assembling → cutting_shorts) ...
generating_metadata → (hands back to Media for generating_thumbnail)
```

## 5. Shared conventions (Foundation-defined, you must follow)

Every stage module lives at `app/pipeline/<stage>.py` and exposes:

```python
def run(session: Session, video_id: uuid.UUID) -> None:
    """Idempotent: return early if row.status isn't the status this stage expects.
    Do the work, set row.status to the next Status on success. Let exceptions propagate —
    never catch-and-swallow; the orchestrator handles failure + alerting."""
```
Candidate-stage functions (`discover`, `gate`, `rank`) take `candidate_id` instead, except
`discover` which takes no id (it creates new `candidate` rows) and `rank` which takes none
either (it scans all `candidate_approved` rows and promotes the best one to a `video` row).

Rules:
- No `session.commit()` inside your stage functions — the orchestrator commits once per stage.
- File paths via `app.storage.work_dir(video_id)` — never a hardcoded path.
- Every Gemini call goes through `llm_cache` (key = `sha256(model + prompt + inputs)`) and
  through `app.quota.check_and_increment(session, "gemini", tokens)` — over-budget calls defer
  (raise a specific `QuotaExceeded` exception; orchestrator treats it as "retry later", not a
  hard failure).
- Never call an external API directly — always through `app.http`'s shared client (Foundation).

## 6. Your scope (Content)

**Files you own** (nobody else touches these):
```
app/pipeline/discover.py   — Wikimedia Pageviews API → create `candidate` rows
app/pipeline/gate.py       — apply config's auto_veto / manual_review lists (DESIGN.md #44)
                              to each new candidate → candidate_vetoed | candidate_manual_review
                              | candidate_approved
app/pipeline/rank.py       — pick best candidate_approved row (score + topic_performance) →
                              create `video` row, status=researching
app/pipeline/research.py   — Gemini + Search grounding → video.research (JSON: claims + sources)
app/pipeline/factcheck.py  — Gemini verifies each claim in research against grounding sources;
                              drops unsourced claims; status → fact_checking done
app/pipeline/script.py     — Gemini writes the script. Mark short-worthy spans inline with
                              `[SHORT]...[/SHORT]` — this markup is internal to your code only;
                              segment.py below is the only other code that parses it.
app/pipeline/segment.py    — split script into `segment` rows (text, image_query per segment);
                              set `in_short_span=True` for segments inside a [SHORT] span. This
                              boolean column — not the raw markup — is the contract with Media.
app/pipeline/images.py     — for each segment.image_query, fetch from Wikimedia Commons / LoC,
                              filter to public-domain-us/cc0 only (DESIGN.md #4), create `asset`
                              row, set segment.image_asset_id
app/pipeline/metadata.py   — Gemini: 3-5 title candidates, description w/ chapters + sources +
                              attribution block, tags → video.title + video.video_metadata (JSON)
app/pipeline/analytics.py  — pull YouTube Analytics API at 48h/7d post-publish for each
                              `upload.youtube_id` → analytics_snapshot rows; also updates
                              topic_performance
prompts/research.md, prompts/factcheck.md, prompts/script.md, prompts/metadata.md
                              — the actual Gemini prompt templates
tests/test_content_pipeline.py — golden-file: fixed topic → script contains ≥1 [SHORT] span;
                              segment.py correctly sets in_short_span
```

## 7. Definition of done

- Each module above implements `run()` per the contract, is idempotent (safe to re-run), and
  writes only to the columns listed as "written by you" in §3.
- `discover.py` + `gate.py` + `rank.py` run against `config.example.yaml`'s topic lists without
  error and correctly veto an "events within the last 10 years" candidate.
- `script.py` output contains at least one `[SHORT]...[/SHORT]` span; `segment.py` correctly
  translates that into `in_short_span=True` on the right segments.
- `images.py` never stores an asset without `license` and `rights_url` populated.
- Gemini calls are cached (`llm_cache`) and quota-checked (`api_quota_ledger`) — verify by
  running the same topic twice and confirming zero new tokens spent on the second run.
- `metadata.py` output includes an on-screen-sources/attribution block in the description
  (DESIGN.md #45).

## 8. Handoff to Media

You must leave, before Media's stages can run:
- `video.script` populated, with `[SHORT]` spans present in the text
- `segment` rows fully populated: `text`, `image_query`, `image_asset_id`, `in_short_span`
- `asset` rows for every `segment.image_asset_id`, each with `uri`, `license`, `rights_url`
- `video.status == fetching_images` (done) so the orchestrator advances to
  `synthesizing_voice`, which is Media's first stage

You do not touch `app/pipeline/tts.py`, `align.py`, `assemble.py`, `shorts.py`,
`thumbnail.py`, `upload.py`, or `review.py` — those are Media's.
