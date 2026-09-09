# YouTube Pipeline

Automated, mostly-free pipeline that takes a trending history/education topic and produces a
~7-minute long-form video plus 7 Shorts cut from it, with one human review gate before upload.

> **Status:** planning / scaffolding. Stack is frozen; application code not yet written.

## Documentation

| Doc | What's in it |
|---|---|
| [STACK.md](STACK.md) | The locked technology decisions, layer by layer, with rejected alternatives and a scalability analysis. |
| [INPUTS.md](INPUTS.md) | Every input the project needs — API keys, local software, asset libraries, manual config, the review gate. |
| [INSTALL.md](INSTALL.md) | Step-by-step Windows 11 native install (uv, PostgreSQL, FFmpeg, models, Google/YouTube setup, services). |
| [REVIEW.md](REVIEW.md) | 47 known risks & improvements, tagged P0/P1/P2, with a pre-launch checklist. |

## Pipeline at a glance

```
daily trigger → trend discovery → LLM research → fact-check → script (+[SHORT] spans)
  → segment plan → open-access image fetch → Kokoro TTS → Whisper word-timing
  → FFmpeg assemble (slideshow + subs + ducked music) → long-form + 7 Shorts
  → human review gate → YouTube upload → analytics pulled back
```

## Stack summary

- **Language:** Python 3.11, managed with `uv` (locked `uv.lock`)
- **Data + queue:** PostgreSQL 16 + SQLAlchemy/Alembic + procrastinate (no Redis)
- **Brain:** Google Gemini 2.5 Flash (cloud, free tier) + Google Search grounding
- **Local AI:** Kokoro TTS (ONNX) + faster-whisper (CTranslate2) — GPU, no PyTorch
- **Media:** FFmpeg (NVENC + libass), Pillow, pysubs2
- **Images:** Wikimedia Commons, Library of Congress, Smithsonian, Met (public-domain / CC0)
- **Publish:** YouTube Data API v3 + YouTube Analytics API v2
- **Serve/ops:** FastAPI review dashboard, NSSM Windows services, fsspec storage abstraction

See [STACK.md](STACK.md) for the full picture.

## Getting started

Follow [INSTALL.md](INSTALL.md) top to bottom. Short version:

```powershell
winget install astral-sh.uv Git.Git Gyan.FFmpeg PostgreSQL.PostgreSQL.16 NSSM.NSSM
uv venv --python 3.11
.\.venv\Scripts\Activate.ps1
uv sync
copy .env.example .env   # then fill it in
alembic upgrade head
procrastinate schema --apply
python scripts\load_config.py config.yaml
```

## Repository layout (planned)

```
app/            pipeline stages, queue app, web dashboard
scripts/        one-off + operational scripts (token setup, smoke tests, run_pipeline)
prompts/        Gemini prompt templates
migrations/     Alembic migrations
assets/         curated local media libraries (git-ignored except .gitkeep)
docs/           design docs (the .md files above)
config.yaml     single source of truth for channel/behaviour config
pyproject.toml  dependencies (installed via uv)
```

## License

Private project — no license granted.
