# Sandbox agent prototype

A deliberately small document agent. The model does the work in an **OpenAI Code Interpreter** sandbox (a short-lived Python computer OpenAI runs) using pandas and openpyxl. Everything it receives, runs and produces is saved in a local run folder.

The loop is the **OpenAI Agents SDK** (`openai-agents` 0.24). This harness adds only:

| Piece | Why |
|---|---|
| A sandbox holding the uploaded zip and template | The model explores and builds with code instead of 30 narrow tools |
| `scout_folders` | Profiles several folders at once, each in its own sandbox, and returns structured file profiles |
| `save_outputs` | Copies everything under `/mnt/data/outputs/` to the run folder and checks it on this machine. The agent fixes what it reports. |
| Limits | Turns, wall-clock time, number of scouts. A keep-alive stops the sandbox expiring after 20 idle minutes. |
| Run folder | The local record. See [store.py](sandbox_agent/store.py) for the layout. |

The local checks never see the answer key, and they work with any template. They check that everything the template already held is unchanged (sheets, cells, formulas, hidden sheets), that the template's own data-validation rules hold (dropdown lists, number-only cells), that no table gains duplicate rows, and that every packet file appears in `document_hierarchy.json`. To make a template stricter, add data-validation rules to it in Excel.

## Use it

It has its own Python environment so it does not touch the main app:

```bash
cd prototype/sandbox_agent && python3.12 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
```

Generate the synthetic test packet (12 files, five entities, 215 expected rows; see [packet.py](sandbox_agent/packet.py) for what makes it messy):

```bash
.venv/bin/python -m sandbox_agent fixture runs/fixture
```

Run the agent and score it against the answer key:

```bash
.venv/bin/python -m sandbox_agent run --zip runs/fixture/packet.zip --template runs/fixture/output_template.xlsx --answer-key runs/fixture/answer_key.json
```

Settings are read like the main app's: `OPENAI_API_KEY`, `OPENAI_BASE_URL` and `OPENAI_MODEL` from the project `.env`. `python -m sandbox_agent settings` shows which endpoint and model a run would use and where each came from. If no endpoint is set anywhere, the run refuses rather than defaulting to public OpenAI. When the endpoint is public OpenAI, every command prints a warning, so a machine that should use the company proxy cannot use OpenAI unnoticed. With no `OPENAI_MODEL`, the default is `gpt-6-luna` rather than the main app's `gpt-5.4-mini`, because it is the model these runs were tested with in the sandbox. The address must use HTTPS, or plain HTTP only to this machine, as in the main app. The run exits with 0 when complete, 2 when partial or stopped, and 1 when it failed.

Useful options: `--effort low|medium|high`, `--max-scouts 0` (no parallel scouts), `--model` and `--base-url` (override `.env` for one run), `--max-turns`, `--timeout`.

Live runs are paid and upload the zip to OpenAI's sandbox, where it is deleted with the sandbox at the end of the run. Responses are sent with `store=False`, so OpenAI keeps no copy of the conversation. **Use synthetic packets only until real payroll data is approved for OpenAI containers.**

## Through a LiteLLM proxy

On the Windows enterprise machine, follow [WINDOWS.md](WINDOWS.md). Elsewhere:

The model calls and the sandbox calls share one client, so the endpoint in `.env` carries both. First check that the proxy supports every step the agent needs:

```bash
.venv/bin/python proxy_check.py
```

`sandbox_settings.json` (copy `sandbox_settings.example.json`) is optional. It holds the LiteLLM options the main app has no setting for: `provider` (`openai` or `azure`) if the proxy picks the provider by header, and `model_on_create` if its credentials sit in `model_list`. It can also override `base_url` or `model` for the prototype only. Flags override everything for one run. Set `PROXY_API_KEY` to use a key other than the one in `.env`.

The agent needs the proxy to pass sandbox calls through to OpenAI or Azure. LiteLLM's other mode, which swaps Code Interpreter for an E2B or OpenSandbox sandbox, cannot upload or download files yet, so the check fails at step 2 or 5.

Offline tests (no network): `.venv/bin/python -m pytest -q tests`. Cheap live check of an endpoint: `proxy_check.py` (above).
