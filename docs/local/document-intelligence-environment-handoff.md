# Document Intelligence: macOS development and Enterprise Windows handoff

Prepared 17 October 2026 for the AI agent building Document Intelligence.

## Read this first

Build on the local Mac, distribute application code through the approved GitHub repository, and run a separate checkout on Enterprise Windows. Keep credentials, documents, databases, logs and generated outputs local to each machine. Git updates code; it does not synchronize application data or installed runtimes.

This handoff records lessons from `/Users/user/Desktop/xbrl-agent`, inspected at commit `4cd0d63` with uncommitted changes present. It describes that working tree, not a clean release or a fresh Windows certification. The destination folder is `/Users/user/Desktop/Document Intelligence` and was empty before this handoff was created.

The user's reference to “true LLM” is interpreted here as **LiteLLM**, the gateway used by the existing project. The enterprise service is an OpenAI-compatible GenAI gateway. Its precise managed implementation and organizational access policies have not been independently verified.

Use the established fixes below without repeating old investigations. Perform a small acceptance check for the new application because permissions, installed software and gateway access can differ. No Windows session or live model request was executed while preparing this document.

## 1. Environment and deployment model

| Area | Local Mac | Enterprise Windows |
| --- | --- | --- |
| Role | Development, synthetic fixtures, automated tests | Enterprise execution and native Office acceptance |
| Application | Python backend; existing project uses FastAPI and Uvicorn with React/Vite | Same application source, rebuilt for Windows |
| Python environment | `venv/bin/python` | `venv\Scripts\python.exe` |
| Existing interpreter gate | Python 3.10+; startup prefers 3.12 | Python 3.10+; launcher and common install paths searched |
| LLM connection | Local LiteLLM at `http://localhost:4000/v1`, or direct provider API | Enterprise gateway at `https://genai-sharedservice-emea.pwc.com/v1` |
| Provider credentials | Separate real OpenAI, Anthropic or Google keys | Enterprise gateway credential supplied locally |
| Certificates | OS trust where required; loopback proxy uses HTTP | Corporate root certificate must be trusted through the Windows certificate store |
| Word-to-PDF | LibreOffice headless | Installed Microsoft Word through `docx2pdf` / COM |
| Frontend build | Node/npm installed locally | Node/npm installed locally, sometimes outside PATH |
| Data | Local test data and local runtime state | Enterprise documents and runtime state stay within the enterprise boundary |

These are existing project choices, not a requirement to copy its financial-statement architecture. Document Intelligence does not inherit XBRL taxonomies, filing templates, accounting rules or mTool integration unless requested.

## 2. GitHub delivery and updates

Implement separate `start.sh` and `start.bat` entry points with equivalent behavior. Resolve the project directory from the script location before doing any work. Quote paths: the project name contains a space. In batch files use `call` when invoking another `.bat`/`.cmd`, including `npm`, so execution returns to the startup script.

Commit application source, dependency declarations, Python constraints or lock files, `package-lock.json`, placeholder-only `.env.example`, tests and maintained setup instructions. Install dependencies on each machine. Keep `venv/`, `.venv/`, `node_modules/`, build caches, runtime settings and generated outputs out of Git.

Suggested ignore entries for the new repository:

```gitignore
.env
.env.*
!.env.example
venv/
.venv/
__pycache__/
.pytest_cache/
node_modules/
dist/
*.tsbuildinfo
output/
data/
logs/
*.log
*.db
*.db-wal
*.db-shm
~$*
docs/local/
.DS_Store
```

Store runtime state under a configurable writable directory. Back it up separately. An ignore file does not remove files already tracked; inspect the staged diff before publishing.

For initial Windows setup, use an approved HTTPS clone URL and a writable destination. The repository URL, branch, Git authentication and permitted GitHub access must be supplied or discovered from approved local configuration. Do not embed a token in a clone URL or script. No repository has been created or pushed as part of this handoff.

For updates, document this sequence for the Windows operator or agent:

1. Stop the application and background jobs. Back up durable data before an update that migrates it.
2. Run `git status --short`, `git branch --show-current` and `git rev-parse HEAD`. Preserve local changes; stop if they conflict with the update.
3. On the intended deployment branch, run `git pull --ff-only`. A failure requires inspection, not an automatic hard reset or clean.
4. Run the Windows startup entry point. Reinstall changed dependencies, rebuild changed frontend assets and apply idempotent database migrations.
5. Confirm the new commit, application readiness, frontend asset loading and persisted data access.

If GitHub access is blocked, use the organization's approved delivery method. Do not infer total GitHub unavailability from the separate blocked LiteLLM pricing fetch described below.

Startup setup should be cached by dependency declarations, lock files, interpreter identity and frontend inputs, with missing outputs invalidating the cache. Write success markers only after successful steps. A backend process starting while the frontend is absent or stale must be reported clearly.

The existing `start.sh` kills whatever owns port 4000 and falls back to direct APIs when the local proxy fails. These are behaviors to improve when adapting it: use a separate configurable port or verify process ownership, and make the selected route visible. Windows enterprise mode should fail clearly if its required gateway is unavailable.

## 3. LLM configuration and transport

### Separate gateway credentials from provider credentials

Use `LLM_PROXY_URL` and `LLM_PROXY_API_KEY` for gateway routing. For direct access, use `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY` / `GOOGLE_API_KEY`. Keep credentials in local environment configuration, never browser bundles.

The old application also accepts its enterprise gateway credential through the historically named `GOOGLE_API_KEY`. That does **not** make it a Google credential. Prefer an explicit gateway key in the new application and avoid carrying this naming ambiguity forward.

Example Windows environment configuration; supply the credential locally:

```dotenv
LLM_PROXY_URL=https://genai-sharedservice-emea.pwc.com/v1
LLM_PROXY_API_KEY=<enterprise-gateway-credential>
HOST=127.0.0.1
PORT=8003
SESSION_SECRET=<separate-random-session-secret>
```

Port 8003 is a proposed new-project value to avoid the existing application's 8002. The new application must implement these settings before this example is executable. The enterprise host is internal environment metadata; remove or parameterize it before sharing this document outside the approved audience.

On the Mac, use `LLM_PROXY_URL=http://localhost:4000/v1` with a local-only gateway key if running a local LiteLLM proxy. Its upstream provider keys remain separate. Never reuse a local development gateway key on Enterprise Windows.

### Match the protocol to the model

The existing factory is `server.py::_create_proxy_model()`. It is shared by web and CLI. Apply the same single-factory approach to every Document Intelligence LLM feature, including previews, conversion helpers, reviewers and background jobs.

| Case | Established application behavior |
| --- | --- |
| OpenAI through a proxy | `OpenAIResponsesModel` using `/v1/responses`; preserve the full gateway model alias |
| OpenAI direct | Responses API with the upstream model ID and `OPENAI_API_KEY` |
| Anthropic through a proxy | `OpenAIChatModel` using Chat Completions |
| Anthropic direct | Native `AnthropicModel` with `ANTHROPIC_API_KEY` |
| Gemini through the Mac local proxy | Bypass to native `GoogleModel` with a real Google key to preserve tool-call signatures |
| Gemini 3 through the enterprise OpenAI-compatible route | Refuse by default; signature preservation is unproven in this path |

Enterprise Responses support was operator-confirmed in the existing project's records on 25 September 2026. The current code always uses Responses for OpenAI. Older `config/models.json` notes still mention `XBRL_OPENAI_RESPONSES=1`; that flag is retired and those notes are stale. Follow the current factory and transport tests.

The existing default is `openai.global.gpt-6-luna`. This is an application registry value, not proof that the new project's credential has access. Gateway aliases such as `openai.global.…`, `bedrock.anthropic.…` and `vertex_ai.…` are not interchangeable with direct-provider IDs. Map direct IDs explicitly; do not guess by deleting arbitrary prefixes. Keep model availability and reasoning support in one registry and treat gateway permissions as a deployment input.

### Known LLM failures and established corrections

- **Direct Google from Enterprise Windows:** observed HTTP 403. Keep enterprise traffic on the approved gateway; a Mac-only native Google fallback is not a Windows remedy.
- **Gemini 3 tool calls through the generic gateway path:** failure on a later turn with missing `thought_signature`. A successful text response or first tool call does not prove compatibility. The existing `XBRL_ALLOW_GEMINI_PROXY` override is an evidence-gated escape hatch, not a fix. Keep the route blocked until a multi-turn tool exchange proves preservation.
- **Gemini 3 temperature:** the project records failures or looping below `1.0` through the proxy. Retain the established model-specific setting; do not apply one low temperature to every model.
- **OpenAI tool calling:** the project records a GPT-5.6 Chat Completions incompatibility when reasoning is enabled or omitted. Its correction is Responses. Do not recreate a generic Chat-only client for all model families.
- **Reasoning controls:** accepted values differ by model. The existing registry excludes `none` and `minimal` for GPT-6 Astra and excludes `minimal` for Sol/Luna. In the installed PydanticAI API, unified thinking-off is `False`; blindly passing the string `none` to every provider fails.
- **Cache controls:** the existing implementation separates legacy `prompt_cache_retention` values from `prompt_cache_options.ttl`, whose supported value in that integration is `30m`. Sending legacy `24h` as the new TTL produced HTTP 400. Port these settings only for a supported, checked model and gateway route.
- **Cost reporting:** upstream list prices and cached pricing maps are estimates, not confirmed enterprise billing. Label estimates and keep usage distinct from cost.

## 4. Windows network trust and diagnostics

The existing fix for corporate TLS failures is:

```python
import truststore
truststore.inject_into_ssl()
```

Run this in the application entry point before creating network clients or SSL contexts, including standalone CLI/helper entry points. The corporate root CA must already be installed in the OS trust store. Installing `truststore` cannot create a missing or unapproved root certificate. Keep certificate verification enabled.

Distinguish these two symptoms:

| Symptom | Meaning and response |
| --- | --- |
| LiteLLM cannot fetch its remote model cost map because of SSL/firewall restrictions | Existing integration falls back to bundled pricing; this alone does not establish an LLM outage |
| Actual gateway call raises `httpx.ConnectError` / `CERTIFICATE_VERIFY_FAILED` / missing issuer | Real connection failure; verify OS trust setup, early truststore initialization and the active venv |

For a new failure, classify TLS/DNS/network errors separately from 401/403 credential or permission errors, 404 route/model errors, 400 request-shape errors and 429 rate limits. Retry only transient failures with bounded backoff. Surface a useful error without printing credentials or source content.

Do not assume pip, npm, model downloads, public CDNs or telemetry endpoints are reachable just because the gateway works. Prefer locally bundled frontend assets and identify any download needed at runtime. The inspected source does not establish blanket bans on Docker, WSL, PowerShell, administrator rights or package installation; treat those as unconfirmed machine capabilities.

## 5. Python, Node and dependency constraints

- Invoke the venv interpreter explicitly for app commands, pip and tests. Activation is shell-local. macOS system Python can be unsupported even when another terminal shows an activated environment.
- Set `PYTHONUTF8=1` before starting Python on Windows. Use explicit UTF-8 when reading/writing text. The existing server reconfigures stdout/stderr **in place**; replacing wrappers around shared stream buffers caused reload/capture failures.
- Windows Python and Node may be installed without PATH entries. Existing Node fallback: `C:\Program Files\nodejs\`. Discover installed runtimes before proposing a reinstall. Check both `node` and npm.
- Record one chosen Python and Node version for the new application and validate them on both machines. The old Python 3.10 floor is not proof every current pinned dependency supports every newer interpreter.
- Install Python dependencies with constraints, for example `venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints.txt`, once those files exist. For a committed npm lock file, use `npm ci` for reproducible deployment; the old launcher currently uses `npm install`.
- The existing Python constraints are Mac-generated. Its `docx2pdf` dependency has a Windows marker but is absent from the constraints, so Windows installation is unpinned. Resolve and pin Windows-only dependencies for the new project. A Mac freeze is not a complete Windows lock.
- Keep platform-only dependencies conditional. Do not copy the old large constraints file wholesale: it includes unrelated and platform-specific packages. Constraints restrict selected packages; they do not install every listed package.
- Avoid importing large OCR/ML stacks by default. The old project moved Word conversion to native Word/LibreOffice and lightweight HTML extraction. No offline model-cache or enterprise GPU capability was established by this inspection.

The inspected pins include PydanticAI `2.9.0` and truststore `0.10.4`. These identify the source baseline, not a requirement to use those exact versions in a new project.

If using PydanticAI, preserve the relevant established API patterns: construct models with `provider=`, read `.usage` as a property, use `agent.iter()` / node streams for events, and use `ProcessHistory` capabilities rather than removed history-processor constructor arguments. Set `end_strategy="early"` for tool agents. Give agents explicit turn/request/time limits; the existing default request ceiling is 50, so a lower application cap must fire first or an explicit SDK limit must be supplied.

## 6. Document conversion and native Office limits

The established input design retains the original `.docx`, converts once to PDF and uses that PDF for page references and review. PDF uploads retain their original bytes. This is reusable if Document Intelligence needs page-grounded extraction.

On Windows, conversion uses installed desktop Word via `docx2pdf`; pip installation alone does not install Word. Run automation in a child process with a timeout because a Word dialog, Protected View or corrupt document can block a COM call. Validate that a readable output PDF exists before reporting success. A headless Windows service account running Office automation has not been certified here.

On macOS/Linux, use an installed LibreOffice binary. Give each headless conversion an isolated LibreOffice profile and a timeout. Pass only the environment variables the converter needs, excluding provider/session secrets. Validate genuine OOXML input before invoking the converter. Conversion failure should offer the established fallback: save as PDF in Word and upload the PDF.

Fonts, pagination and native Office rendering can differ. Mac parser tests cannot certify Windows Word/Excel output. If spreadsheet output is added, remember that openpyxl writes formulas but does not calculate them. Excel/mTool compatibility requires native acceptance; the existing mTool integration preserves package structure with a specialized standard-library patcher rather than reserializing the workbook with openpyxl. Only bring that constraint into scope if manipulating mTool files.

## 7. Files, persistence and background work

- Use `pathlib`, absolute paths derived from application configuration, and argument lists for subprocesses. Test paths with spaces and Unicode. Validate both slash forms in user-supplied path components.
- Sanitize generated filenames for Windows. Existing agent IDs containing colons caused trace writes to fail silently. Use stable safe IDs; do not derive paths directly from model output or source titles.
- Write complete files to same-directory temporary paths, close handles and publish by atomic replacement. Serialize writes to shared workbooks. On Windows, readers, Office, scanners or sync clients can deny replacement temporarily; use bounded retries for known transient errors such as Windows 5/32/33, and preserve the previous destination on failure.
- Separate runtime data from source updates. Migrations should advance sequentially and be idempotent. Do not synchronize a live SQLite database by Git or treat deleting it as an update procedure.
- Persist a job record before work starts, preserve partial results where useful and always reach a terminal status after failure or cancellation. Include conversion, gateway waits and final validation in progress reporting.
- Background threads with separate `asyncio.run()` calls own separate event loops. Process-global async clients, futures and semaphores require explicit loop ownership. Existing fixes use per-loop semaphores, `concurrent.futures.Future` with `asyncio.wrap_future` for cross-thread sharing, and `loop.call_soon_threadsafe` for cancellation.
- Stream logs and traces can contain document text and model responses. Keep them within the approved data boundary and apply retention; removing binary payloads does not make a trace safe to share.

## 8. Authentication and enterprise data boundary

Default a workstation application to loopback (`127.0.0.1`). If network sharing is needed, design authentication and server-side authorization before exposing it. Use a distinct persistent session secret, protect API routes and keep any development login bypass limited to explicitly approved local testing.

The existing Windows investigation instructions require synthetic or formally approved fixtures, retention of documents/workbooks/screenshots/traces inside Windows, and sanitized plain-text reports leaving that environment. Treat that as the inherited handling baseline until the user supplies the new project's policy. The approval status of this new repository and of sending new document classes to the gateway remains unconfirmed.

Do not move enterprise documents to the Mac or GitHub to diagnose a bug. Reproduce with synthetic data and return content-free observations. Organization-specific rules and approvals take precedence over a technical capability to transfer files.

## 9. Build sequence and acceptance criteria

1. Inspect the new folder and its instructions. Create the smallest app matching the user's product request, with separate source and runtime directories. Completion: repository structure and startup contract are documented.
2. Implement equivalent Mac and Windows launchers, explicit interpreter use, UTF-8, certificate initialization and repeatable dependency installation. Completion: offline startup and configuration validation work without calling a paid model.
3. Implement one LLM factory with explicit deployment mode, credentials, model alias, transport, limits and visible route selection. Completion: offline tests cover direct/proxy routing, credential separation, Responses selection and refusal of unsupported Gemini routing.
4. Add document handling only as needed. Completion: synthetic PDF/Word fixtures cover success, conversion failure/timeout, safe paths, locked-file failure and durable output publication.
5. Prepare a Windows acceptance run using the exact application commit. Record Python/Node/package versions and pass/fail/blocker for startup, UI assets, Unicode paths, conversion, persistence across restart and a Git update preserving local state.
6. With user authorization for paid/live calls, test only the chosen gateway model: a small text request, image input if needed, a multi-turn tool exchange, structured output if used, streaming and cancellation if used. Completion: record the actual gateway route and outcome. A text-only ping is insufficient for an agent workflow.

Readiness endpoints must avoid sending inference calls. The existing Mac launcher uses `/health/readiness`; LiteLLM's broader `/health` endpoint was found to ping configured models and spend tokens. An application opening successfully does not establish gateway compatibility.

Do not repeat a broad model comparison or investigate known fixed failures. Repeat narrow checks when a relevant dependency, model, gateway route or Windows installation changes. Report unverified capabilities as unverified.

## 10. Source map and evidence limits

These paths are relative to the existing `xbrl-agent` repository. They are optional references available on this Mac; the new app must not require that checkout at runtime.

| Topic | Inspect when implementing or troubleshooting |
| --- | --- |
| Provider routing, signatures, early trust and output encoding | `server.py::_create_proxy_model`, `_warn_if_gemini_loses_thought_signatures`, startup imports; `tests/test_provider_routing.py`, `tests/test_gpt56_transport.py`, `tests/test_server_windows_streams.py` |
| Reasoning, temperatures and cache request shapes | `model_settings.py`; `tests/test_thinking_levels.py`; `CLAUDE-REFERENCE.md` sections 2, 5, 6, 18 |
| Startup, version gates and setup caching | `start.sh`, `start.bat`, `scripts/startup_cache.py`; `tests/test_startup_config.py` |
| Dependency declarations | `requirements.txt`, `constraints.txt`, `web/package.json`, `web/package-lock.json` |
| Word conversion | `ingest/word_convert.py`; `tests/test_upload_docx.py`; `CLAUDE-REFERENCE.md` section 29 |
| Safe paths and publication | `utils/paths.py`, `utils/atomic_io.py`, `utils/workbook_io.py`; `tests/test_atomic_io.py`, `tests/test_agent_tracing.py` |
| Loop ownership | `task_registry.py`; `tests/test_task_registry_cross_loop.py`, `tests/test_page_cache_single_flight.py`; reference section 2a |
| Enterprise artifact handling | `docs/local/handoffs/AGENT-BRIEF-windows-mtool-followup.md` |

Historical missing porting/architecture documents named in the old repository are not required inputs. Prefer the live code and pinning tests above over old plans or catalog prose.

Preparation checks: read the existing Git status/revision, router, relevant invariant sections, configuration, launchers, transport/conversion/file helpers and named test source. Inspect the destination directory; review the resulting Markdown and verify the delivered copies match. No secret configuration file was read for this handoff.

Not executed: application tests, frontend builds, prompt audits and template generators because this delivery changes only documentation; native Windows/Office checks because no Windows environment was available in this session; live/paid LLM checks because they were unnecessary for preparing the handoff and were not authorized.
