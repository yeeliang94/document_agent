# Document Intelligence MVP

One PydanticAI agent prepares a monthly payroll workbook from a nested submission folder and an Excel template. The web app shows progress and inspectable tool activity. OpenAI is the only model provider in this MVP.

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

Upload the source folder (or ZIP) and destination `.xlsx` template, choose Payroll, supply monthly instructions and optional module context, then start the run. Use progress events and Diagnostics to inspect tool activity and failures. Download the generated workbook and review any unresolved items.

## Sample payroll submission

Generate a synthetic packet with nested folders, a source workbook, a scanned image, and a formula-bearing output template:

```bash
.venv/bin/python scripts/create_sample.py
```

Use `data/samples/payroll_packet.zip` as the source and `data/samples/output_template.xlsx` as the destination template. The sample task instructions are inside the packet. A sample upload is already saved in this development instance's History; it is waiting for an OpenAI key. These files contain synthetic records only.

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

## Live synthetic acceptance test

With the server running and a configured OpenAI key/model, explicitly run:

```bash
.venv/bin/python scripts/live_smoke.py
```

This paid test requires the direct `gpt-6-luna` route. It runs the actual agent twice using generated synthetic payroll documents: a normal workbook job and a job that asks for a period confirmation and resumes. It checks downloaded employee values, preserved formulas/hidden sheet/formatting, image-reading tool execution, source immutability, diagnostic exports/redaction, and terminal SSE replay. Reports and downloaded outputs are saved under `data/live-tests/`. Use `--scenario baseline` for a single normal job. Multiple model calls are expected; this is separate from the one-request connection check. No real payroll records are submitted.

## Windows enterprise setup

1. Install an approved Python 3.12+ distribution with the Python launcher or Python on PATH. Run `start.bat` from a writable local checkout, including paths containing spaces.
2. Allow package installation from the approved Python package index. Existing `PIP_INDEX_URL` / `PIP_CERT` settings are respected. If installation fails, the next launch retries it.
3. Set the approved `OPENAI_BASE_URL` (including its `/v1` prefix where required), exact `OPENAI_MODEL` alias, and credential in `.env`. The endpoint must support OpenAI Responses, tool calls, and image input. There is no automatic provider fallback or Azure API-version adapter.
4. Enterprise operating-system certificate trust is loaded before clients are created. If required, set `SSL_CERT_FILE` to the approved PEM CA bundle. Keep TLS verification enabled. Standard HTTPS proxy environment variables are respected by model requests; local readiness probes bypass proxies.
5. Keep source documents local until uploading them to this application. Relevant text and page images are sent to the configured model endpoint during a run. Use an endpoint approved for payroll data.

The app runs its work on the server, so Windows Excel is not required. The supported source readers cover `.xlsx`, PDF, `.docx`, text, JSON, and images; ZIP uploads are expanded while retaining nested paths. PDF pages are rendered locally and read through the LLM's image input; there is no separate OCR service or Docling dependency. Legacy `.xls`, password-protected documents, external workbook links, and complex Excel features may require conversion or manual review. This MVP uses `openpyxl` and cannot reproduce native Excel calculation or guarantee preservation of every advanced workbook feature.

## Runtime and observability

The same agent construction is used across modules; module instructions define the task. Payroll is the implemented module, with other modules reserved for expansion. FastAPI serves both the static frontend and API from one port. SQLite and job artifacts live under `DI_DATA_DIR` (default `./data`). One process executes background jobs and records progress, tool arguments/results, available model reasoning summaries, timings, and failures. Private model chain-of-thought is not exposed. Logs and exports can contain payroll data; treat the data directory as sensitive.

Saved job state supports recovery, but interrupted model requests are not exactly-once operations. Repeating a request can incur additional model usage. Pause and cancel take effect at operation boundaries; an in-flight model request or tool operation may finish first. Interrupted jobs become paused on restart and can be resumed from the UI. Older bulky tool results are replaced with retrievable artifact pointers to reduce context growth; this is job memory, not a separate vector database or guaranteed unlimited context. Agent tools are deliberately limited to document inspection and workbook operations; there is no arbitrary shell execution tool.

This is a local MVP: one server process, one local SQLite store, no enterprise sign-in or multi-tenant isolation. Keep the default loopback binding for workstation testing. To bind a network interface, set a strong `DI_ACCESS_TOKEN`; the UI asks for it. Never expose an unprotected instance to the internet. Input handling limits and file validation do not make this a hardened public file service.

Requests whose host name is not loopback are refused unless `DI_ACCESS_TOKEN` is set or the name is listed in `DI_ALLOWED_HOSTS`; this stops malicious web pages from reaching a token-less local instance through DNS rebinding. Behind an HTTPS reverse proxy, add the public address to `DI_ALLOWED_ORIGINS` (for example `https://payroll.example.com`) and set uvicorn's `FORWARDED_ALLOW_IPS` to the proxy address so browser requests are not rejected as cross-origin.

Workbook outputs are saved with openpyxl, which can drop shapes, pivot tables, slicers, macros and some charts or images. `validate_output` reports any of these that the template had and the output lost, and the run is then marked partial.

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
