# YouTube Pipeline

Automated, near-$0 pipeline: a trending history/education topic → a ~7-minute long-form video
+ 7 Shorts cut from it → one human review gate → YouTube. Fully scheduled.

> **Status:** scaffolding. Stack is frozen; application code not yet written. The DB / run
> steps below need the `app/` + `scripts/` code that arrives in the build phase.

```
daily/weekly trigger → trend discovery → LLM research → fact-check → script (+[SHORT] spans)
  → segment plan → open-access image fetch → Kokoro TTS → Whisper word-timing
  → FFmpeg assemble (slideshow + subs + ducked music) → long-form + 7 Shorts
  → metadata + thumbnail → human review gate → YouTube upload → analytics pulled back
```

**Everything is in [DESIGN.md](DESIGN.md)** — stack, inputs, install steps, data model,
risks, scalability. Config templates: [`.env.example`](.env.example),
[`config.example.yaml`](config.example.yaml).

## Quick start

```powershell
# one winget package per call
winget install --id astral-sh.uv --exact
winget install --id Git.Git --exact
winget install --id Python.Python.3.11 --exact
winget install --id Gyan.FFmpeg --exact
winget install --id PostgreSQL.PostgreSQL.16 --exact
winget install --id NSSM.NSSM --exact

uv sync                               # creates .venv from pyproject.toml + uv.lock
copy .env.example .env                # fill in — see DESIGN.md §3
copy config.example.yaml config.yaml  # edit — see DESIGN.md §3.4

# --- after the build-phase code exists: ---
uv run alembic upgrade head
uv run procrastinate --app=app.queue.app schema --apply
uv run python scripts/load_config.py config.yaml
```

Full walkthrough: [DESIGN.md §4](DESIGN.md#4-install-windows-11-native). Prefer `uv run <cmd>`
over activating the venv.

## Planned layout

```
app/            pipeline stages, queue app, web dashboard
scripts/        token setup, smoke tests, run_pipeline, load_config, doctor
prompts/        Gemini prompt templates
migrations/     Alembic (alembic.ini + env.py + versions/)
tests/          golden-file + unit tests
assets/         curated local media (git-ignored)
DESIGN.md       the one design + setup reference
config.yaml     single source of truth (git-ignored; see config.example.yaml)
pyproject.toml  dependencies (uv)
```

Private project — no license granted.
