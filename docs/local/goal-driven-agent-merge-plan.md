# Plan: merge the sandbox agent design into the main app

Date: 12 November 2026
Branch: `sandbox-agent-prototype`
Status: Plan only. No code changed.
Builds on: [harness efficacy review](harness-efficacy-review-2026-11.md), the prototype in `prototype/sandbox_agent/`, and the proxy results from the Windows machine (explicit sandboxes work with the EMEA address and the `openai.eu` model alias).

## 1 The goal in one paragraph

The user gives the app a goal (for example "prepare this month's payroll instructions" or "produce the payment journal in our upload format"), a description of the finished result (a template workbook, a written format, or both) and a zip of whatever files they have. Nobody knows in advance which files matter. The agent works out what each file is, decides what is relevant, does the work with code, checks its own result against the goal, and hands back the deliverable plus a clear record of what it used, what it set aside and why. The harness does not prescribe the steps. It gives the agent good tools, a map of the files, firm checks on the finished result, and a record of everything.

## 2 The main design ideas

1. **Goal first, steps second.** The agent receives the goal and a definition of done, then chooses its own path. The prompt states outcomes and stopping rules, not a procedure.
2. **Relevance is a decision, not a failure.** Every file must get a decision (used, reference, not relevant, unclear) with a one-line reason. "Not relevant" is a valid answer. The check enforces that every file was looked at and decided, never that every file was used.
3. **Code does the work.** Code Interpreter replaces most of the 30 narrow tools. The model reads, joins and writes with pandas and openpyxl, which it handles far better than a home-made tool set.
4. **Check at the end, not at every step.** Per-call bookkeeping caused most failures in the main app. Strict checks move to one "save and check" step that the agent calls whenever it wants. It fixes what the check reports.
5. **Read in parallel, write in one place.** Scouts may read and profile files at the same time. Only the lead agent writes the deliverable. Two writers make conflicting choices.
6. **Everything is kept locally.** Inputs, the file map, every script the agent ran, every output and every check result are saved in the job folder and the database, so a run can be audited and resumed.

## 3 What a run looks like after the change

1. **Upload.** The user picks a module, enters the goal, and uploads the zip and the template or format. The app unpacks the zip on this machine and lists every file with a fingerprint. This list is the official inventory. The model cannot add to it or leave anything out.
2. **Goal card.** The harness combines the module context, the user's instructions, the template's own rules (dropdowns, number cells, locked cells) and the module's deliverable rules into one short brief: what done looks like.
3. **Map.** The lead agent decides how to explore. On a small packet it reads everything itself. On a large one it sends scouts to profile folders in parallel. Each scout returns a structured profile per file: what it is, its tabs and columns, the period, the entity, and a first view of whether it matters for this goal. The lead agent writes the document map, a file-by-file record with a decision and reason for each.
4. **Work.** The lead agent builds the deliverable with code in its sandbox. It may ask a scout a narrower question ("which of these two salary files is final?") when it needs one.
5. **Ask.** When a material fact is genuinely unclear, it continues the independent work first, then asks the user one question. The run pauses safely and resumes with the answer.
6. **Save and check.** The agent calls save and check. The harness downloads the outputs, then checks on this machine: template untouched, dropdown and number rules followed, no duplicate rows, the module's rules met (for example "journal debits equal credits"), and every file in the map. The agent fixes and repeats.
7. **Review.** Once the checks pass, a fresh reviewer agent with no memory of the work compares the deliverable with the goal and a sample of the sources, and lists doubts. The lead agent resolves them or reports them as open.
8. **Hand back.** The user sees the deliverable, the document map, what was set aside and why, open questions, and the check results.

## 4 Agent team design: what to research and decide

There are four realistic shapes. Phase 2 tests them on the same packets and picks the simplest one that is accurate enough.

| Shape | How it works | Strength | Risk |
|---|---|---|---|
| **A. One agent with code** | The lead agent does everything in one sandbox. | Simplest, cheapest, no handover loss. In the prototype it was as accurate as B and faster on a 12-file packet. | Context fills up on large packets; slow on many files. |
| **B. Lead agent plus read-only scouts** (the prototype) | Scouts profile folders in parallel, each in its own sandbox, and return structured profiles. The lead agent writes. | Faster mapping on big packets; the lead's context stays clean. | Profiles can miss detail the lead later needs; extra cost per scout. One prototype scout took 11 minutes. |
| **C. B plus an independent reviewer** | After the checks pass, a fresh agent reviews the result against the goal. | Catches wrong-but-valid results the rule checks cannot see, such as a superseded file used instead of the final one. | Extra time and cost; reviewers can raise false alarms. |
| **D. Specialist writers** | Several agents each build part of the deliverable, for example one per entity, and the lead agent merges. | Parallel writing on very large jobs. | Conflicting decisions between writers. Only worth testing if A to C are too slow on real sizes. |

**What published experience says** (to be confirmed in Phase 2):
- Anthropic's research system uses a lead agent that spawns parallel sub-agents for breadth-first reading, at several times the token cost of one agent. It works best when the sub-tasks are independent. ([Anthropic engineering](https://www.anthropic.com/engineering/multi-agent-research-system))
- Cognition warns that parallel agents that each make decisions produce conflicting work. Its advice is to keep decisions in one thread and share full context. ([Cognition](https://cognition.ai/blog/dont-build-multi-agents))
- The OpenAI Agents SDK offers two patterns: an agent called as a tool (the lead keeps control, as with our scouts) and a handoff (control moves to another agent). For this app, the lead keeping control fits better. ([Agents SDK docs](https://openai.github.io/openai-agents-python/multi_agent/))

**Questions Phase 2 must answer with numbers:**
1. From what packet size (files, megabytes) do scouts beat one agent on time without losing accuracy?
2. Should the harness decide when to use scouts (by packet size), or should the model? Today the model decides.
3. What must a scout's profile contain so the lead agent rarely has to re-read a file?
4. Does a reviewer agent catch real errors on the test packets, and how many false alarms does it raise?
5. Is per-folder the right split for scouts, or per file type, or per entity?

## 5 Phases

Each phase ends with a check that must pass before the next starts. Phases 1 and 2 happen in the prototype, so the main app does not change until the design is proven.

### Phase 0: decisions and prerequisites (you)
- Approval for real payroll files to go into OpenAI sandboxes through the proxy. Until then, synthetic files only.
- Level of proof: is "each output row records its source file, and every file has a decision" enough, or must every cell keep its exact source? This decides how much bookkeeping stays.
- Confirm the proxy settings that work: the EMEA address with the `openai.eu` model alias. Ask the GenAI team whether that alias is the one to use long term.
- **Done when:** the three answers are written down in this file.

### Phase 1: a proper test set (the scorecard)
- Build 6 to 10 synthetic packets with answer keys, covering both kinds of goal:
  - payroll instructions into a template (the prototype packet is the first one);
  - a payment journal in a written format with no template, including the rule that debits equal credits;
  - packets with irrelevant files, superseded drafts, duplicates, scanned PDFs, nested folders, hidden sheets and a file that genuinely needs a question to the user;
  - one large packet (over 100 files and over 50 MB) to test speed and scouts.
- Extend the prototype scorer: rows and cells, the relevance decision per file, whether the agent asked when it should have, time, cost and number of model calls.
- **Done when:** every packet scores 100% against its own answer key when the answer key is fed in as the "output". This proves the scorer itself.

### Phase 2: choose the team design (prototype only)
- Add a reviewer agent (shape C) and a "deliverable format" input (for goals with no template) to the prototype.
- Run shapes A, B and C three times each on every packet. Record the measures from Phase 1.
- Answer the five questions in section 4.
- Make goal and relevance general: a goal card built from the module context, the instructions and the template or format; relevance decisions in the map; module rules that live as data, not code.
- **Done when:** one shape is chosen with numbers behind it, and the prototype completes every packet with no hand-made per-packet prompt rules.

### Phase 3: the new engine inside the main app, behind a switch
- Add a new engine in `app/engine/` built from the prototype: sandbox, agent, checks, run record. The current engine stays as it is.
- **First step, a short spike:** can the app's current agent library (Pydantic AI) use an explicit sandbox through the proxy? If yes, the new engine uses it, giving one library. If not, the new engine uses the OpenAI Agents SDK, as the prototype does, which adds one library.
- Reuse what the main app already does well: upload and unzip, the job store, the module contexts, pause for a user question, new instructions mid-run, and the job folder.
- Map the prototype's run folder into the job folder and the database: inventory, document map, scouts, scripts, outputs, checks, events.
- Settings page: engine choice (current or new), scouts on or off and how many, reviewer on or off, model and endpoint shown with a warning when they are in different regions. Remove the unused `DI_CODE_EXECUTION` setting.
- **Done when:** the new engine passes the Phase 1 packets through the main app with scores within 2 points of the prototype, all existing tests pass, and the old engine is unchanged.

### Phase 4: pauses, restarts and limits
- A sandbox vanishes after 20 idle minutes. After every save, outputs are already on this machine. On resume after a pause, the harness creates a new sandbox, re-uploads inputs and saved outputs, and gives the agent its map and notes so it continues rather than restarts.
- Time, turn and cost limits per job, shown on the Settings page.
- Clear messages for proxy errors (403 and region "container not found") that say what to change.
- **Done when:** a job paused for 30 minutes on a question resumes and completes, a job stopped mid-run can be resumed, and each limit stops a run cleanly with a partial result saved.

### Phase 5: screens (HTML prototype first)
- New views: the document map (each file, its decision and reason, which scout looked at it), live progress ("scouting 3 folders", "running code"), check results, and reviewer doubts.
- Build as an HTML prototype for your approval before any app screen changes, following `~/.agents/taste/ui.md`.
- **Done when:** you approve the prototype, the app screens match it, and every state (empty, running, paused for a question, failed, partial, complete) is checked in a browser.

### Phase 6: side-by-side trial and switch-over
- Run both engines on the scorecard and on approved real packets.
- Switch the default when the new engine is at least as accurate, finishes more jobs and needs fewer questions.
- Then retire the old engine and its 30 tools in a separate change.
- **Done when:** the new engine is the default, and the old engine is removed or kept only as a read-only viewer for old jobs.

### Phase 7: Windows enterprise rollout
- Run the scorecard on the Windows machine through the proxy, using the same scripts as the prototype.
- **Done when:** results on Windows match the Mac results.

## 6 How success is measured

| Measure | Target before switching over |
|---|---|
| Jobs finished without help | at least 9 in 10 scorecard packets |
| Rows and cells correct | at least 99% |
| Relevance decision per file correct | at least 95% |
| Asks when it should, and not when it should not | every "needs a question" packet asks; no others do |
| Time | under 10 minutes for a 20-file packet |
| Cost per job | recorded and shown; no target until Phase 2 gives a baseline |

## 7 Risks

- **Data policy.** Whole files go to OpenAI sandboxes. This is blocked until Phase 0 approval.
- **Proxy region.** The model alias must match the endpoint's region, or sandboxes are not found. Covered by the Settings warning and the error message.
- **Slow replies.** Single OpenAI replies took up to 12 minutes in prototype runs. Covered by time limits, progress lines and the scout timeout.
- **Less cell-level proof** than today. Depends on the Phase 0 decision.
- **Two engines for a while.** More to maintain until Phase 6. Kept short by not changing the old engine.

## 8 What is not decided by this plan

- Whether the reviewer agent stays (Phase 2 decides).
- Which agent library the new engine uses (the Phase 3 spike decides).
- Whether scouts are chosen by the harness or the model (Phase 2 decides).
