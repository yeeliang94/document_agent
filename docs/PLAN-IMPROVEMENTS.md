# Implementation Plan: A steadier agent, clearer runs, and client notes

**Overall Progress:** `88%` (37 of 42 steps) (Phases 1-10 done; Phases 11-12 left)
**Builds on:** `docs/PLAN.md` and `docs/PLAN-NEXT.md` (both built), and the chat-and-zoom branch (PR yeeliang94/document_agent#1)
**Design reference:** `prototype/improvements/index.html`, six batches, and `prototype/comment-answers/index.html`, batch 7 (there is no PRD)
**Last Updated:** 2026-11-26

## Summary

Three things change. The agent learns its limits, keeps its context small, sees what its formulas add up to, and sees its files as the person receiving them will. The person sees how far a run has got, gets a plain message and a way back when a run stops, and gets a proper report when it finishes. Each client gets its own notes, which a run can follow on top of its playbook. And, taken from a study of Claude's document panel (26 Nov): the agent answers each comment, a cell or file it names opens at that spot, and any two versions of an output can be compared.

As in the earlier plans, the engine and backend are built and tested first. The screens follow, one prototype batch at a time, each after that batch is approved.

## How it fits the app today

| Today | After |
|---|---|
| The agent is cut off at a limit with no warning | It is told when it is close, saves, and writes down what is open |
| The whole history is re-sent on every step | OpenAI condenses older history past a size set on the Settings page |
| A formula's result is unknown to the agent and the checks | Every save reports the totals and any formula that fails |
| The agent cannot see how its files look | It gets a picture of each sheet, drawn as Excel shows it, and looks before it finishes |
| A workbook built from a format has no set look | The format can set a house style, applied by a ready-made helper, and the save warns of #### columns and numbers stored as text |
| A stopped run keeps only the last save | Every save is a version; a run can continue from any of them |
| A failed run shows the raw error | A plain sentence, with the raw text folded away |
| The finish message is one paragraph | A report: what was done, decisions made, what to check |
| Instructions belong to a playbook only | A client has notes too; where they disagree with the playbook, the client wins |
| Earlier runs offered as examples can be another client's | The same client's earlier runs are offered first |
| Panels have fixed widths | Both dividers drag; the run list can be hidden |
| A comment gets a verdict worked out by code: changed or not | It also gets the agent's own answer, and can be replied to or resolved |
| Cells and files named in the chat are plain text; a sent comment opens its file at the top | They open at that spot, as does a sent comment |
| The Changes switch compares with the last round of comments | It can also compare with any saved version, and an older version can be opened |

## Key Decisions

- **OpenAI's built-in compaction, with the trigger on the Settings page.** Why: you chose it, and the installed OpenAI libraries already accept the setting. It is tested against the real endpoint before anything depends on it.
- **The notes file is the agent's lasting memory.** The agent is told to write findings and decisions there as it goes, because compaction keeps the notes exactly and the history only roughly. *(Assumed; I proposed it and you did not object.)*
- **A saved version is a copy of the outputs at each successful save.** Why: saves already happen at natural stopping points and are already checked, so no new mechanism is needed inside the agent.
- **Continuing from a version starts a fresh workspace from that copy**, as Resume does today. The earlier workspace cannot be brought back.
- **One notes file per client, covering all their work**, with versions and Undo. It reuses the playbook's version storage. Why: one place to look per client.
- **A client is optional on a run and sits on the main New run form.** *(Confirmed 2026-11-26.)*
- **The app suggests a client and never attaches one silently.** Client folders follow no fixed pattern, so it looks for a known client's name anywhere in the folder and file names; if the box is left empty and the agent sees a known client in the documents, it asks once.
- **Client notes are text only for now.** No attached reference files. *(Confirmed 2026-11-26.)*
- **Remembering gets a scope.** PLAN-NEXT moved "what to remember" from a tick box to the agent's own judgement. That stays. What is new: a lesson is saved to the client or to the playbook, the agent proposes which, and the person can set it on a comment. This partly brings back a choice PLAN-NEXT removed, because you asked to save things to a client on purpose.
- **The agent's pictures are drawn on this machine by the app's own code, not by Excel or LibreOffice.** Why: nothing extra to install on Windows or Mac, and the agent sees what the file preview shows you. The cost: close to Excel, not identical; what it cannot draw is named, not guessed. *(Proposed 2026-11-26.)*
- **The look is checked by warnings, not by a second reviewer.** Clear faults (####, numbers stored as text) are caught by code; taste is left to the agent's own look. *(Proposed 2026-11-26.)*
- **The agent writes each answer; code still says whether the spot changed.** Why: the screen already trusts only the file comparison for "changed". The answer explains; where the two disagree, the screen shows the code's verdict. *(Confirmed 2026-11-26.)*
- **A reply is a new comment on the same spot, sent as a round like any other,** carrying the earlier exchange. Resolve and Reopen are for the screen only and never reach the agent. Why: one way of talking to the agent, not two. *(Confirmed 2026-11-26.)*
- **Links are checked, not trusted.** The agent names cells as `Sheet!B4` and files by name; the server turns into links only those that exist. Why: a link to nowhere is worse than plain text. *(Confirmed 2026-11-26.)*
- **Comparing versions extends the existing Changes switch,** with its green cells and grey old values, rather than adding a second compare tool. *(Confirmed 2026-11-26.)*
- **No setting or limit without a control.** A test fails when a Settings option has no control on the Settings page, and the project instructions say so.

## Pre-Implementation Checklist

- [x] 🟩 Prototype approved: all six batches as drawn (2026-11-26). The screens also keep what chat-and-zoom added (files in the message box, Send now, zoom).
- [x] 🟩 The three client decisions confirmed: main form, optional, text only
- [x] 🟩 How client folders are named: no fixed pattern, so the suggestion looks for a known client's name anywhere in folder and file names
- [x] 🟩 An OpenAI key is set on this machine, and live steps may use it; each live run is announced with its rough cost first
- [x] 🟩 Prototype for batch 7 approved: comments that answer back, links to the spot, versions to compare (`prototype/comment-answers/index.html`, 2026-11-26)
- [x] 🟩 No conflicting in-progress work: chat-and-zoom is committed (PR yeeliang94/document_agent#1), and this plan builds on that branch

## Tasks

### Phase 1: The agent knows its limits (engine)

- [x] 🟩 **Step 1: Only describe tools the agent has** — the briefing mentions scouts only when scouts are switched on.
  - [x] 🟩 Build the scout line the way the source-proof line is already built
  - **Verify:** tests pass; a test reads the briefing with scouts at 0 and finds no mention of them.

- [x] 🟩 **Step 2: A warning before each limit** — at about 80% of the step, time or token limit the agent is told what is left and to save and note what is open.
  - [x] 🟩 One message per limit, sent once
  - [x] 🟩 An event the screen can show ("8 steps left")
  - **Verify:** a test with a 10-step limit shows the warning reaching the agent at step 8 and a save before the cut.

- [x] 🟩 **Step 3: A passed run stays passed** — if a limit is hit during the follow-up checks after the work already passed, the run still ends as Finished.
  - **Verify:** a test that hits the limit in the follow-up ends as Finished, not Partly done.

- [x] 🟩 **Step 4: The clock stops while a question waits** — time spent waiting for your answer no longer counts against the run's time limit.
  - [x] 🟩 The "no answer yet" reply tells the agent to write its findings to the notes and save before it ends
  - **Verify:** a test with a 1-minute limit and a 2-minute wait finishes normally once answered.

- [x] 🟩 **Step 5: More steps on request** — a paused or nearly finished run can be given 20 more steps.
  - [x] 🟩 One API route; the amount is a Settings value
  - **Verify:** a test extends a run at its limit and it continues without restarting.

### Phase 2: A smaller context (engine)

- [x] 🟩 **Step 6: Try compaction against the real endpoint** — a long scripted run with compaction on, before building on it.
  - [x] 🟩 Confirm it triggers, with our settings and through the gateway
  - [x] 🟩 Record what the condensed history keeps and loses
  - **Verify:** I report the result. If it does not trigger, we stop here and choose again; nothing else depends on it yet.
  - **Result (2026-11-26, direct OpenAI, gpt-6-luna):** with a 20k trigger it condensed at about 23k tokens, twice in a 9-call run, and the next call fell to about 9k. A new run sent the whole list again (as the engine does after a message) and was billed about 16k, not 120k, so OpenAI honours the condensed item in a re-sent history. Kept: the facts the task named. Lost: other detail (a figure nobody asked about could not be recalled). Not tried: a LiteLLM gateway.

- [x] 🟩 **Step 7: Compaction with its trigger on the Settings page** — "Condense history at" with a tested default.
  - [x] 🟩 Setting, with a range; passed to every agent call
  - [x] 🟩 An event when it happens, shown in the Technical log
  - **Verify:** change the value on Settings, start a run, and the Technical log shows the condense event at that size.
  - **Result:** a live 12-file payroll run with the trigger at 20k condensed three times and finished passing. One reply can grow past the trigger on its own (many sandbox steps in one reply), so the log line gives no size.

- [x] 🟩 **Step 8: Notes as lasting memory** — one general line in the briefing: write findings and decisions to the notes as you go.
  - **Verify:** in a live run the notes file fills in during the run, not only at the end.
  - **Result:** in the live run notes.md was saved at 2.5 minutes of 13, and at every save after.

### Phase 3: The agent sees its totals (engine)

- [x] 🟩 **Step 9: Work out formulas on every save** — the save reply lists each total with its value and any formula that fails or misses rows.
  - [x] 🟩 Reuse the calculator the preview already uses
  - [x] 🟩 Add "Totals add up" to the check summary
  - **Verify:** a test saves a workbook whose total misses the last row; the reply names the cell and the run does not pass.
  - **Result:** each save lists every total with its value; an error value, or a plain number between a total's range and the total that the formula reads nowhere else, is a problem, so the save does not pass. A fault the blank template already had is not blamed on the agent. A formula this machine cannot work out is counted, never guessed. The check summary has `totals` and `totals_problems`.

### Phase 4: The agent sees and shapes its output (engine)

Today the agent can only draw a rough chart of cell values, and in the live runs it never did. It cannot see fonts, fills, borders, widths or number formats, and a workbook built from a written format has no look at all. These steps give it a real view of its files and a ready-made way to format them.

- [x] 🟩 **Step 10: Draw a sheet as Excel shows it** — on this machine, from the saved file, using the code the file preview already uses (cell formats and formula results), drawn into a picture with Pillow (already installed).
  - [x] 🟩 Fonts, fills, borders, column widths, row heights, merged cells, number formats, formula results, hidden rows and columns left out
  - [x] 🟩 A sheet too big for one picture is drawn as tiles with row and column labels; anything it cannot draw (charts, pictures in cells, conditional colours) is named under the picture, not guessed
  - **Verify:** tests draw a sample template and check a merged title, a bold shaded header, a date shown as 31/10/2026 and a total shown with its formula result.
  - **Result:** drawn at 1.25x Excel's size with Arial (Mac, Windows) or DejaVu (Linux), else Pillow's own font with bold faked. Numbers that do not fit show ####, text spills into empty cells, frozen panes show as a line. Up to 200 rows and 40 columns per call, in at most 4 tiles; the rest is named.

- [x] 🟩 **Step 11: A tool to look at the output** — `look_at_output(file, sheet, cells)` returns the picture to the agent, with the whole sheet as the default.
  - [x] 🟩 Pictures kept small enough to send (the existing size limits), at most a few per call
  - [x] 🟩 One briefing line replaces the matplotlib advice: look at each finished sheet before you finish, as the person receiving it would
  - **Verify:** a test calls the tool and gets a picture back; a scripted run sees a missing border it was asked to fix.
  - **Result:** the tool reads the file straight from the sandbox, so no save is needed first. The scripted "missing border" run was not built: the test shows the border's line in one picture and its absence in the other instead.

- [x] 🟩 **Step 12: A look before finishing** — the first save that passes every check also returns a picture of the top of each deliverable and of the rows around its totals.
  - [x] 🟩 Switched on or off on the Settings page ("Show the agent its files before it finishes"), on by default
  - [x] 🟩 The cost is noted in the Technical log (each picture is roughly 1,000–2,000 tokens)
  - **Verify:** a test's passing save carries the pictures; with the setting off it does not.
  - **Result:** the top 30 rows of each deliverable (up to two workbooks) and, when the last total is further down, the rows around it; at most 4 pictures. A sample sheet's picture costs about 255 tokens, so the estimate in the log is usually below the 1,000-2,000 guessed above.

- [x] 🟩 **Step 13: A house style for workbooks built from a format** — a written format can say how it looks: header style, number and date formats per column type, column widths, a frozen header row. A format with no style gets a plain, tidy default.
  - [x] 🟩 A small helper placed in the sandbox (`from house_style import apply`) applies the style, so the agent does not hand-code it each run
  - [x] 🟩 New save warnings that need no model: a value too wide for its column (Excel shows ####), numbers or dates stored as text, a number column with no number format, data rows hidden. Shown as "Looks right" in the check summary
  - [x] 🟩 A filled template keeps its own look; the warnings name only cells the agent wrote
  - **Verify:** a test builds a workbook from a format, applies the helper and passes; a workbook with a ##### column and a date stored as text gets both warnings.
  - **Result:** the helper (`app/engine/house_style.py`) is copied into the sandbox for runs with no template. It also turns numbers and dates written as text into real ones in number and date columns. Look warnings are listed as `looks` in the save reply and `looks_right` in the summary; they do not stop a save from passing.

### Phase 5: Runs that stop can be picked up (backend)

- [x] 🟩 **Step 14: A version at every save** — a copy of the outputs, with the step number, time and a one-line description.
  - [x] 🟩 A cap on versions kept per run, on the Settings page
  - [x] 🟩 API: list a run's versions
  - **Verify:** run the demo; the API lists one version per save, newest first.
  - **Result:** versions are copies, not links, so a file saved from Excel in the outputs folder cannot change an older version. Kept under `<job>/versions/`. There is no demo mode, so the check is a scripted run through the API.

- [x] 🟩 **Step 15: Continue from a version** — restores that copy as the outputs and resumes from it.
  - [x] 🟩 The resume note tells the agent which version it starts from
  - **Verify:** a test stops a run after two saves, continues from the first, and the outputs match the first.
  - **Result:** `POST /api/jobs/{id}/versions/{n}/continue`, for a run that has stopped or finished; refused while a run is going.

- [x] 🟩 **Step 16: Plain errors** — one place turns a failure into a plain sentence and a next step; the raw text is kept for the Technical log.
  - [x] 🟩 Used for run failures, tool failures and the connection test
  - [x] 🟩 Form errors return a sentence, not "Request failed (422)"
  - [x] 🟩 "See the run record" replaced by the issues themselves
  - **Verify:** tests feed in the known failures (no key, lost workspace, refused, over the day's limit) and read back plain sentences.
  - **Result:** `app/plain_errors.py`. The raw text is kept as `error_detail` on the job and `raw` on the event. Unknown failures say so plainly and ask for the Technical log.

- [x] 🟩 **Step 17: Progress the screen can show** — the run carries steps used, minutes used, both limits and its stage.
  - **Verify:** the run's API answer shows "14 of 60" style numbers changing during a demo run.
  - **Result:** the job carries `progress`: steps, max_steps, minutes, max_minutes, stage (reading, filling, checking) and clock_paused, updated at each step of the lead agent.

### Phase 6: A proper finish report (engine)

- [x] 🟩 **Step 18: The agent ends with a report** — a headline, what it did, decisions it made, what to check before sending.
  - [x] 🟩 Stored with the run; the downloadable summary file uses the same text
  - **Verify:** a test run ends with all four parts filled; an older run still opens and shows its one paragraph.
  - **Result:** the agent's final answer has headline, done, decisions and to_check (assumptions count as decisions). The job keeps it as `report`; `summary` is the headline, so lists and the current screen show one line. processing_summary.txt holds the whole report, with why a run stopped after the headline. A run from before keeps its paragraph (`app/engine/report.py`).

### Phase 7: Clients (backend and engine)

- [x] 🟩 **Step 19: Client storage and API** — create, rename, list, edit notes; versions and Undo as playbooks have.
  - **Verify:** tests create a client, save twice, restore the first version.
  - **Result:** `app/clients.py` on the version store playbooks now share (`app/versioned.py`). Routes under `/api/clients`. Clients cannot be deleted yet.

- [x] 🟩 **Step 20: A run follows its client's notes** — the run copies the notes when it is created, so later edits do not change it.
  - [x] 🟩 Shown to the agent beside the house rules, with one rule: where they disagree, the client wins and the notes say so
  - **Verify:** a test run's briefing contains the client's notes; a run with no client is unchanged.
  - **Result:** New run takes an optional `client_id`. The job keeps `client` and `client_notes`; the house-rules check after a pass covers the client notes too.

- [x] 🟩 **Step 21: Remembering has a scope** — a lesson goes to the client or the playbook.
  - [x] 🟩 The agent's remember tool takes the scope; a comment can set it
  - [x] 🟩 Undo works for both
  - **Verify:** tests save one lesson to each, and each lands in the right notes and can be undone.
  - **Result:** a comment's `remember_to` (client or playbook) wins over the agent's choice. Undo is a restore, on either. A client lesson gets no replay test (tests replay playbooks). Until the Clients screens (Step 37), the run shows a client lesson as a plain line with no Undo button.

- [x] 🟩 **Step 22: Earlier runs of the same client first** — as examples of layout.
  - **Verify:** a test with runs for two clients offers only the same client's.
  - **Result:** a run for a client is offered that client's runs, then runs with no client, never another client's. A run with no client is offered what it was before.

- [x] 🟩 **Step 23: Suggest the client** — from folder and file names when files are chosen; if left empty, the agent asks once when it sees a known client in the documents.
  - **Verify:** tests match "Acme/October 2026", "Payroll/acme_oct.xlsx" and "October/ACME Sdn Bhd" to Acme, and suggest nothing for an unrelated folder.
  - **Result:** `POST /api/clients/suggest` with the chosen paths. A name matches when its words appear in a row (legal words such as Sdn Bhd ignored); two clients equally named give no suggestion. A run with no client sees the known clients' names; its follow_client tool attaches one only after the user answered a question naming it.

### Phase 8: Does it work with the real model?

- [x] 🟩 **Step 24: Live runs** — the sample payroll packet and one long packet, before and after.
  - [x] 🟩 Compare accuracy, steps, time and tokens
  - [x] 🟩 One run stopped on purpose and continued from a version
  - [x] 🟩 One run with a client and one without
  - [x] 🟩 One run built from a format, with the look before finishing on and off: did the agent fix what it saw?
  - **Verify:** I give you the before and after numbers and the output files.
  - **Result (2026-11-26, direct OpenAI, gpt-6-luna, team A, one run each):**

    | Packet | Cells right before → after | Model calls | Minutes | Input tokens |
    |---|---|---|---|---|
    | payroll_consolidation | 100% → 100% | 4 → 6 | 3.4 → 4.2 | 267k → 294k |
    | large_packet (122 files) | 100% → 100% | 3 → 10 | 3.3 → 4.8 | 85k → 374k |
    | payment_journal (format), look on / off | 100% / 100% | 3 / 4 | 2.9 / 2.9 | 108k / 132k |

    Accuracy held. The cost rose because the agent now looks at its sheets with look_at_output, one sheet per step (up to six), and each step re-sends the conversation; the pictures themselves were 3–5k tokens a run. In these runs the looks found nothing to fix. Through the app: a run for a client finished with every cell right and followed its notes (notes.md opened with the line they asked for). A run with no client was paused after its sixth step and continued from version 1 (step 5); it finished with every cell right. Both reports' headlines described only the last check, not the whole job; the briefing now says the report covers the whole job (not yet tried live). Files: the scratchpad's `live/` folder, and `prototype/sandbox_agent/app_resume_client.py` reruns the app part.

### Phase 9: Comments that answer back (backend)

From the study of Claude's document panel (26 Nov). A comment gets an answer, what the agent names can be opened, and any two versions of an output can be compared. Built and tested before its screen, as in the other phases.

- [x] 🟩 **Step 25: The agent answers each comment** — one or two sentences per comment: what it changed, or why it left the spot as it was.
  - [x] 🟩 Given with the save, beside comment_changes (no new tool), and kept with the round's report
  - [x] 🟩 The round message asks for an answer to every comment; a save that leaves one unanswered says which
  - [x] 🟩 The code's changed / not changed verdict is kept as it is and shown beside the answer
  - **Verify:** a test round of three comments ends with three answers stored; a save missing one names it.
  - **Result:** `save_outputs(answers=[{comment, answer}])`; kept on the round's job entry as `answers`, a later answer replacing an earlier one. An answer naming no open comment is returned as `answers_not_kept` with its reason. `comment_changes.unanswered` lists the rest. The round message asks for answers and for cells as Sheet!B4.

- [x] 🟩 **Step 26: Reply and resolve** — a reply goes to the agent as a new round on the same spot, carrying the comment and the answer it replies to; resolve and reopen are stored on the run for the screen only.
  - **Verify:** a test replies to a comment and the agent's message shows the earlier exchange; a resolved comment stays resolved after a reload and sends nothing.
  - **Result:** a comment with `reply_to` ("1.2") takes that comment's spot, whatever the browser sent, and its line in the round message quotes the earlier comment and answer. A reply opens a resolved comment again. `POST /api/jobs/{id}/comments/resolve` with `{comment, resolved}`; rounds carry `resolved`.

- [x] 🟩 **Step 27: Links to the spot** — one briefing line: name cells as `Sheet!B4` and files by name in the report, answers and messages. The server marks the ones that exist (a sheet and cell of an output, a file of this run) and returns them with the text.
  - [x] 🟩 A sent comment carries its spot, so View opens the file there, not at the top
  - **Verify:** a test text with one real cell, one missing sheet and one file name returns two links and leaves the wrong one as text.
  - **Result:** `app/links.py`; `POST /api/jobs/{id}/links` with `{texts}` returns each text's links with their positions. A sheet found in two workbooks, a cell past the sheet's used area, or a file name two files share stays plain text. The report briefing names cells as Sheet!B4. The `feedback.sent` event carries each comment's spot.

- [x] 🟩 **Step 28: Compare with any version, and open an older one** — the changes API takes a version as well as the last round; the preview can read a file from a version.
  - [x] 🟩 Comments on an older version are refused; they go on the latest
  - **Verify:** tests compare the outputs with version 1 and list each changed cell with its old value; the preview of version 1 matches its copy; a comment on version 1 is refused.
  - **Result:** `/changes`, `/preview`, `/page` and `/file` take `version` (the file as that version kept it); `/changes` takes `since` (a version to compare with). Versions kept before files had their own folder still read. Comments have no version field, so they always point at the latest files; the screen offers no Comment mode on an older version.

### Phase 10: Screens (each batch after its prototype is approved)

- [x] 🟩 **Step 29: Waiting** (batch 1) — step and time counter, three named stages, plain step wording, the near-limit card with "Give it 20 more steps", "Clock paused" on a question.
  - **Verify:** start a run and watch each state appear; I check every state in a browser.
  - **Result:** stage line (Reading files › Filling in › Checking) and "Step 14 of 60 · 6 of 30 min" counter, "Clock paused" while a question waits; the near-limit card with "Give it 20 more steps" and "Let it wrap up"; plain feed wording. `/api/config` carries `more_steps`. The counter moves only when the agent takes a step.

- [x] 🟩 **Step 30: When a run stops** (batch 2) — plain message, Continue from the last version, the list of versions (the same list as the file panel's Version menu, Step 33), Start over, raw error folded away.
  - **Verify:** stop a run midway and continue from an earlier version on screen.
  - **Result:** a card for failed, paused and cancelled runs: plain sentence, next step, "Your work is saved up to step N", Continue, Earlier versions, Start over, raw error under "Technical detail". New `POST /api/jobs/{id}/start-over` empties the outputs, keeps every version and tells the agent it starts over. The version list is `web/versions.js`.

- [x] 🟩 **Step 31: Finished** (batch 3) — the report, the deliverable first with one Download, "Still to fix" shown once, "Totals add up" in the checks, the message box stays open as "Ask for a change".
  - **Verify:** finish a run; type a change request after it ends and the agent acts on it.
  - **Result:** the report card (headline, what I did, decisions, still to fix once, check before you send), deliverables with one Download, "Totals add up" in Checks, "Ask for a change" in the message box. A partly done run keeps `stopped_because`. "How it was done" left out: the Technical log already covers it.

- [x] 🟩 **Step 32: Reviewing** (batch 4) — which value came from which file, notes shown formatted, Send to agent only when there are comments, the remember scope on a comment.
  - **Verify:** click a row and read its sources; add a comment and choose where it is remembered.
  - **Result:** "Row N came from" lists each value with its file and place (page, row, line), and a formula cell as "Worked out"; `.md` files are shown formatted by a small safe renderer (`web/markdown.js`) with line comments kept; the comment box has the remember scope; Send to agent shows only while comments wait.

- [x] 🟩 **Step 33: Comments that answer back** (batch 7) — the answer under each comment with Reply and Resolve; the verdict's colour on the pins and on the sent-comments card; cell and file links in the chat and answers that open the spot; the Version menu in the file header; "Since" on the Changes switch, with Previous and Next; an older version with Continue from here (Step 30) and Back to latest.
  - **Verify:** send three comments, read the answers, reply to one and resolve one; click a cell link in the chat; compare with version 1 and step through the changes. I check every state in a browser.
  - **Result:** answers, Reply and Resolve under each comment, verdict colours on pins and the sent card, checked links in the report, answers and messages (`web/links.js`), the Version menu, "Since" with Previous and Next on Changes, an older version with Continue from here and Back to latest. A thread counts as resolved when its latest message is.

- [x] 🟩 **Step 34: Panels that resize** — both dividers drag, double-click hides the run list, widths are remembered.
  - **Verify:** drag both, reload, and the widths hold; a small laptop width still shows everything.
  - **Result:** `web/panels.js`: both dividers drag and take arrow keys; double-click or Enter hides the run list, with Show runs to bring it back; widths kept in localStorage and narrowed to fit. The file header stays on one line; zoom, find, full screen and download fold into More when the panel is narrow.

- [x] 🟩 **Step 35: Run list** (batch 5) — each run's own name and status, rename and delete.
  - **Verify:** rename a run and delete one; a running run cannot be deleted.
  - **Result:** `PATCH /api/jobs/{id}` renames (empty goes back to the goal's name); `DELETE /api/jobs/{id}` is refused while a run is going and asks to confirm on screen. `web/runs.js`. Duplicate left out.

- [x] 🟩 **Step 36: Settings** (batch 5) — plain labels with units, the error beside the field, Save lit only after a change, plus the new values from Steps 5, 7, 12 and 14 and the questions-per-run limit.
  - **Verify:** type a wrong value and read the message beside it; every setting in the app has a control.
  - **Result:** grouped as in the prototype, plain labels with units, the server's message beside the field it names (`SettingError.field`), Save lit only after a change. New setting "Questions it may ask per run" (0-10, default 3) replaces the fixed three.

- [x] 🟩 **Step 37: Clients** (batch 6) — the Clients page, one client's notes and history, the client box on New run with its suggestion, "Following the notes for Acme" on the run.
  - **Verify:** create a client, start a run with it suggested, and see a remembered comment appear in its notes with Undo.
  - **Result:** `web/clients.js`: the Clients page, one client's Notes, Runs and History, the client row on New run with its suggestion and Use, "Following the notes for X" on the run, Undo for lessons and comments remembered to a client. The "Ask the agent to change these notes" box is left out: there is no backend for it yet.

**How Phase 10 was checked (2026-11-27):** each batch was checked in a browser on pretend runs (a scratch data folder with no API key, states written straight into the store), at desktop and phone widths; no live agent run was started, so a continue or start over that really runs, and answers written by the real model, are not yet seen on screen. A review (8 specialists, Codex, an independent checker) kept seven findings, all fixed: a row-0 cell name broke every link; a cancelled run deleted while still queued stopped the job worker; Start over could lose files newer than the last version (it now keeps them as a version first and asks to confirm); a client's runs were matched by name, not id; "Questions it may ask per run" counts the whole job, so it now says "per job"; the save tool and the round message disagreed on which comments to answer; and Continue could not restore a version kept before files had their own folder (that fix touches the other uncommitted work's version layout). A second Codex review found two more, also fixed: the stopped card's main Continue restored the last version and could discard newer files (it now carries on from the current files; going back to a version first keeps the current files as a version unless they match the newest one), and the report hid questions the agent left open (now under "Still open").

### Phase 11: Keep it from drifting

- [ ] 🟥 **Step 38: The no-hidden-settings test and rule** — a test fails when a Settings option has no control on the Settings page; the project instructions state the rule for features and limits.
  - **Verify:** I add a dummy setting, show the test failing, and remove it.

- [ ] 🟥 **Step 39: Tidy and review** — README brought up to date (it still describes a "Remember for next time" tick), the prototype moved to its own branch, full tests and build, `ship-review`.
  - **Verify:** I report the test and build results, the review verdict, and the git state.

### Phase 12: Speak instead of type (its own branch, after Phase 11)

Asked for 2026-11-27. A microphone button on every box people write in, like Codex and Claude Code have. Press it and speak; the words appear in the box as you talk; press again to stop, then edit before sending. Uses OpenAI's newest transcription models (July 2026), not Whisper or gpt-4o-transcribe, which OpenAI shuts down on 26 Feb 2027:
- **gpt-live-transcribe** for live text while speaking: a Realtime transcription session over a WebSocket (`/v1/realtime?intent=transcription`), fed 24 kHz mono PCM16 audio, sending the words back as deltas.
- **gpt-transcribe** as the fallback: the recording is sent when the person stops, and the text streams back (`/v1/audio/transcriptions` with `stream=true`).

The browser never holds the key. It sends its audio to this app, which opens the session to the gateway and passes the words back.

- [ ] 🟥 **Step 40: Try both through the gateway** — a short synthetic recording through the gateway: a live session with gpt-live-transcribe, and a streamed file transcription with gpt-transcribe. Confirm the aliases the gateway uses for both.
  - **Verify:** I report which of the two the gateway passes, how soon the first words come back, and the cost of one minute of speech. If the gateway does not pass the live WebSocket, Phase 12 is built on the fallback alone and the words appear when the person stops, not while they speak; you decide before Step 41.

- [ ] 🟥 **Step 41: A transcription relay** — a WebSocket route on this app: the browser streams audio in, the app relays it to the gateway's live session and streams the words back. A plain `POST /api/transcribe` for the fallback. Both model aliases, the mode (live or after stopping) and a length limit are Settings values; the key stays in `.env`. No recording is kept, and the same access rules as the rest of the app apply.
  - **Verify:** tests with a fake gateway: words arrive in order while audio is still being sent, the fallback returns the text, a recording past the limit is stopped with a plain sentence, a lost gateway connection says so, and nothing is written to disk.

- [ ] 🟥 **Step 42: The microphone button** (prototype first) — one shared button on every text box: the message box, comment and reply boxes, New run's goal, playbook and client notes. The words appear where the cursor was as they are spoken, then settle when the person stops. A plain message when the browser blocks the microphone, and when the app is opened over a network without HTTPS (browsers allow the microphone only on a secure address or on this computer).
  - **Verify:** I check each box in a browser, plus a blocked microphone and a gateway that refuses.

## Risks

- **Compaction may not trigger** with our settings or through the gateway (one user has reported this). Step 6 finds out first.
- **Compaction can lose detail.** Step 8 and the notes file are the guard; Step 24 measures accuracy before and after.
- **A wrong client suggestion** would apply one client's rules to another. The choice is always visible before Start and never applied silently.
- **Pictures cost tokens.** A few per run, kept small, and switched off on the Settings page if needed. Step 24 measures the cost.
- **A picture that differs from Excel** could send the agent to fix something that is fine. Step 10's tests compare against known features, and the picture names what it leaves out.
- **Versions use disk space.** The cap in Step 14 limits it.
- **An answer can claim a change that did not happen.** The code's verdict is shown beside it and wins where they differ.
- **Answers add a little output to every round.** One or two sentences per comment.

## Rollback Plan

- Every phase is its own set of commits on a feature branch; any phase can be reverted without the others, except that Phase 10 needs the phase behind each screen.
- Compaction, versions and more-steps are Settings values, so each can be switched off without a code change.
- New data (clients, versions) is added beside existing data. Existing runs and playbooks are not altered, so reverting leaves them as they were. Check that older runs still open after each phase.

## Not in this plan

- **User accounts** (sign-in, who did what, roles): on hold, asked 2026-11-27. Today one optional shared access code guards the app.

Found in the audit but not agreed. Say if you want any added:

- Resetting the count of dropped connections after a good step (a small fix; I'd add it first)
- A ready-made tool for inserting rows into a template
- A faster Stop
- Removing repeated guidance from the agent's briefing
- The same error code and wording for the same problem across the API
- Using the server's live feed in place of asking every 1.5 seconds
- Trimming old pictures and printouts from the history (the fallback if compaction disappoints)
- Reference files attached to a client
- Phone-size layout
- Deliverables other than Excel (Word, PDF, CSV). The sandbox could make them, but the app, its checks and its pictures handle .xlsx only
