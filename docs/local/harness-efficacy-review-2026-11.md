# Harness efficacy review and OpenAI Code Interpreter research

Date: 12 November 2026
Branch: `simplify-harness` (at `4f5b373`)
Status: Review only. No code changed.
Follows: [harness simplification review](harness-simplification-review.md) (21 October) and [run_python prototype](python-tool-prototype.md).

## 1 Verdict

The tools break mostly because of the harness's paperwork rules, not because the code is broken. In the saved runs, 9 of every 10 tool failures are the harness refusing a call, not a tool crashing.

Swapping in OpenAI's Code Interpreter would not fix this by itself. The refusals come from the accounting rules around the tools, and those rules would still apply. Code Interpreter is worth one short, measured experiment. It is not a drop-in replacement.

Recommended order:

1. Fix the five real bugs.
2. Stop refusing calls the harness can repair itself. Keep the strict check only at the end, in `validate_output`.
3. Make one Python tool able to finish a job, so most of the 30 narrow tools can go.
4. Run Code Interpreter as a side-by-side experiment on the same test packets, after the data question in section 5 is settled.

## 2 What the saved runs show

Source: `data/jobs.db`, 16 runs that reached the model (18–21 October, all `gpt-6-luna`, all on the 2-employee synthetic packet). There are no saved runs after 21 October, and `run_python` has never run live.

| | 18 Oct (old harness) | 20 Oct (goal harness) | 21 Oct (simplified) |
|---|---|---|---|
| Runs | 5 | 9 | 2 |
| Finished | 5 | 7 (2 paused) | 2 |
| Model requests per run | 7–12 | 11–29 | 15–20 |
| Tool failures per run | 0–2 | 2–6 | 3–5 |

The 21 October simplification removed the pauses. Runs still take about twice as many requests as on 18 October, and every one still hits several refusals.

### The 50 failed tool calls, by cause

| Cause | Count | Examples |
|---|---|---|
| **Rule refusals.** The call was reasonable but broke a harness rule. | 32 | `resolve_visual` on a standalone image: "use record_decisions instead" (10). `update_work_plan` "cites no saved result" (9). `save_output_requirements` on the template, or before `create_output`, or without an evidence ID (6). `save_interpretation` "cite its evidence_id; filenames are insufficient" (4). `extract_region` "account for the complete accepted region" (2). `record_decisions` "inclusion is established by writing output" (1). |
| **Wrong name, ID or path.** The model referred to something in a form the tool did not accept. | 13 | "Unknown inventory item" / "Unknown source" (5). "Source must appear in the job inventory" (4). "Unknown dataset" (3). A bare filename instead of the full path (1). |
| **Real code bugs.** | 5 | `read_sheet`: `'EmptyCell' object has no attribute 'coordinate'` (3). `populate_template`: `decimal.ConversionSyntax` (1). `read_job_state`: `'list' object is not a mapping` (1). |

The `update_work_plan` refusals were removed in commit `e44cfc6`. The other rule refusals are still in the code.

### Why this happens

The harness asks the model to keep a set of books while it works: every claim must cite an evidence ID, every source must be listed in the inventory, every region must be accounted for in full, and requirements must be saved against an output copy before writing. These are good goals. But each rule is enforced at the moment of the call, by a different tool, with its own ID format. That gives the model about 30 places to trip, and each trip costs a round trip.

Specific friction in the current code:

- **30 tools, about 5,600 tokens of tool descriptions** sent with every request. The largest are `transform_dataset` (2,153 characters), `save_output_requirements` (1,975) and `populate_template` (1,699).
- **Three different ID systems** (inventory IDs, evidence IDs, dataset and observation IDs). The model must copy the right one into the right tool. "Unknown …" errors are this.
- **Order rules.** Some tools must come before others (`create_output` before `save_output_requirements`; inspect before interpret; view before observe). The prompt does not state these. The model learns them by failing.
- **Two tools for one idea.** Images inside workbooks go to `resolve_visual`; image files go to `record_decisions`. The model guessed wrong 10 times in 16 runs.
- **The Python tool cannot finish a job.** Cells it writes carry no source record, so `validate_output` fails until the same data goes back through `populate_template`. It is also off by default, macOS only, and pandas is not installed. So it adds a 30th tool without removing any.

## 3 What OpenAI's Code Interpreter is

Code Interpreter is a Python sandbox that OpenAI runs. The model writes code; OpenAI runs it in a "container" (a short-lived private computer) and returns the output and any files made. It is a built-in tool of the Responses API, which this app already uses. The agent library here (Pydantic AI 2.57) supports it as a native tool, so adding it is a small code change.

| Topic | What the docs say | Source |
|---|---|---|
| Setup | Tool type `code_interpreter`. The container is either created automatically per conversation, or created first through `/v1/containers` and passed by ID. | [Code Interpreter guide](https://developers.openai.com/api/docs/guides/tools-code-interpreter) |
| Files in | Upload into the container; `.xlsx`, `.csv`, `.pdf`, `.docx`, `.zip` and others are accepted. | same |
| Files out | Returned as `container_file_citation` notes and downloaded from the container. | same |
| Lifetime | **A container is deleted after 20 minutes without use.** It cannot be revived; its files and memory are lost. | same |
| Memory | 1 GB (default), 4 GB, 16 GB or 64 GB. | same |
| Network | Off by default. | [Shell guide](https://developers.openai.com/api/docs/guides/tools-shell) |
| Price | Per container per 20-minute session: $0.03 (1 GB), $0.12 (4 GB), $0.48 (16 GB). Model tokens are billed as usual. | [Pricing](https://developers.openai.com/api/docs/pricing) |
| Old version | The Assistants API version shut down on 26 August 2026. Only the Responses version remains. | [Deprecations](https://developers.openai.com/api/docs/deprecations) |
| OpenAI's advice | Prefer hosted tools such as Code Interpreter; keep custom tools for business-specific actions. Short prompts that state the outcome and when to stop. | [GPT-5.5 guide](https://developers.openai.com/api/docs/guides/latest-model/gpt-5.5.md) |

**Not confirmed by OpenAI:** the list of preinstalled libraries (pandas and openpyxl are likely, not documented), whether `pip install` works, file size limits, which models support it, and whether it works in background mode.

### Known problems reported by users

- **"Container is expired" (error 400)** once a run sits idle for 20 minutes, for example while waiting for `ask_user`. All work in the container is lost. ([forum](https://community.openai.com/t/container-is-expired-error-in-openai-responses-api-stream/1321773))
- **Missing files.** The model writes a file but no file citation comes back, or it links a file it never made. ([forum, Sept 2026](https://community.openai.com/t/responses-api-code-interpreter-with-gpt-4-1-sometimes-creates-mnt-data-docx-but-returns-no-container_file_citation/1397663))
- **Background mode** can drop the code outputs. ([forum](https://community.openai.com/t/responses-api-background-true-drops-code-interpreter-call-outputs-even-when-retrieved-with-include/1372536))
- **Hung calls** on Azure. ([Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/5784203/openai-responses-api-calls-code-interpreter-tool-h))

### Two newer OpenAI options that fit better

- **Agents API hosted sandboxes (beta).** Same idea, but idle timeout is **1 hour**, and files saved in `/workspace/outputs` **stay downloadable after the sandbox ends**. That removes Code Interpreter's two biggest problems. ([docs](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted))
- **Agents SDK sandboxes (April 2026).** Run the sandbox on your own machine with Docker, or with a hosted provider. Data can stay local. This would also solve the current Python tool's "macOS only" gap. ([OpenAI blog](https://openai.com/index/the-next-evolution-of-the-agents-sdk/), [SDK docs](https://openai.github.io/openai-agents-python/sandbox/clients/))

## 4 Would Code Interpreter help the model finish?

**What it would improve**

- The model is trained heavily on this exact tool. It writes pandas and openpyxl code with fewer mistakes than it makes with 30 home-made tools and three ID systems.
- One tool can read, transform and write. Most of the reading and dataset tools could go.
- No sandbox to build or maintain on Linux and Windows.

**What it would not fix, or would make worse**

- **The rule refusals stay.** They come from the accounting tools, not from the lack of a sandbox. A Code Interpreter run would still have to cite evidence IDs and push data through `populate_template` to pass validation, unless the accounting is redesigned (section 6, step 3).
- **Your final check cannot see inside the container.** `validate_output` re-hashes sources and compares cells on this machine. The finished workbook must be downloaded and checked here every time, and the source of each value must be reported back.
- **Lost work on pauses.** Any `ask_user` wait or slow resume over 20 minutes loses the container. The app's pause-and-resume design assumes work survives. Every resume would need to re-upload all files.
- **More data leaves the machine.** Today only what the model reads reaches OpenAI. With Code Interpreter, whole payroll files are uploaded into OpenAI's containers.
- **Enterprise gateway.** The Windows enterprise setup points `OPENAI_BASE_URL` at a company gateway. That gateway would need to support Code Interpreter and containers. Unknown.

## 5 Decisions only you can make

1. **Data policy.** May whole payroll files be uploaded into OpenAI-run containers? Zero Data Retention support for Code Interpreter is not confirmed; a 2025 report says it was switched off under it ([forum](https://community.openai.com/t/responses-api-does-not-support-code-interpreter-tool-if-zero-data-retention-enabled/1320875)). Ask OpenAI or your account contact before any real data goes in.
2. **Gateway.** Does the enterprise gateway support the Code Interpreter tool and the containers endpoints?
3. **Where the evidence trail lives.** Today the harness proves where every cell came from, cell by cell. A code-first design would prove it at the end instead: the script's output and a source column per row, checked by `validate_output`. Is that level of proof acceptable for payroll?

## 6 Recommended plan

Change one thing at a time and rerun the same test packets after each step, as the October review advised.

1. **Fix the five real bugs** (`EmptyCell` in `read_sheet`, the decimal conversion in `populate_template`, the list/mapping error in `read_job_state`). Small and certain.
2. **Repair instead of refuse.** When the harness can tell what the model meant, do it and say so in the result. Examples:
   - an image file sent to `resolve_visual`: record it as a decision;
   - requirements saved against the template: apply them to the output copy, creating it if needed;
   - a file path where an ID is expected: look up the ID;
   - a missing evidence ID: use the latest inspection of that file.

   Keep the strict check only in `validate_output`. This targets about 45 of the 50 failures above.
3. **Let one Python tool finish a job.** Give `run_python` a way to report the source record for each cell it writes. Then `validate_output` can check Python-written cells the same way it checks `populate_template` cells. Install pandas. Then remove the reading and dataset tools it replaces, aiming for about 10 tools.
4. **Code Interpreter experiment, in parallel with step 3, only after decision 1.** Use synthetic packets only. Give the model Code Interpreter plus `ask_user`, `create_output` and `validate_output`, and compare it against the local tool on completion, cell accuracy, requests, tokens and time. This cheaply shows how far a code-first design can go before more work goes into the local sandbox. If it wins, prefer the Agents API sandbox (1-hour idle, outputs survive) or a local Docker sandbox over plain Code Interpreter.
5. **Build the scorecard first.** All of the above needs 5–10 realistic, anonymised packets with checked answers. Two employees cannot show what breaks. This is still missing from the October review.

## 7 Limits of this review

- All saved runs use one tiny synthetic packet. The real-world breakage you see may have other causes. Real job logs or Diagnostics exports from failing runs would sharpen this a lot.
- No runs exist after 21 October, so the 2 November change (`4f5b373`) is not measured.
- Code Interpreter facts come from OpenAI's docs and user forums as of November 2026. Items marked "not confirmed" need a quick test.
- `DI_CONTEXT_CHAR_LIMIT=160000` is still in `.env` but the code no longer reads it. It is harmless and can be deleted.
