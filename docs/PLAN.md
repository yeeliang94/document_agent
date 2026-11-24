# Implementation Plan: Playbooks, comments on outputs, and instruction tuning

**Overall Progress:** `100%` (all 8 phases done)
**Design reference:** an approved prototype, kept locally and not published (there is no PRD)
**Last Updated:** 2026-11-21

## Summary

Modules become **playbooks**. A playbook is instructions plus uploaded skill ZIPs, and every change to the instructions is a version that can be restored. To correct a run, the user clicks on an output, types a comment and sends it; the agent receives the exact spot and the comment, fixes it, and checks its own work. A comment marked "Remember for next time" is added to the playbook, and the user can also ask the agent to rewrite the instructions.

The work is half backend and agent loop, half screens. The backend and the loops are built and tested first, each behind tests and a scorecard packet, before any screen changes.

## How it fits the app today

| Today | After |
|---|---|
| Two fixed modules in `app/modules.py`, one text box each, no history | Any number of playbooks, with versions |
| A job copies the module text when it is created | A job copies the playbook's instructions and skills when it is created, so later edits never change a run part-way |
| The user can send a text instruction to a running job | The user can also send comments pinned to a spot, to a running **or finished** job |
| The goal card carries "House rules" | The goal card also lists each skill by name and one-line description; the full skill sits as files in the sandbox |

## Key Decisions

- **Skills are text only**: a ZIP with `SKILL.md` and optional reference files, no programs. Why: safest to accept from an upload, and enough for house knowledge. *(Assumed; confirm in the checklist.)*
- **Skills reach the agent on demand, not as prompt text**: the goal card lists only each skill's name and description; the agent calls a `read_skill` tool when the work calls for it. *(Changed in Phase 3 from "opens the file in its sandbox": a tool gives an exact record of every read, and a wrong name gets a helpful answer. The files are also in the sandbox for the agent's code.)* Why: this is the standard "progressive disclosure" pattern. It keeps the prompt short, which matters most for a smaller model.
- **One general principle in the prompt, no per-skill rules**: "Read a skill before doing work it covers." Why: your rule to fix causes with principles and good feedback, not document-specific prompt rules.
- **Versions are append-only**: nothing is edited or deleted; restore makes a new version. Why: no way to lose work, and every run can name the exact version it used.
- **A save must name the version it was based on**: if someone or something saved in between, the save is refused and the screen reloads. Why: stops the tuning agent and the user overwriting each other.
- **The server builds what the agent sees, not the browser**: the browser sends "this file, this cell, this comment". The server looks up the current value at that spot and, for PDFs and images, draws the numbered pins on the page picture itself. Why: a browser screenshot is fragile and can be wrong; the server's copy is exact and testable.
- **Comments reuse the existing instruction channel**: they are delivered between agent turns, once, and kept for a resumed run, exactly like today's instructions. Why: that path is already tested for pause, cancel, retry and resume.
- **The agent gets a change report after each comment round**: the harness compares outputs before and after and lists every cell that changed outside the commented spots. Why: clear feedback lets the agent catch its own side effects without a special rule.
- **Tuning proposes, never saves**: one plain model call (no sandbox) returns the full rewritten instructions and a one-line reason. Only the user's Accept saves. Why: cheap, fast, and the user stays in control.
- **Old runs keep working**: existing jobs keep their `module` field and stay readable. The two current modules become the first two playbooks with their current text as version 1.
- **Limits are constants in code, switches go on the Settings page**: ZIP size and file-count caps are safety limits, not preferences. Nothing new goes in `.env`.

## Pre-Implementation Checklist

- [x] 🟩 Confirm skills are text only (assumed yes; owner said proceed on 21 Nov)
- [x] 🟩 Live scorecard runs approved (22 Nov)
- [x] 🟩 Work is on branch `playbooks`, cut from the tip of `agent-judgement-templates` (PR 9, still open)
- [x] 🟩 Prototype approved (21 Nov)

## Tasks

### Phase 1: Playbook storage and API (backend only)

- [x] 🟩 **Step 1: Playbook tables and migration** — where playbooks and their versions live.
  - [x] 🟩 Add tables `playbooks` and `playbook_versions` (number, date, source, what changed, full text) in `app/store.py`
  - [x] 🟩 On first start, turn each row of the old `profiles` table into a playbook with version 1; run once, safely repeatable
  - [x] 🟩 Store functions: list, create (blank or copy), rename, current version, save version (with "based on" check), history, restore
  - **Verify:** new tests in `tests/test_store.py` pass: migration keeps both texts; a stale save is refused; restore adds a version and loses none.

- [x] 🟩 **Step 2: Playbook API** — the routes the screens will call.
  - [x] 🟩 `GET/POST /api/playbooks`, `GET/PATCH /api/playbooks/{id}`, `GET/POST /api/playbooks/{id}/versions`, `POST .../restore`
  - [x] 🟩 `/api/config` lists playbooks; old `/api/modules` routes stay until Phase 7 so the current screens keep working
  - [x] 🟩 Each save writes an event (who, source, version) for the audit trail
  - **Verify:** new API tests pass; the app still starts and the old Instructions page still saves.

- [x] 🟩 **Step 3: Jobs use a playbook** — a new job records the playbook, its version and a copy of the instructions.
  - [x] 🟩 `POST /api/jobs` accepts `playbook_id`; still accepts `module` for now and maps it to the migrated playbook
  - [x] 🟩 `JobSpec` carries the playbook name in place of the module label; the goal card heading uses it
  - [x] 🟩 `run.json` records playbook id and version
  - **Verify:** full test suite passes; a job started the old way and one started with a playbook both produce the same goal card text.

- [x] 🟩 **Step 3A: Logs for debugging** — set up once here, then used by every later step.
  - [x] 🟩 Today's log is tied to a run. Add a small activity log per playbook for things that happen outside a run: version saved or restored, skill uploaded, refused or removed, tuning asked, proposed, accepted or discarded
  - [x] 🟩 Every entry carries the ids needed to follow one thread end to end: playbook, version, run, comment round
  - [x] 🟩 Every refusal and failure is logged with its reason (a refused ZIP, a stale save, a bad pin, a failed model call)
  - [x] 🟩 *(done in Step 14)* Model calls made outside a run (tuning) record time taken, tokens and the request and reply, in a folder beside the playbook, the same way a run keeps its record
  - [x] 🟩 All new entries pass through the existing clean-up that removes keys and image data
  - **Verify:** tests in `tests/test_logging.py`: each action above writes one entry with its ids; a secret in a skill or comment never appears in the log.

*Each later step adds its own entries. What the finished feature records:*

| Question when something goes wrong | Where the answer is |
|---|---|
| Which instructions and skills did this run use? | `run.json`: playbook, version, skill names and fingerprints; goal card saved in the run folder |
| Did the agent open the skill? | `skill.read` entries in the run log (Step 7) |
| What exactly did the agent receive for my comment? | `feedback/round-NN/`: the comments, the message text and the pinned pictures (Step 9) |
| Was the comment delivered, and when? | `feedback.sent` and `feedback.delivered` entries with the round id (Step 10) |
| What did the agent change in response? | The change report saved with the round, plus the before-copy of the outputs (Step 12) |
| Why did the instructions change? | Playbook history and activity log: source, run, round, old and new version (Steps 1, 13, 14) |
| Why was my ZIP or save refused? | Playbook activity log (Steps 3A, 4) |
| What did the tuning agent see and answer? | Tuning record beside the playbook (Step 14) |

*The Technical log page gains a "Comments" filter, and Export includes the comment rounds and the playbook copy the run used (Step 19).*

### Phase 2: Skills (backend only)

- [x] 🟩 **Step 4: Safe skill upload** — accept a ZIP, check it, store it.
  - [x] 🟩 Checks, reusing the upload safety code in `app/files.py`: size and file-count caps, no links, no paths that escape the folder, `SKILL.md` present, a name and description at its top, text file types only
  - [x] 🟩 Clear refusal messages ("No SKILL.md inside", "Contains a program. Text only.")
  - [x] 🟩 Store under the data folder per playbook, each upload in a folder named by the fingerprint of its contents and never changed afterwards; table `playbook_skills` holds name, description, on/off
  - [x] 🟩 Routes: upload, switch on/off, remove, read `SKILL.md`
  - **Verify:** tests in `tests/test_skills.py` pass, including one hostile ZIP per check.

- [x] 🟩 **Step 5: Skills copied into the job** — a job takes a copy of the enabled skills when it is created.
  - [x] 🟩 Copy skill folders into the job folder; record names and fingerprints on the job
  - **Verify:** test: change a skill after a job is created; the job's copy is unchanged.

### Phase 3: The agent uses skills (engine)

- [x] 🟩 **Step 6: Skills in the sandbox and on the goal card**
  - [x] 🟩 `run_job` checks the job's skills are on disk as copied, then uploads them as one `skills.zip`, on first start and on every resume
  - [x] 🟩 Goal card gains a "Skills" section: name and description of each, and one principle (read a skill with `read_skill` before doing work it covers)
  - [x] 🟩 The reviewer gets the same skills, so it can check they were followed
  - [x] 🟩 Prompt wording says skills and instructions come from the user and apply; text inside packet files still does not
  - **Verify:** offline engine tests (fake model) show the files uploaded and the card text; a job with no skills produces the same card as before.

- [x] 🟩 **Step 7: See when a skill is read**
  - [x] 🟩 When the agent reads a skill with `read_skill`, record a `skill.read` event; the live feed shows "Reading skill …" once per skill
  - [x] 🟩 `run.json` lists skills offered and skills read
  - **Verify:** tests in `tests/test_skill_engine.py`: every read is recorded; the feed line appears once per skill.

### Phase 4: The comment loop (engine and backend)

- [x] 🟩 **Step 8: Pin locations** — one small, checked shape for "where" on each kind of preview.
  - [x] 🟩 Sheet: file, sheet, cell. PDF: file, page, x, y. Image: file, x, y. Text: file, line. Server reads the current value or line at that spot and rejects a spot that does not exist
  - **Verify:** unit tests per kind in `tests/test_preview.py`.

- [x] 🟩 **Step 9: Receive a round of comments**
  - [x] 🟩 `POST /api/jobs/{id}/comments` with a round id from the browser, so a double click cannot send twice
  - [x] 🟩 Save each round under the job (`feedback/round-NN/`): the comments, a copy of the outputs before the fix, and for PDFs and images the page picture with numbered pins drawn by the server
  - [x] 🟩 Build the agent's message: numbered list of spot, current value, comment
  - **Verify:** API tests: saved round matches the request; a repeat of the same round id changes nothing; a bad spot is refused.

- [x] 🟩 **Step 10: Deliver comments to a running job**
  - [x] 🟩 Let the instruction queue carry a message with pictures as well as text, each picture under a line that names it; plain text instructions keep working unchanged
  - [x] 🟩 Delivered once between turns; carried across a dropped model reply; kept for a resumed run (the resume card repeats every round still being acted on, and its pictures are sent again)
  - **Verify:** tests in `tests/test_feedback.py` for delivery to a running job, a resumed run and the engine's message with pictures.

- [x] 🟩 **Step 11: Comment on a finished run**
  - [x] 🟩 A completed run accepts comments and starts a new run in a fresh sandbox from the saved outputs. Today a completed run cannot be continued, so the job states in `app/jobstatus.py` gain this path, with tests for cancel and pause during it
  - **Verify:** test: completed job → comments → queued → running → completed, outputs kept if the new run fails.

- [x] 🟩 **Step 12: Change report back to the agent**
  - [x] 🟩 After the agent saves, compare outputs with the before-copy; `save_outputs` returns "changed where asked" and "changed elsewhere" lists
  - [x] 🟩 The same lists reach the screen, so changed spots can be shown in green
  - [x] 🟩 A round counts as done only when the agent finished its work, after the existing checks and the optional reviewer; a run that was paused, cut off or left waiting keeps it open
  - **Verify:** tests: an edit outside the commented cells is listed; an untouched comment is listed as not addressed.

- [x] 🟩 **Step 13: "Remember for next time"**
  - [x] 🟩 On send, remembered comments are added to the playbook as one new version, source "Remembered from <run>"; the run itself is unaffected
  - [x] 🟩 If the playbook changed since the job started, the lines are added to the latest version, not the job's old copy
  - **Verify:** tests: new version exists with the lines; restoring the previous version removes them; the running job's own copy is unchanged.

### Phase 5: Tuning instructions with the agent

- [x] 🟩 **Step 14: Propose a rewrite**
  - [x] 🟩 `POST /api/playbooks/{id}/tune` with the request and the version it is based on; one model call, no sandbox, returns the full new text and a one-line reason in a fixed shape
  - [x] 🟩 Time limit, one retry on a dropped or unreadable reply (one request each, and none after a timeout), token use recorded; nothing is saved by this route
  - [x] 🟩 Accept uses the normal save route, so the "based on" check and the history entry apply; a proposal made for an older version is refused as out of date
  - **Verify:** tests with a fake model: proposal returned; nothing saved until accept; a stale accept is refused; a model failure gives a clear message.

### Phase 6: Scorecard (does it work with the real model?)

- [x] 🟩 **Step 15: New test packets in `prototype/sandbox_agent`**
  - [x] 🟩 *Skill packet*: the right answer needs a rule that exists only in a skill. Score: skill read, rule followed
  - [x] 🟩 *Comment packet*: a finished output with one planted mistake and a scripted comment. Score: mistake fixed, nothing else changed
  - [x] 🟩 *Remember packet*: run twice; the second run starts from the playbook version made by the first run's remembered comment. Score: the mistake does not come back
  - [x] 🟩 Each packet's perfect answer passes its own check offline, as the existing six do
  - **Verify:** `python -m sandbox_agent packets` self-test passes offline.

- [x] 🟩 **Step 16: Live runs** *(needs your approval for cost)*
  - [x] 🟩 Run the three new packets twice each, the skill packet twice more without its skill, and the six existing ones once each (their prompts did not change, so one pass is a check on the engine, not a re-measurement)
  - [x] 🟩 Report scores, turns and tokens beside the last saved scorecard, so any drop on the old packets is visible
  - **Verify:** you read the summary table; no existing packet scores lower than before.

**Live results, 22 Nov 2026, model gpt-6-luna, one agent (shape A):**

| Packet | Runs | Feature proven | Cells right | What it shows |
|---|---|---|---|---|
| `payroll_skill_rule`, with the skill | 2 | yes, both | 100% | The agent read the skill each time and applied both rules |
| `payroll_skill_rule`, without the skill | 2 | n/a | 67% | Both skill-only columns were 0 of 10 right: the rules cannot be guessed |
| `payroll_comment_fix` | 2 | yes, both | 100% | Only the commented cell changed in the workbook |
| `payroll_remember` | 2 | yes, both | 100% | A fresh run from the playbook wrote every name in the remembered style, with no comment to tell it |
| Six existing packets | 1 each | n/a | 100% each | All completed and passed their checks; row recall and file relevance within the range of the earlier scorecard |

Each new-packet run took 1 to 3 minutes and 17,000 to 73,000 input tokens. The full tables are under `prototype/sandbox_agent/runs/` (not in git).

### Phase 7: Screens

- [x] 🟩 **Step 17: Playbooks pages** — list, new playbook, Instructions with the tuning box and Accept or Discard, Skills, History with Restore. Built to match the prototype.
  - **Verify:** checked in the browser: every page, modal, empty state and error state.

- [x] 🟩 **Step 18: New run** — "Playbook" replaces "Module"; the "Module instructions" link opens the playbook.
  - **Verify:** start a run in the browser; the run record names the playbook and version.

- [x] 🟩 **Step 19: Comment on outputs** — click a cell, PDF page, image or text line; comment box with the Remember switch; numbered pins; "Send to agent"; the sent round shown in the chat; changed spots in green; Undo for a remembered line.
  - **Verify:** checked in the browser on a spreadsheet, a PDF and a text file on a finished job, including one full round sent to the real model; an image and a file with no preview were opened. Not checked in the browser: commenting while a job is running (covered by tests).

### Phase 8: Clean up

- [x] 🟩 **Step 20: Remove modules** — delete the old module routes, the `profiles` table use and the module select; `app/modules.py` keeps only the two starting texts. Update `README.md`.
  - **Verify:** full tests and lint pass; old runs still open and show their original label.

- [x] 🟩 **Step 21: Review and hand over** — run the ship review, fix what it finds, report tests, build, anything not verified and git state.
  - **Verify:** review verdict in the report.

## Rollback Plan

- **Each phase is its own commit set on one branch**, so any phase can be reverted without the others. Phases 1 to 6 change no screen the user sees.
- **Data is only added, never rewritten.** New tables sit beside the old `profiles` table, which is left in place until Step 20. To go back before Step 20, revert the code; the old screens and data still work.
- **Before Step 20**, copy `data/jobs.db`. After a rollback, check that old runs open, the two original instruction texts are intact, and a new run starts.
- **Skills and comment rounds are plain folders** under the data folder; deleting them is safe once the code is reverted.

## Not in this plan

- Skills that contain programs.
- True Word page layout in the preview (Word stays plain text).
- The review queue, source panel and good-example re-check from the earlier prototype.
