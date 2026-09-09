# YouTube Pipeline

Automated, near-$0 pipeline: a trending history/education topic → a ~7-minute long-form video
+ 7 Shorts cut from it → one human review gate → YouTube. Fully scheduled.

> **Status:** scaffolding. Stack is frozen; application code not yet written.

```
daily trigger → trend discovery → LLM research → fact-check → script (+[SHORT] spans)
  → segment plan → open-access image fetch → Kokoro TTS → Whisper word-timing
  → FFmpeg assemble (slideshow + subs + ducked music) → long-form + 7 Shorts
  → human review gate → YouTube upload → analytics pulled back
```

**Everything is in [DESIGN.md](DESIGN.md)** — stack, inputs, install steps, data model,
risks, scalability. Config templates: [`.env.example`](.env.example),
[`config.example.yaml`](config.example.yaml).

## Quick start

```powershell
winget install astral-sh.uv Git.Git Gyan.FFmpeg PostgreSQL.PostgreSQL.16 NSSM.NSSM
uv venv --python 3.11
.\.venv\Scripts\Activate.ps1
uv sync
copy .env.example .env               # fill in — see DESIGN.md §3
copy config.example.yaml config.yaml # edit — see DESIGN.md §3.4
alembic upgrade head
procrastinate schema --apply
python scripts\load_config.py config.yaml
```

Full walkthrough: [DESIGN.md §4](DESIGN.md#4-install-windows-11-native).

## Layout

```
app/            pipeline stages, queue app, web dashboard
scripts/        token setup, smoke tests, run_pipeline, load_config
prompts/        Gemini prompt templates (added in build phase)
migrations/     Alembic migrations
assets/         curated local media (git-ignored)
DESIGN.md       the one design + setup reference
config.yaml     single source of truth (git-ignored; see config.example.yaml)
pyproject.toml  dependencies (uv)
```

Private project — no license granted.
