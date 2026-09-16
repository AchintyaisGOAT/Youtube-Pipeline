# Foundation & Infrastructure — Context for AI Assistant

Paste this whole file into your AI coding assistant at the start of a session. It contains
full project context plus your specific scope. You do not need to read the other two
WORK_*.md files — this one is self-contained.

## 1. What this project is

Automated, near-$0 pipeline: a trending history/education topic → a ~7-minute long-form
YouTube video + 7 Shorts cut from it → one human review gate → YouTube upload. Runs on a
schedule (weekly topic discovery, daily "advance" tick), single Windows 11 machine, one GPU.

```
discover → gate → rank → research → fact-check → script → segment → images
  → tts → align → assemble → shorts → metadata → thumbnail
  → human review → upload → analytics
```

`DESIGN.md` in the repo root has the original, more elaborate design (background on risks,
API details, install steps). **Ignore its §2 (stack) and §8 (build order) — those describe a
Postgres + job-queue + web-dashboard architecture we deliberately simplified away.** The
stack below is what we're actually building. Treat DESIGN.md §3 (inputs/API keys) and §6
(risks) as still-valid background reading, nothing else.

## 2. Simplified stack (supersedes DESIGN.md §2)

| Layer | Choice | Why |
|---|---|---|
| DB | **SQLite** (single file, `data/pipeline.db`) | Tiny volume (~6 videos/month). No server, no service, trivial backup (copy the file). |
| ORM | SQLAlchemy 2.0 (already written, `app/db.py`) | Reuse as-is; port from Postgres JSONB/Uuid to cross-dialect types. |
| Migrations | None — `Base.metadata.create_all()` at startup | Single dev, local file, schema changes are rare. No Alembic. |
| Scheduling | Windows Task Scheduler runs `run_pipeline.py` | No persistent worker process, no queue library. |
| Orchestration | One sequential Python script, no queue | Pipeline is linear and low-volume — a queue (procrastinate) solves a concurrency problem this project doesn't have. |
| Review UI | CLI script (owned by Media team) | No FastAPI/web dashboard. |
| Config | `pydantic-settings` (.env) + `pydantic` (config.yaml → DB) | Already written (`app/config.py`), keep. |
| HTTP | `httpx` + `tenacity` retry | No `hishel` cache, no `aiolimiter` — call volume is too low to need them. |
| Notifications | One direct HTTP call to ntfy (or similar) | No `apprise` abstraction for a single channel. |
| Storage | Plain `pathlib`, files under `data/` | No `fsspec` — no cloud storage planned; add it later if that ever changes. |

## 3. Full data model

Already implemented in `app/db.py` (needs the Postgres→SQLite port described in your tasks
below). This table is the contract every team codes against.

| Table | Key columns | Written by | Read by |
|---|---|---|---|
| `channel` | id, handle, config (JSON), config_version | Foundation (`load_config.py`) | everyone |
| `candidate` | id, channel_id, source, title, summary, score, status | Content | Content |
| `video` | id, channel_id, candidate_id, status, title, script, research(json), video_metadata(json), duration_s, thumbnail_uri, error | Content (research→images columns) + Media (duration_s, thumbnail_uri, status through publish) | everyone |
| `segment` | id, video_id, idx, text, image_query, image_asset_id, start_s, end_s, in_short_span | Content (text/image cols) + Media (start_s/end_s) | Media reads `in_short_span` + `text` |
| `short` | id, video_id, idx, start_s, end_s, title, caption, status, youtube_id | Media | Media |
| `asset` | id, kind, source, source_id, license, rights_url, attribution, uri | Content (image fetch) | Media (assemble reads `uri`) |
| `render` | id, video_id, short_id, kind, stage, status, output_uri, tool_versions | Media | Media |
| `upload` | id, target_type, target_id, client_token, status, youtube_id, visibility, published_at, quota_units | Media | Content (analytics needs `youtube_id`) |
| `analytics_snapshot` | id, youtube_id, captured_at, views, watch_time_minutes, retention | Content | Content |
| `api_quota_ledger` | api, day, units_used, tokens_used | via your `app/quota.py` helper, called by Content + Media | Content + Media |
| `topic_performance` | channel_id, topic_key, tag, score | Content | Content |
| `heartbeat` | key, at, detail | Foundation (orchestrator, after each run) | monitoring/doctor |
| `llm_cache` | key(sha256), model, response(json), hits | Content | Content |

**You own this schema.** Content/Media must not edit `app/db.py`; if they need a new column,
they ask you.

## 4. Status enum (already implemented, `app/status.py` — do not redesign, just reuse)

```
candidate: candidate_new → candidate_vetoed | candidate_manual_review → candidate_approved | candidate_rejected
video: researching → fact_checking → scripting → segmenting → fetching_images
     → synthesizing_voice → aligning → assembling → cutting_shorts
     → generating_metadata → generating_thumbnail → awaiting_review
     → approved | rejected → uploading → published | failed
```

## 5. Shared conventions (you define these; Content/Media code against them)

Every pipeline stage module lives at `app/pipeline/<stage>.py` and exposes:

```python
def run(session: Session, video_id: uuid.UUID) -> None:
    """Idempotent: return early if row.status != the status this stage expects.
    Do the work, update row.status to the next Status on success, let exceptions
    propagate on failure — do not catch-and-swallow."""
```
(Candidate-stage functions take `candidate_id` instead of `video_id`.)

Rules every stage must follow:
- No stage calls `session.commit()` directly — the orchestrator wraps each stage call in
  `app.db.session_scope()`, one commit per stage.
- File paths only via `app.storage.work_dir(video_id)` / `output_dir(video_id)` — never a
  hardcoded `data/...` string.
- Channel behavior config only via a `get_channel_config(session)` helper you provide (reads
  the `channel.config` JSON column) — never re-read `config.yaml` from stage code.
- Secrets only via `app.config.get_settings()`.

## 6. Your scope (Foundation)

**Files you own** (nobody else touches these):
```
app/db.py            — port from Postgres to SQLite (JSONB→JSON, keep Uuid — SQLAlchemy 2.0
                        Uuid type is cross-dialect), drop any procrastinate-specific bits
app/config.py         — already mostly done; add get_channel_config(session) helper
app/status.py         — already done, no changes expected
app/http.py           — httpx client factory + tenacity retry preset, one shared client
app/storage.py        — work_dir()/output_dir() helpers, atomic write (*.tmp + rename)
app/notify.py         — one function, direct HTTP POST to an ntfy topic on failure
app/quota.py          — check_and_increment(session, api, units) against api_quota_ledger
run_pipeline.py        — orchestrator: dict of {Status: (module, "run")}, sequential loop,
                          try/except per stage → on failure set status=failed + notify.alert()
scripts/load_config.py — validate config.yaml, write into channel row, bump config_version
scripts/doctor.py       — checks: db reachable, .env keys present, ffmpeg on PATH, disk space
scripts/get_youtube_token.py — OAuth flow, writes refresh token
pyproject.toml          — you own dependency list; prune to the simplified set (see below)
tests/test_db.py, tests/test_config.py
```

**Consolidated dependency list to put in `pyproject.toml`** (covers all three teams — add
these now so Content/Media never need to touch this file):
```
pydantic pydantic-settings pyyaml tzdata          # you
sqlalchemy httpx tenacity loguru                  # you
google-genai                                       # Content (Gemini)
google-api-python-client google-auth-oauthlib
google-auth-httplib2                               # Media (YouTube upload) + you (token script)
kokoro-onnx[gpu] soundfile faster-whisper
huggingface-hub pillow pysubs2                     # Media
dev: ruff pytest pytest-asyncio
```
Removed vs. original scaffold: `psycopg`, `procrastinate`, `alembic`, `hishel`, `aiolimiter`,
`apprise`, `fsspec`, `pytrends`, `fastapi`, `uvicorn`, `jinja2`, `python-multipart`.

## 7. Definition of done

- `uv sync` succeeds with the pruned `pyproject.toml`.
- `python -c "from app.db import Base; Base.metadata.create_all(get_engine())"` creates a
  working `data/pipeline.db` with all 13 tables.
- `load_config.py config.example.yaml` writes a `channel` row and prints the assigned
  `config_version`.
- `doctor.py` prints a green/red table for at least: db, ffmpeg, disk space, `.env` keys present.
- `run_pipeline.py` runs end-to-end against stub `run()` functions (even if Content/Media
  haven't filled in real logic yet — coordinate on stub signatures early so nobody blocks).
- `pytest` passes for `test_db.py` and `test_config.py`.

## 8. What blocks the other two teams

Content and Media cannot start real work until you've shipped: the SQLite-ported `app/db.py`,
`app/http.py`, `app/storage.py`, and the `run()` function signature convention above. Ship
these first, even as a rough first pass — they can be refined later without breaking the
contract.
