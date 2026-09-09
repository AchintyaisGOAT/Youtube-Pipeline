# Install Guide — Windows 11, native (locked stack)

Follow top to bottom. Grey boxes are typed into **PowerShell** (press `Win`, type
"PowerShell", Enter). Each step says how to **verify** it worked.

Estimated time: 45–70 min, mostly downloads. No Docker, no Redis, no PyTorch.

---

## Part 0 — Windows prep (do this first)

```powershell
# Allow the venv activation script to run
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# Enable long path support (media paths get long) — needs admin PowerShell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

After the whole install, add `D:\Youtube_Pipeline\work` and `...\output` and `...\data` to
**Windows Security → Virus & threat protection → Exclusions** (antivirus scanning files
mid-render causes FFmpeg "permission denied" — REVIEW.md #30).

---

## Part 1 — Base tools

### 1.1 uv (Python env + dependency manager)
```powershell
winget install --id astral-sh.uv -e
```
Reopen PowerShell, verify:
```powershell
uv --version
```

### 1.2 Git for Windows
```powershell
winget install --id Git.Git -e
```
Reopen PowerShell:
```powershell
git --version
```

### 1.3 Python 3.11
You have some Python already — check the version:
```powershell
python --version
```
If it is **not** `3.11.x`, either install it or let uv manage it:
```powershell
winget install --id Python.Python.3.11 -e
# (uv can also do: uv python install 3.11)
```

### 1.4 FFmpeg (with libass + NVENC)
```powershell
winget install --id Gyan.FFmpeg -e
```
Reopen PowerShell:
```powershell
ffmpeg -version
ffmpeg -hide_banner -encoders | Select-String nvenc
ffmpeg -hide_banner -filters   | Select-String "ass|xfade"
```
Expect a version banner, an `h264_nvenc` line, and `ass` + `xfade` filter lines.
If "not recognized", reboot (PATH refresh) and retry.

### 1.5 NVIDIA driver
- **GeForce Experience → Drivers → latest Studio Driver**, or nvidia.com/drivers.
- No CUDA Toolkit needed — the CUDA/cuDNN runtime arrives as pip wheels.
- Verify:
```powershell
nvidia-smi
```
Expect a table with your GPU name, driver version, and VRAM. Note the VRAM figure.

### 1.6 NSSM (service manager, used at the end)
```powershell
winget install --id NSSM.NSSM -e
```

---

## Part 2 — Database (PostgreSQL 16 + pgAdmin)

This is the database **and** the job queue (procrastinate runs on Postgres — there is no
Redis).

### 2.1 Install
```powershell
winget install --id PostgreSQL.PostgreSQL.16 -e
```
Graphical installer choices:
- Components: **PostgreSQL Server**, **pgAdmin 4**, **Command Line Tools** (skip Stack Builder)
- Data directory: default
- **`postgres` superuser password:** choose one and **write it down**
- Port: **5432**
- Locale: default

### 2.2 Create the pipeline's database and user
Open **pgAdmin 4** → set its own master password → Servers → PostgreSQL 16 → enter the
`postgres` password.

Right-click **Login/Group Roles → Create → Login/Group Role**
- General → Name: `ytpipe`
- Definition → Password: choose one, write it down
- Privileges → **Can login**: ON

Right-click **Databases → Create → Database**
- Database: `ytpipe`
- Owner: `ytpipe`

### 2.3 Verify
```powershell
psql -U ytpipe -d ytpipe -h localhost -c "select version();"
```
Enter the `ytpipe` password. Expect `PostgreSQL 16.x ...`.

`DATABASE_URL` = `postgresql+psycopg://ytpipe:THE_PASSWORD@localhost:5432/ytpipe`

---

## Part 3 — The project

### 3.1 Version control
```powershell
cd D:\Youtube_Pipeline
git init
```
(Project code lands here in the next phase of work; `pyproject.toml` + `uv.lock` come with it.)

### 3.2 Create the environment and install everything
```powershell
uv venv --python 3.11
.\.venv\Scripts\Activate.ps1        # prompt now shows (.venv)
uv sync                              # installs from pyproject.toml + uv.lock, exactly pinned
```
> `uv sync` replaces the old `pip install -r requirements.txt`. No separate PyTorch step —
> `onnxruntime-gpu` and the CUDA wheels are in the lockfile.

### 3.3 Verify the GPU is visible to onnxruntime
```powershell
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
```
Expect a list containing `CUDAExecutionProvider`. If it only shows `CPUExecutionProvider`,
update the NVIDIA driver (1.5) — the pipeline still runs on CPU meanwhile.

### 3.4 Create working folders
```powershell
mkdir data, work, output, assets\music, assets\sfx, assets\fonts, assets\branding, assets\thumbnails
```

---

## Part 4 — Google / YouTube accounts & keys

### 4.1 Two Google accounts (deliberate — REVIEW.md #8)
1. **accounts.google.com** → create the **channel account**. Enable 2-Step Verification.
2. **accounts.google.com** → create a **second account** for Cloud + Gemini. 2FA on.

### 4.2 YouTube channel (on the channel account)
1. **youtube.com** → profile → **Create a channel**.
2. **YouTube Studio → Settings → Channel → Feature eligibility** → verify by phone.
3. Note the **Channel ID**: Studio → Settings → Channel → Advanced → "Channel ID" →
   `.env` as `YOUTUBE_CHANNEL_ID`.

### 4.3 Gemini API key (on the second account)
1. **aistudio.google.com** → **Get API key → Create API key**.
2. Copy to `.env` as `GEMINI_API_KEY`. Free tier is default; no billing.

### 4.4 Google Cloud project + YouTube APIs (on the second account)
1. **console.cloud.google.com** → **New Project** → name `yt-pipeline` → Create.
2. **APIs & Services → Library** → Enable:
   - *YouTube Data API v3*
   - *YouTube Analytics API*
3. **APIs & Services → OAuth consent screen**:
   - User type **External** → Create
   - App name `yt-pipeline`; support + developer email = the second account
   - **Test users → Add** the **channel account** email
   - Leave **Publishing status: Testing**
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Type **Desktop app**, name `yt-pipeline-desktop` → Create
   - **Download JSON** → save as `D:\Youtube_Pipeline\client_secret.json`
5. Open that file, copy `client_id` / `client_secret` into `.env`.

### 4.5 Generate the YouTube refresh token (once)
With the venv active and `client_secret.json` present:
```powershell
python scripts\get_youtube_token.py
```
Browser opens → log in as the **channel account** → "Google hasn't verified this app" →
**Continue** → approve. The script requests **minimum scopes** (`youtube.upload`,
`youtube.readonly`, `yt-analytics.readonly`) and prints a **refresh token** + writes
`token.json`. Copy the token to `.env` as `YOUTUBE_REFRESH_TOKEN`.

> `token.json` grants upload rights to your channel. It is git-ignored — also keep it out of
> any cloud-synced or backed-up folder (REVIEW.md #19).

### 4.6 Smithsonian key (optional image source)
**api.data.gov/signup** → key emailed instantly → `.env` as `SMITHSONIAN_API_KEY`.

---

## Part 5 — Fill in `.env`
```powershell
copy .env.example .env
notepad .env
```
Fill every blank from Parts 2 and 4. Set `WIKIMEDIA_CONTACT` to a real email string (required
by Wikimedia or it blocks requests). Use forward slashes in paths. Pick one `ALERT_URL`
(e.g. `ntfy://ntfy.sh/some-private-topic` — then subscribe to that topic in the free ntfy app).

---

## Part 6 — Initialise the database
```powershell
alembic upgrade head              # application tables
procrastinate schema --apply      # queue tables (separate ownership from Alembic)
```
Expect both to finish without error. In pgAdmin, the `ytpipe` database now has the app tables
(`channel`, `video`, `segment`, `asset`, `api_quota_ledger`, …) plus `procrastinate_*`.

Seed your channel config:
```powershell
python scripts\load_config.py config.yaml
```
This writes `config.yaml` into the `channel` row with a `config_version`.

---

## Part 7 — Smoke tests (prove each piece)

Run one at a time (venv active). Each ships as a small script under `scripts/`.

| Command | Proves |
|---|---|
| `python scripts\check_db.py` | Postgres reachable, tables present, procrastinate schema OK |
| `python scripts\check_gemini.py` | Gemini key works — prints a test completion + token count |
| `python scripts\check_youtube.py` | YouTube auth works — prints channel title, sub count, today's quota used |
| `python scripts\check_trends.py` | Discovery sources respond (Wikimedia Pageviews primary; pytrends best-effort) |
| `python scripts\check_images.py "roman empire"` | Wikimedia / LoC / Smithsonian / Met — prints result counts + confirms User-Agent accepted |
| `python scripts\check_tts.py` | Kokoro (ONNX) works — writes `work/tts_test.wav`; prints measured words/sec |
| `python scripts\check_whisper.py work\tts_test.wav "the known transcript text"` | faster-whisper works — prints word timestamps + alignment-mismatch % |
| `python scripts\check_ffmpeg.py` | Builds a 5 s NVENC clip with burned subtitles + ducked audio in `work/` |
| `python scripts\check_alert.py` | Sends a test notification to your `ALERT_URL` |

When all nine pass, the environment is ready for the build phase.

---

## Part 8 — First runs (safe mode)

```powershell
python scripts\run_pipeline.py --dry-run        # everything except the upload
python scripts\run_pipeline.py --visibility unlisted   # first 5–10 real videos, unlisted
```
Only switch to scheduled/private-then-publish once a few unlisted videos look right
(REVIEW.md #32).

---

## Part 9 — Run unattended (services via NSSM)

Two long-running processes:
- **worker** — `python -m procrastinate --app=app.queue.app worker` (runs stage jobs + the
  daily periodic discovery task)
- **dashboard** — `uvicorn app.web:app --host 127.0.0.1 --port 8765`

Register both as services (admin PowerShell):
```powershell
nssm install YtPipeWorker    "D:\Youtube_Pipeline\.venv\Scripts\python.exe" "-m" "procrastinate" "--app=app.queue.app" "worker"
nssm set     YtPipeWorker    AppDirectory "D:\Youtube_Pipeline"
nssm set     YtPipeWorker    AppStdout    "D:\Youtube_Pipeline\data\logs\worker.log"
nssm set     YtPipeWorker    AppStderr    "D:\Youtube_Pipeline\data\logs\worker.log"
nssm set     YtPipeWorker    AppExit Default Restart
nssm start   YtPipeWorker

nssm install YtPipeDashboard "D:\Youtube_Pipeline\.venv\Scripts\python.exe" "-m" "uvicorn" "app.web:app" "--host" "127.0.0.1" "--port" "8765"
nssm set     YtPipeDashboard AppDirectory "D:\Youtube_Pipeline"
nssm set     YtPipeDashboard AppExit Default Restart
nssm start   YtPipeDashboard
```
Dashboard: http://127.0.0.1:8765 — review queue, pipeline status, failed jobs, quota used
today, disk free.

Manage: `nssm restart YtPipeWorker` · `nssm stop YtPipeWorker` · `nssm remove YtPipeWorker confirm`.

---

## Part 10 — Backups (set up now, not later — REVIEW.md #9)

Scheduled Task, daily:
```powershell
pg_dump -U ytpipe -h localhost -Fc ytpipe -f "E:\backups\ytpipe_%DATE%.dump"
```
Point it at a **different physical drive** (or add `rclone copy output/ remote:bucket` to a
free cloud bucket). Test a restore into a throwaway database once a quarter.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `winget` not found | Install "App Installer" from Microsoft Store, reopen PowerShell |
| `ffmpeg` not recognized after install | Reboot (PATH refresh) |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, retry |
| `onnxruntime` shows only `CPUExecutionProvider` | Update NVIDIA Studio driver; pipeline runs on CPU meanwhile |
| `psql: could not connect` | `services.msc` → start `postgresql-x64-16` |
| `alembic` vs `procrastinate` table conflict | Run `procrastinate schema --apply` separately; never let Alembic autogenerate over `procrastinate_*` |
| FFmpeg "Permission denied" mid-render | Add `work/`, `output/`, `data/` to antivirus exclusions |
| Wikimedia returns 403 | `WIKIMEDIA_CONTACT` not set / not a real contact string |
| Gemini `RESOURCE_EXHAUSTED` | Daily free-tier cap hit; task retries next day, or slow the schedule |
| Upload succeeds but video is Private, won't publish | Expected pre-audit — publish in Studio; submit the API audit form later |
| Path too long errors | Confirm Part 0 long-paths reg key applied, then reboot |
