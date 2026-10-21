# `run_python` prototype: what it could replace and what it costs in security

Branch `python-tool-prototype`, cut from `simplify-harness`. Step 6 of the [harness simplification review](harness-simplification-review.md). **Off by default** (`DI_PYTHON_TOOL=1` turns it on). Not for production until security approves it.

## What was built

One agent tool, `run_python(code, datasets=[])` ([app/python_tool.py](../../app/python_tool.py)). The model writes Python 3 with openpyxl (pandas too, but only if someone installs it; it is not added to `requirements.txt`). The script runs in a fresh interpreter, and in that interpreter:

| Name | What it is | Access |
|---|---|---|
| `INPUTS`, `TEMPLATE` | the uploaded sources and template | read-only |
| `WORKING` | the job's working folder (state, attachments) | read-only |
| `OUTPUTS` | *copies* of the existing output workbooks | writable, kept only if the script succeeds |
| `SCRATCH` | `working/python/scratch`, kept between runs | writable |
| `DATASETS` | the rows of the datasets the call named, with their sources and evidence | in memory |
| `save_dataset(rows, kinds, excluded)` | saves a derived dataset | checked by the harness |

How it fits the existing harness:

- **Same runner and lock.** `app/execution.py` now has two shared pieces, `acquire_job_lock` and `run_killable`. The existing isolated tools and `run_python` both use them. The job lock is held from staging through commit.
- **Same deadline.** `DI_TOOL_TIMEOUT_SECONDS` applies, plus a CPU limit of the same length inside the child. A script that times out is killed and nothing is saved. That is an ordinary tool error, not a pause, because outputs are staged.
- **Bounded output.** Stdout and stderr are each cut to a quarter of `DI_TOOL_RESULT_BYTES`. The start of stdout and the end of stderr (where tracebacks are) are kept. The pipes are drained without being stored, so a script printing gigabytes cannot exhaust memory.
- **Outputs are staged.** Changed workbooks are committed only when the script exits cleanly, the file still opens, and no interrupted `populate_template` journal exists for it. New files in `OUTPUTS` are refused; `create_output` still makes outputs.
- **Source accounting survives.** A saved dataset is refused if any row cites a source that is not in its input datasets, or if a source from the first dataset is neither kept nor listed in `excluded` with a reason. Lineage covers every input dataset, so a later instruction change still marks Python-derived datasets as stale.
- **The completion gate is unchanged.** `validate_output` still re-hashes every source, compares every assigned cell with its source record, and checks formulas and lost template features. Python edits change the workbook's hash, so it must be validated again. Cells written directly by Python carry no source assignment, so coverage fails until they come from `populate_template` (or `write_cells` with `record`). The intended path is: extract, then `run_python` to build a dataset, then `populate_template`, then `validate_output`.
- **Audit.** Every committed run saves its code, input datasets, changed outputs and resulting dataset to `working/python-runs/<sha>.json`. The full code is also recorded in the tool event.

## What it could replace

| Today | With `run_python` |
|---|---|
| `transform_dataset` and its 10-operation typed language (filter, select, rename, convert, lookup, join, group, deduplicate, arithmetic, unpivot); about 200 lines in `datasets.py` plus the `Transformation` schema in every prompt | One script per transformation. Operations the language never had, such as conditional mapping, date windows, pro-rating, multi-key fuzzy matching or rounding rules, become possible. |
| `query_dataset` with `Predicate` filters | `print()` whatever slice or summary is needed |
| Paging large sheets through `read_sheet`/`read_cell`/`search_cells` for analysis (not as evidence) | Reading a workbook directly and printing a summary in one call |
| Some `resolve_records` / `record_observations` cases (computed corrections) | Fixing values in a script, with sources still attached |

What it should **not** replace: `extract_dataset`/`extract_region` (they create the source records that accounting depends on), `populate_template` (it records assignments and write journals), `validate_output`, `record_decisions`, and the requirement/interpretation tools. Those are the verification spine the review says to keep. Removing `transform_dataset` and `query_dataset` would take the toolset from 28 to about 26, plus a simpler prompt. It has not been measured; the scorecard packets in the review should decide it.

## Security model

Two layers. Only the first is a real boundary.

1. **OS sandbox (macOS `sandbox-exec`, deny-by-default profile).** It allows:
   - reading the Python installation, system libraries, and the job's `inputs/`, `template/` and `working/`;
   - writing only the run's staging folder and `working/python/scratch`;
   - starting no new processes (fork is blocked) and running only the Python binary;
   - no network of any kind, including local and Unix sockets; no system services (mach lookups).
2. **An audit hook inside the child** applies the same rules with readable error messages. Python documents audit hooks as not being a security boundary; `ctypes` alone can defeat one. Every escape test therefore runs twice, once with the hook switched off, to show the OS layer holds by itself.

Also: the child runs with `python -I` and an empty environment, so `OPENAI_API_KEY`, `DI_ACCESS_TOKEN` and `.env` are out of reach (the `.env` read is tested). Limits cover CPU time, file size (512 MB), open files (256) and core dumps.

Tests ([tests/test_python_tool.py](../../tests/test_python_tool.py)) try to escape and check the effects from outside the sandbox:

- **Network:** TCP to a local listener (the backlog is checked afterwards to confirm nothing connected), DNS, urllib, a Unix socket.
- **Outside the job:** reading a file next to the store, the user's SSH key, `..` traversal, a symlink planted in scratch, copying out, writing into another job, listing parent folders.
- **Inputs and harness state:** append, truncate, delete, rename, hard-link, chmod and create on inputs; writes to the template, `state.sqlite` and a forged `validation.json`. Source hashes are compared before and after, and the intake hash check still passes.
- **Processes:** `os.system`, `subprocess`, `fork`, `posix_spawn`, `exec`, `ctypes`.

With the sandbox removed, all eight escape tests fail, so they test something real.

## Trade-offs for payroll data

- **Known gaps in this prototype.**
  - **macOS only.** On Linux (the Dockerfile) and Windows, the tool refuses to run rather than run unconfined. Production on Linux needs bubblewrap, nsjail or gVisor with no network namespace and read-only bind mounts, plus its own copy of these tests.
  - `sandbox-exec` is deprecated by Apple, though still shipped and used by major browsers.
  - **No memory limit** (`RLIMIT_AS` does nothing on macOS). A script can use a lot of RAM until the deadline kills it.
  - **File names leak.** File metadata is readable anywhere, so a script can learn that a path exists, but not what is in it.
  - **Shared software is readable.** Read access covers `/usr`, `/System` and `/opt/homebrew`. These hold software, not job data, but they are broad.
- **The data never leaves the machine through the tool.** But the model provider still sees whatever the script prints, the same as with today's read tools. The model can now compute over the whole dataset in one call and print as much as the output cap allows. This is a privacy question, not a sandbox question: how much payroll data per call may reach the provider?
- **Integrity relies on the gate, not on the code.** Model code can compute wrong numbers that look right. The typed language limited what *kinds* of mistakes were possible. Python does not. `validate_output` catches lost sources, changed cells, broken formulas and requirement totals. It does not catch a wrong business rule applied to every row consistently, such as the wrong tax band. The review's "skeptical reviewer" step matters more if this ships.
- **Auditability is better in one way and worse in another.** Each transformation is now a readable script saved with its hash, rather than a list of typed operations. But reviewing arbitrary code is harder than reviewing ten known operations.
- **Attack surface.** Prompt injection in an uploaded document could steer the model to write hostile code. The sandbox limits it to the job's own data with no way out. The worst it can do is corrupt this job's outputs or datasets, which validation and the source hashes then reveal. It cannot reach other jobs, credentials or the network.

## Before turning it on

1. Security sign-off on the profile in `sandbox_profile()`, and a decision on pandas.
2. A Linux sandbox backend with the same escape tests running in CI.
3. Run the review's scorecard packets with the tool on and off, compare requests, tokens, accuracy and pauses, and only then remove `transform_dataset`/`query_dataset`.
