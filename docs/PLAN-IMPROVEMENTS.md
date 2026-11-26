# Implementation Plan: A steadier agent, clearer runs, and client notes

**Overall Progress:** `0%`
**Builds on:** `docs/PLAN.md` and `docs/PLAN-NEXT.md` (both built), and the chat-and-zoom branch (PR yeeliang94/document_agent#1)
**Design reference:** `prototype/improvements/index.html`, six batches (there is no PRD)
**Last Updated:** 2026-11-26

## Summary

Three things change. The agent learns its limits, keeps its context small, and sees what its formulas add up to. The person sees how far a run has got, gets a plain message and a way back when a run stops, and gets a proper report when it finishes. Each client gets its own notes, which a run can follow on top of its playbook.

As in the earlier plans, the engine and backend are built and tested first. The screens follow, one prototype batch at a time, each after that batch is approved.

## How it fits the app today

| Today | After |
|---|---|
| The agent is cut off at a limit with no warning | It is told when it is close, saves, and writes down what is open |
| The whole history is re-sent on every step | OpenAI condenses older history past a size set on the Settings page |
| A formula's result is unknown to the agent and the checks | Every save reports the totals and any formula that fails |
| A stopped run keeps only the last save | Every save is a version; a run can continue from any of them |
| A failed run shows the raw error | A plain sentence, with the raw text folded away |
| The finish message is one paragraph | A report: what was done, decisions made, what to check |
| Instructions belong to a playbook only | A client has notes too; where they disagree with the playbook, the client wins |
| Earlier runs offered as examples can be another client's | The same client's earlier runs are offered first |
| Panels have fixed widths | Both dividers drag; the run list can be hidden |

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
- **No setting or limit without a control.** A test fails when a Settings option has no control on the Settings page, and the project instructions say so.

## Pre-Implementation Checklist

- [x] 🟩 Prototype approved: all six batches as drawn (2026-11-26). The screens also keep what chat-and-zoom added (files in the message box, Send now, zoom).
- [x] 🟩 The three client decisions confirmed: main form, optional, text only
- [x] 🟩 How client folders are named: no fixed pattern, so the suggestion looks for a known client's name anywhere in folder and file names
- [x] 🟩 An OpenAI key is set on this machine, and live steps may use it; each live run is announced with its rough cost first
- [x] 🟩 No conflicting in-progress work: chat-and-zoom is committed (PR yeeliang94/document_agent#1), and this plan builds on that branch

## Tasks

### Phase 1: The agent knows its limits (engine)

- [ ] 🟥 **Step 1: Only describe tools the agent has** — the briefing mentions scouts only when scouts are switched on.
  - [ ] 🟥 Build the scout line the way the source-proof line is already built
  - **Verify:** tests pass; a test reads the briefing with scouts at 0 and finds no mention of them.

- [ ] 🟥 **Step 2: A warning before each limit** — at about 80% of the step, time or token limit the agent is told what is left and to save and note what is open.
  - [ ] 🟥 One message per limit, sent once
  - [ ] 🟥 An event the screen can show ("8 steps left")
  - **Verify:** a test with a 10-step limit shows the warning reaching the agent at step 8 and a save before the cut.

- [ ] 🟥 **Step 3: A passed run stays passed** — if a limit is hit during the follow-up checks after the work already passed, the run still ends as Finished.
  - **Verify:** a test that hits the limit in the follow-up ends as Finished, not Partly done.

- [ ] 🟥 **Step 4: The clock stops while a question waits** — time spent waiting for your answer no longer counts against the run's time limit.
  - [ ] 🟥 The "no answer yet" reply tells the agent to write its findings to the notes and save before it ends
  - **Verify:** a test with a 1-minute limit and a 2-minute wait finishes normally once answered.

- [ ] 🟥 **Step 5: More steps on request** — a paused or nearly finished run can be given 20 more steps.
  - [ ] 🟥 One API route; the amount is a Settings value
  - **Verify:** a test extends a run at its limit and it continues without restarting.

### Phase 2: A smaller context (engine)

- [ ] 🟥 **Step 6: Try compaction against the real endpoint** — a long scripted run with compaction on, before building on it.
  - [ ] 🟥 Confirm it triggers, with our settings and through the gateway
  - [ ] 🟥 Record what the condensed history keeps and loses
  - **Verify:** I report the result. If it does not trigger, we stop here and choose again; nothing else depends on it yet.

- [ ] 🟥 **Step 7: Compaction with its trigger on the Settings page** — "Condense history at" with a tested default.
  - [ ] 🟥 Setting, with a range; passed to every agent call
  - [ ] 🟥 An event when it happens, shown in the Technical log
  - **Verify:** change the value on Settings, start a run, and the Technical log shows the condense event at that size.

- [ ] 🟥 **Step 8: Notes as lasting memory** — one general line in the briefing: write findings and decisions to the notes as you go.
  - **Verify:** in a live run the notes file fills in during the run, not only at the end.

### Phase 3: The agent sees its totals (engine)

- [ ] 🟥 **Step 9: Work out formulas on every save** — the save reply lists each total with its value and any formula that fails or misses rows.
  - [ ] 🟥 Reuse the calculator the preview already uses
  - [ ] 🟥 Add "Totals add up" to the check summary
  - **Verify:** a test saves a workbook whose total misses the last row; the reply names the cell and the run does not pass.

### Phase 4: Runs that stop can be picked up (backend)

- [ ] 🟥 **Step 10: A version at every save** — a copy of the outputs, with the step number, time and a one-line description.
  - [ ] 🟥 A cap on versions kept per run, on the Settings page
  - [ ] 🟥 API: list a run's versions
  - **Verify:** run the demo; the API lists one version per save, newest first.

- [ ] 🟥 **Step 11: Continue from a version** — restores that copy as the outputs and resumes from it.
  - [ ] 🟥 The resume note tells the agent which version it starts from
  - **Verify:** a test stops a run after two saves, continues from the first, and the outputs match the first.

- [ ] 🟥 **Step 12: Plain errors** — one place turns a failure into a plain sentence and a next step; the raw text is kept for the Technical log.
  - [ ] 🟥 Used for run failures, tool failures and the connection test
  - [ ] 🟥 Form errors return a sentence, not "Request failed (422)"
  - [ ] 🟥 "See the run record" replaced by the issues themselves
  - **Verify:** tests feed in the known failures (no key, lost workspace, refused, over the day's limit) and read back plain sentences.

- [ ] 🟥 **Step 13: Progress the screen can show** — the run carries steps used, minutes used, both limits and its stage.
  - **Verify:** the run's API answer shows "14 of 60" style numbers changing during a demo run.

### Phase 5: A proper finish report (engine)

- [ ] 🟥 **Step 14: The agent ends with a report** — a headline, what it did, decisions it made, what to check before sending.
  - [ ] 🟥 Stored with the run; the downloadable summary file uses the same text
  - **Verify:** a test run ends with all four parts filled; an older run still opens and shows its one paragraph.

### Phase 6: Clients (backend and engine)

- [ ] 🟥 **Step 15: Client storage and API** — create, rename, list, edit notes; versions and Undo as playbooks have.
  - **Verify:** tests create a client, save twice, restore the first version.

- [ ] 🟥 **Step 16: A run follows its client's notes** — the run copies the notes when it is created, so later edits do not change it.
  - [ ] 🟥 Shown to the agent beside the house rules, with one rule: where they disagree, the client wins and the notes say so
  - **Verify:** a test run's briefing contains the client's notes; a run with no client is unchanged.

- [ ] 🟥 **Step 17: Remembering has a scope** — a lesson goes to the client or the playbook.
  - [ ] 🟥 The agent's remember tool takes the scope; a comment can set it
  - [ ] 🟥 Undo works for both
  - **Verify:** tests save one lesson to each, and each lands in the right notes and can be undone.

- [ ] 🟥 **Step 18: Earlier runs of the same client first** — as examples of layout.
  - **Verify:** a test with runs for two clients offers only the same client's.

- [ ] 🟥 **Step 19: Suggest the client** — from folder and file names when files are chosen; if left empty, the agent asks once when it sees a known client in the documents.
  - **Verify:** tests match "Acme/October 2026", "Payroll/acme_oct.xlsx" and "October/ACME Sdn Bhd" to Acme, and suggest nothing for an unrelated folder.

### Phase 7: Does it work with the real model?

- [ ] 🟥 **Step 20: Live runs** — the sample payroll packet and one long packet, before and after.
  - [ ] 🟥 Compare accuracy, steps, time and tokens
  - [ ] 🟥 One run stopped on purpose and continued from a version
  - [ ] 🟥 One run with a client and one without
  - **Verify:** I give you the before and after numbers and the output files.

### Phase 8: Screens (each batch after its prototype is approved)

- [ ] 🟥 **Step 21: Waiting** (batch 1) — step and time counter, three named stages, plain step wording, the near-limit card with "Give it 20 more steps", "Clock paused" on a question.
  - **Verify:** start a run and watch each state appear; I check every state in a browser.

- [ ] 🟥 **Step 22: When a run stops** (batch 2) — plain message, Continue from the last version, the list of versions, Start over, raw error folded away.
  - **Verify:** stop a run midway and continue from an earlier version on screen.

- [ ] 🟥 **Step 23: Finished** (batch 3) — the report, the deliverable first with one Download, "Still to fix" shown once, "Totals add up" in the checks, the message box stays open as "Ask for a change".
  - **Verify:** finish a run; type a change request after it ends and the agent acts on it.

- [ ] 🟥 **Step 24: Reviewing** (batch 4) — which value came from which file, notes shown formatted, Send to agent only when there are comments, the remember scope on a comment.
  - **Verify:** click a row and read its sources; add a comment and choose where it is remembered.

- [ ] 🟥 **Step 25: Panels that resize** — both dividers drag, double-click hides the run list, widths are remembered.
  - **Verify:** drag both, reload, and the widths hold; a small laptop width still shows everything.

- [ ] 🟥 **Step 26: Run list** (batch 5) — each run's own name and status, rename and delete.
  - **Verify:** rename a run and delete one; a running run cannot be deleted.

- [ ] 🟥 **Step 27: Settings** (batch 5) — plain labels with units, the error beside the field, Save lit only after a change, plus the new values from Steps 5, 7 and 10 and the questions-per-run limit.
  - **Verify:** type a wrong value and read the message beside it; every setting in the app has a control.

- [ ] 🟥 **Step 28: Clients** (batch 6) — the Clients page, one client's notes and history, the client box on New run with its suggestion, "Following the notes for Acme" on the run.
  - **Verify:** create a client, start a run with it suggested, and see a remembered comment appear in its notes with Undo.

### Phase 9: Keep it from drifting

- [ ] 🟥 **Step 29: The no-hidden-settings test and rule** — a test fails when a Settings option has no control on the Settings page; the project instructions state the rule for features and limits.
  - **Verify:** I add a dummy setting, show the test failing, and remove it.

- [ ] 🟥 **Step 30: Tidy and review** — README brought up to date (it still describes a "Remember for next time" tick), the prototype moved to its own branch, full tests and build, `ship-review`.
  - **Verify:** I report the test and build results, the review verdict, and the git state.

## Risks

- **Compaction may not trigger** with our settings or through the gateway (one user has reported this). Step 6 finds out first.
- **Compaction can lose detail.** Step 8 and the notes file are the guard; Step 20 measures accuracy before and after.
- **A wrong client suggestion** would apply one client's rules to another. The choice is always visible before Start and never applied silently.
- **Versions use disk space.** The cap in Step 10 limits it.

## Rollback Plan

- Every phase is its own set of commits on a feature branch; any phase can be reverted without the others, except that Phase 8 needs the phase behind each screen.
- Compaction, versions and more-steps are Settings values, so each can be switched off without a code change.
- New data (clients, versions) is added beside existing data. Existing runs and playbooks are not altered, so reverting leaves them as they were. Check that older runs still open after each phase.

## Not in this plan

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
