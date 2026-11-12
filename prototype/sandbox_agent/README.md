# Sandbox agent: command line, test packets and experiments

The agent itself now lives in the main app, in [app/engine](../../app/engine). This folder runs that same engine from the command line. It also holds the synthetic test packets (the scorecard), the scorer and the team-shape experiments.

## What the agent does

A job is a **goal**, a **zip of whatever files the user has**, and one or more **deliverables**. Each deliverable is either a template to fill or a written format to build to. The agent works in an OpenAI Code Interpreter sandbox (a short-lived Python computer OpenAI runs) and decides its own path:

1. It reads the goal card. That is one brief built by the harness from the goal, the module's house rules, the template's own rules or the format, and what done means.
2. It maps the files. Each file gets a decision: used, reference, not relevant or unclear, with a reason. Large packets can be split across parallel **scouts**.
3. It builds the deliverables with pandas and openpyxl.
4. It asks the user **one specific question** when the documents cannot settle a material fact.
5. It calls **save_outputs**. That copies everything to the user's machine and checks it there:
   - template unchanged
   - dropdowns and number cells respected
   - the format and the job's rules followed, such as "journal balances"
   - every file decided
   - every written row naming its source files

   The agent fixes what the check lists and saves again.
6. Optionally, an **independent reviewer**, a fresh agent with no memory of the work, looks for results that are valid but wrong. The lead agent settles its doubts.

**Team shapes:**
- **A:** lead agent only.
- **B:** lead agent with scouts.
- **C:** B plus the reviewer.

## Use it

```bash
cd prototype/sandbox_agent && python3.12 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m sandbox_agent packets runs/packets                         # build and self-test the 6 test packets
.venv/bin/python -m sandbox_agent run --job runs/packets/payment_journal --shape C
.venv/bin/python -m sandbox_agent run --zip my.zip --template t.xlsx --goal "Prepare ..." --ask console
.venv/bin/python -m sandbox_agent experiment --packets runs/packets --shapes A,B,C --repeats 2
```

**The test packets:**

| Packet | What it tests |
|---|---|
| `payroll_consolidation` | Five entities, five languages and layouts, a superseded draft, leavers, FX |
| `payment_journal` | A format deliverable with no template, approval and on-hold rules, credit notes, a balanced journal |
| `payroll_instructions_question` | Emails, PDFs and Word files, plus one real conflict the agent should ask about |
| `payroll_noise` | 28 files, only 5 relevant, a hidden sheet, a nested zip, a duplicate change |
| `ap_invoice_upload` | Invoice PDFs and a portal export that overlaps them, a cancellation, a credit note |
| `large_packet` | 122 files and 55 MB; only 2 registers matter |

Each packet comes with an answer key the agent never sees. `packets` proves each key is consistent: its perfect output passes every check and scores 100%.

**Run folders:**
- `run` writes `runs/<id>/`: inputs, inventory, goal card, outputs, scout profiles, every script, events, transcript, `run.json` and `score.json`.
- `experiment` writes `runs/experiments/<id>/summary.md`, with averages per shape and per packet.

**Exit codes:** 0 complete, 2 partial or stopped, 1 failed.

**Options:**
- `--shape A|B|C`, or `--max-scouts N` and `--review/--no-review` to set the parts yourself
- `--proof row|none`
- `--effort`, `--max-turns`, `--timeout`, `--memory`
- `--base-url`, `--model`

**Settings and data:**
- **Settings** are read like the main app's: `OPENAI_API_KEY`, `OPENAI_BASE_URL` and `OPENAI_MODEL` come from the project `.env`. `python -m sandbox_agent settings` shows what a run would use.
- **Paid and remote:** live runs are paid and upload the zip to OpenAI's sandbox. Real payroll files were approved for OpenAI sandboxes on 12 November 2026; keep the run folders out of synced folders, because they hold copies of the inputs and outputs.

## Through the main app

`app_parity.py` sends template packets through the real app on the sandbox engine. It answers the agent's questions as the packet scripts them, and scores the result. Run it with the main app's Python:

```bash
.venv/bin/python prototype/sandbox_agent/app_parity.py --only payroll_consolidation,payroll_noise
```

## Through a LiteLLM proxy

On the Windows enterprise machine, follow [WINDOWS.md](WINDOWS.md). `proxy_check.py` checks, step by step, that an endpoint supports what the agent needs.

- **What the proxy must allow:** sandbox calls must pass through to OpenAI or Azure.
- **Region:** the model alias must be in the same region as the endpoint, or the sandbox is "not found".
- **Optional settings file:** `sandbox_settings.json` (copy `sandbox_settings.example.json`) holds the LiteLLM options, `provider` and `model_on_create`. It can also override the endpoint or the model for this prototype only.

Offline tests (no network): `.venv/bin/python -m pytest -q tests`.
