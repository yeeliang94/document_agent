# Document Intelligence MVP

One coordinating PydanticAI agent prepares a monthly payroll workbook from a nested submission folder and an Excel template. The web app shows progress and inspectable tool activity. OpenAI is the only model provider in this MVP.

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

## Harness, scouting and large workbooks

The main agent can delegate scoped scouting, investigation or review to read-only workers. Workers have separate histories, share the job's request/time allowance, cannot write the deliverable or create further workers, and return persisted handovers. They may record structural discovery (inventory and row observations), but their document reads do not count toward the main agent's coverage. Pausing a job marks running workers interrupted; a worker that repeats an identical action loop is cancelled. Delegation is off by default; set `DI_MAX_SUBAGENTS` to the number of concurrent workers to offer it. The main agent owns reconciliation and completion; delegation is optional and chosen by the model.

Native `.xlsx` regions can be extracted into durable job datasets, queried in bounded pages, transformed with typed operations, and written directly into declared template regions. Built-in operations include filtering, selection, rename, conversion, code lookup, keyed joins, grouping, deduplication and decimal arithmetic. There is no arbitrary Python or SQL execution. Ambiguous matches and missing formula caches remain exceptions. Records extracted from documents/images can also be saved as source-backed datasets; fields whose values are all numbers are treated as decimals unless a kind is declared. Individual `write_cells` changes that name their source record count as output assignments. Semantic corrections create new dataset revisions. Interrupted output journals can be settled or explicitly aborted without changing workbook bytes, including after new instructions arrive; retries, restarts and explicit recovery settle a journal the same way. A bulk overwrite must include all previously mapped columns in affected rows; this prevents losing validation of retained cells.

Inventory and row coverage live in job-local SQLite, separate from model history. Document reads track chunk/page coverage; a preview alone cannot establish a reviewed disposition. Completion requires both current workbook validation and source/assignment coverage, with no unsettled delegated tasks. User instructions are retained separately. Each one blocks completion until the agent applies it, naming the datasets, mappings, decisions, sources or tasks it affects (datasets derived from an invalidated one are refused too); only an explicit reset invalidates all earlier results. Originals remain unchanged; unresolved records can be returned in a partial draft.

Ordinary tool results are bounded to 16 KB and larger results remain retrievable as artifacts. Context is estimated before requests, including instructions, tool definitions, images and response space. Set `DI_CONTEXT_TOKEN_LIMIT` to the configured model's real context window (default 200,000). The local estimate is corrected from the provider's reported input tokens. A locally available cached `o200k_base` tokenizer is used when available, otherwise a conservative byte estimate is used. Runtime does not intentionally download tokenizer assets. Above 80% of the budget, older bulky tool results are cleared to retrievable pointers in one batch, so the provider's prompt cache breaks rarely. Above 90% after clearing, history is rebuilt from durable facts and recent coherent exchanges. Optional narrative summarization uses the configured model and shared budget; failure retains a deterministic continuation note. Essential instructions that cannot fit cause a recoverable pause.

Configure `DI_CONTEXT_TOKEN_LIMIT`, `DI_RESPONSE_TOKEN_RESERVE`, `DI_TOOL_RESULT_BYTES`, `DI_IMAGE_TOKEN_ESTIMATE`, `DI_INSPECTION_CELLS`, `DI_INSPECTION_SECONDS`, `DI_MAX_SUBAGENTS` and `DI_SUBAGENT_REQUESTS` in `.env`. Model request allowances apply per start/resume session, including workers and narrative summaries; cumulative usage remains visible. Repeat detection ignores volatile counters, warns the model after repeated identical results, detects cycles of up to five actions, and pauses an identical loop. There is no turn-by-turn progress supervisor: the request and time allowances bound a run, and which work comes next is the model's decision. Parsing, rendering, extraction, transformations and workbook writes run in killable subprocesses; the application reaps a cancelled operation before releasing its mutation lock. Interrupted writes retain recovery journals. New jobs use sparse XML indexing with bounded scan units; existing jobs retain their original discovery/accounting version.

Authenticated inventory, coverage and task pages are available at `/api/jobs/{id}/inventory`, `/coverage` and `/tasks`. Run details and Diagnostics show coverage, estimated context use, delegation, recovery and validation events. Existing jobs initialize missing coverage as unknown when resumed; their existing files/history remain intact.

Run the synthetic 5,000 × 30 acceptance without model calls:

```bash
.venv/bin/python scripts/harness_acceptance.py
```

The script checks every output value, numeric types, six-digit identifiers, the template formula and hidden sheet, discovery of a hidden source sheet, source formulas without cached results remaining exceptions, and coverage; results are stored under `data/harness-tests/`. To explicitly run the configured agent through the running application (paid), add `--live`. Live results, usage and exceptions are saved for inspection. Synthetic checks do not establish accuracy on real payroll submissions or native Excel compatibility.

## Goal-oriented workbook processing

Coverage tracks each attempted output row, including rows expanded from one source record. Rejected rows remain completion blockers until a corrected or explicitly filtered dataset rewrites their destination. Saved journals retain these obligations through interruption and recovery; settled retries preserve later replacements. Region corrections retire obsolete observations while retaining checks on existing output contributions and current reference joins.

New uploads use accounting version 2. Existing saved jobs stay on version 1; opening or resuming them does not reinterpret earlier observations or discard outputs, instructions, decisions or journals.

The owning agent keeps a work plan with its objective, focus, work items, blockers, next actions and free-form notes; saving it is never refused. Tool outcomes attach saved results and compact attempted-approach records. Continuation snapshots retain the current plan, source/region/evidence references, requirements and validation; detailed facts stay queryable. The plan never establishes completion: file hashes, requirements, source coverage and journals are checked when the agent returns its final result.

`inspect_source` creates a sparse structural index, including hidden sheets, declared tables, merges, cell comments, formula distinctions, validation guidance and embedded drawings. Its cache is keyed by source content hash and index version. A bounded scan returns its saved cursor and stop reason; an incomplete index cannot pass completion. Repeating complete inspection leaves the index unchanged. Candidate regions are suggestions and may require splitting, resizing or merging with `revise_region`.

Use `search_cells` to find labels below introductory rows and `inspect_regions` to query coordinates and boundary samples. Inspect relevant content, then save an evidence-backed role with `save_interpretation`: records, reference, instructions, outside scope, unresolved or unsupported. Filename matches alone do not establish period/entity or authorize exclusions. `extract_region` creates records within the accepted region/mapping identity; reference extraction supports joins without creating separate payment obligations. Adjacent tables can share worksheet rows. Typed `unpivot` complements existing joins/grouping for one-to-many and many-to-one mappings. Repeated headers, totals and absent formula caches remain explicit exceptions requiring resolution or supported filtering.

Create the output copy and use `save_output_requirements` to establish writable areas, preserved/reference areas, field meanings/types, required values, allowed values, uniqueness and expected totals. Classify every discovered destination area. Writers enforce those input areas. `validate_output` checks current output bytes, template sheets and formulas, source accounting, destination requirements, overlap/duplicate contributions and unsettled operations. Unknown material source scope, unsupported relevant visuals and unresolved required values block completion. They can still leave a downloadable partial draft. Every declared output specification must have current validation.

Configure `DI_TOOL_TIMEOUT_SECONDS` for the killable parser/writer deadline; requests and total job time remain shared final limits.

Current focus and the latest useful result appear in Activity; region/accounting and context-method/fixed-cost details appear in Diagnostics. `/api/jobs/{id}/plan` and `/regions` expose bounded saved state. Diagnostic events include application/index/accounting versions, safe configuration settings, actor, work focus and estimated/reported input usage.

Provision tokenizer data explicitly during setup (the runtime never downloads it):

```bash
.venv/bin/python scripts/provision_tokenizer.py --download
# Offline enterprise setup: verify and copy an approved encoding asset.
.venv/bin/python scripts/provision_tokenizer.py --from-file /approved/o200k_base.tiktoken
```

Set `TIKTOKEN_CACHE_DIR` consistently if using a custom cache. `DI_TOKENIZER_FILE` can point the launcher at an approved offline asset. Checksums are verified before installation. Startup/readiness diagnostics report the active estimation method; `o200k_base` remains an estimate for enterprise aliases. Fixed schema/instruction cost is measured separately; result clearing precedes history replacement.

Embedded raster images are extracted as local artifacts with sheet/anchor metadata and can be inspected through `view_page`. Relevant native drawings/charts without a supported reader remain explicit limitations. A local LibreOffice conversion spike rendered the synthetic fixture, but clipped a header and omitted hidden-sheet presentation; it is not enabled as a deployment dependency. Windows provisioning/fidelity and representative enterprise acceptance remain external acceptance work. Native Excel recalculation and universal advanced-feature preservation remain unsupported.

Run the region, supervision, interruption and actual-runtime fixture tests without model calls:

```bash
.venv/bin/python -m pytest tests/test_goal_harness.py
```

The conservative pilot defaults are: reconcile all potentially material source scope; treat required destination fields/checks and unresolved material questions as completion blockers; support embedded raster evidence and explicitly account for other visual features. These defaults do not establish production accuracy on arbitrary workbooks or replace representative, hand-checked Windows acceptance.

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
