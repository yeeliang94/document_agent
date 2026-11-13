# Document Intelligence MVP

One agent with a Python sandbox prepares a workbook from a nested submission folder and either an Excel template or a written format. The web app shows the run as a conversation, lets you look inside every file, and shows which source files each output row came from. OpenAI (or an OpenAI-compatible gateway) is the only model provider in this MVP.

## Run locally

Install Python **3.12 or newer**. No Node.js or separate frontend build is required.

macOS / Linux:

```bash
./start.sh
```

Windows PowerShell or Command Prompt:

```powershell
.\start.bat
```

The launcher creates `.venv`, installs pinned application dependencies when requirements change, copies `.env.example` to `.env` if needed, starts the backend, and opens the frontend after its readiness check succeeds. The default address is <http://127.0.0.1:8003>. Use `./start.sh --no-browser` or `.\start.bat --no-browser` for terminal-only startup. Stop with Ctrl+C. A second invocation reopens an already running instance; an unrelated occupied port produces an error instead of stopping that process.

Edit `.env` before processing documents:

```dotenv
OPENAI_API_KEY=your-key-here
OPENAI_MODEL=gpt-5.4-mini
OPENAI_BASE_URL=https://api.openai.com/v1
```

The UI can open without a key. Real agent runs require a valid key, model access, and a reachable endpoint. Keep `.env` private. Model calls incur usage charges.

After editing `.env`, reload the application with `./start.sh --restart` (Windows: `.\start.bat --restart`). The launcher restarts only a server identifying itself as this project; it leaves unrelated processes untouched. Pause active work before restarting. On Windows, process termination may interrupt the current operation; saved jobs can be resumed after restart.

Choose New run, describe what the agent should produce, pick the source folder (or ZIP) and the `.xlsx` template or `.json` format, then start. Follow the run in the conversation, answer the agent's questions there, and open any file in the Files panel. The Technical log holds every event. Download the workbook and review anything listed under Still to fix.

## Sample payroll submission

Generate a synthetic packet with nested folders, a source workbook, a scanned image, and a formula-bearing output template:

```bash
.venv/bin/python scripts/create_sample.py
```

Use `data/samples/payroll_packet.zip` as the source and `data/samples/output_template.xlsx` as the destination template. The sample task instructions are inside the packet. These files contain synthetic records only.

## Check a direct OpenAI call

After startup has installed dependencies:

```bash
.venv/bin/python scripts/check_openai.py
```

Windows:

```powershell
.\.venv\Scripts\python.exe scripts\check_openai.py
```

This makes one short synthetic Responses API request to `https://api.openai.com/v1`, with retries disabled and `store=False`. It sends no uploaded documents. When the application uses the official OpenAI endpoint, `OPENAI_DIRECT_API_KEY` and `OPENAI_DIRECT_MODEL` optionally override its normal key/model. When `OPENAI_BASE_URL` points elsewhere, both direct variables are required; gateway credentials and model aliases are never reused for the direct check. Missing configuration produces an error without a request. Direct OpenAI access may be blocked by enterprise network policy.

## Windows enterprise setup

1. Install an approved Python 3.12+ distribution with the Python launcher or Python on PATH. Run `start.bat` from a writable local checkout, including paths containing spaces.
2. Allow package installation from the approved Python package index. Existing `PIP_INDEX_URL` / `PIP_CERT` settings are respected. If installation fails, the next launch retries it.
3. Set the approved `OPENAI_BASE_URL` (including its `/v1` prefix where required), exact `OPENAI_MODEL` alias, and credential in `.env`. The endpoint must support the OpenAI Responses API with the code interpreter (sandbox containers) and file uploads. There is no automatic provider fallback or Azure API-version adapter. Gateway options (a LiteLLM provider header, naming the model when a sandbox is created) are on the Settings page.
4. Enterprise operating-system certificate trust is loaded before clients are created. If required, set `SSL_CERT_FILE` to the approved PEM CA bundle. Keep TLS verification enabled. Standard HTTPS proxy environment variables are respected by model requests; local readiness probes bypass proxies.
5. Keep source documents local until uploading them to this application. The whole source packet is uploaded to the model endpoint's sandbox during a run. Use an endpoint approved for payroll data.

Windows Excel is not required. The agent reads sources with Python inside the sandbox (workbooks, CSV, PDF, Word, text and nested ZIPs). Scanned pages and images have not been verified on this engine; check any figure that exists only in a scan. Password-protected documents and legacy `.xls` may need conversion first. Outputs are written with `openpyxl`, which cannot recalculate formulas and can drop shapes, pivot tables, slicers, macros and some charts.

## How a run works

The app zips the uploaded sources and gives them to one agent with a Python sandbox, together with the template or format, the module instructions and your goal. The agent explores the packet with code, asks you when a material fact is unclear, and saves:

- the deliverable workbook
- `document_map.json`: every source file with a decision (used, reference, not relevant, unclear) and a reason
- `provenance.json`: the source files behind each row it wrote (when Proof level is "Sources for every row")
- `notes.md`: which file won where two competed, what was left out, and assumptions

Every save is checked: the deliverable against its template or format rules, a decision for every file, and a source for every written row. A run is Finished only when the checks pass; otherwise it is Partly done and lists what is still to fix. An optional independent reviewer and parallel scouts can be switched on.

Settings that shape a run live on the Settings page: scouts, reviewer, proof level, reasoning effort, turn/time/token limits, how long the sandbox waits for an answer, and sandbox memory. `.env` holds only secrets and connection details.

## Runtime and observability

FastAPI serves both the static frontend and the API from one port. SQLite and job files live under `DI_DATA_DIR` (default `./data`). One process runs jobs one at a time and records progress, timings and failures; each run also keeps its scripts and a full record under the job's `engine/` folder. Logs and exports can contain payroll data; treat the data directory as sensitive.

Pause and stop take effect between steps; an in-flight model request may finish first. Interrupted jobs become paused on restart and can be resumed, which starts a new sandbox from the saved outputs and notes. Repeating work incurs additional model usage.

This is a local MVP: one server process, one local SQLite store, no enterprise sign-in or multi-tenant isolation. Keep the default loopback binding for workstation testing. To bind a network interface, set a strong `DI_ACCESS_TOKEN`; the UI asks for it. Never expose an unprotected instance to the internet. Input handling limits and file validation do not make this a hardened public file service.

Requests whose host name is not loopback are refused unless `DI_ACCESS_TOKEN` is set or the name is listed in `DI_ALLOWED_HOSTS`; this stops malicious web pages from reaching a token-less local instance through DNS rebinding. Behind an HTTPS reverse proxy, add the public address to `DI_ALLOWED_ORIGINS` (for example `https://payroll.example.com`) and set uvicorn's `FORWARDED_ALLOW_IPS` to the proxy address so browser requests are not rejected as cross-origin.

Runs made by the earlier engine stay in the list and can be opened and read, but not continued. Start a new run instead.

## Azure App Service container preparation

The included Linux container is a deployment starting point:

```bash
docker build -t document-intelligence .
docker run --rm -p 8003:8003 --env-file .env -e DI_ACCESS_TOKEN=replace-with-a-strong-token -e DI_DATA_DIR=/data -v document-intelligence-data:/data document-intelligence
```

Container binding uses `0.0.0.0`; configure the App Service container port to `8003`, supply secrets through App Service settings, and mount persistent storage at `/data`. Configure HTTPS and access restrictions on the hosting platform. Run **one instance and one worker**. This MVP's SQLite/local worker design is not ready for horizontal scaling; use shared durable storage and a separate durable queue/worker before scaling out. App Service deployment, identity integration, network approval, and native Excel compatibility are not established by a successful local run.

## Development checks

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

Job status groups (which states can start, pause or take messages) live in `app/jobstatus.py`; the browser reads them from `/api/config`, so change them there only.

On Windows, substitute `.\.venv\Scripts\python.exe` for `.venv/bin/python`. `GET /health/readiness` checks application startup independently of OpenAI credentials; the direct-call script checks upstream model connectivity separately.
