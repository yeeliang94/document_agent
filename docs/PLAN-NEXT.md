# Implementation Plan: A general agent that learns and checks itself

**Overall Progress:** `95%` (Phases 1 to 6 are built, reviewed and run live; scanned pages, failure handling and a daily limit are done; several runs at once and a money figure are not built)
**Builds on:** `docs/PLAN.md` (Playbooks, all 8 phases done, branch `playbooks`)
**Last Updated:** 2026-11-26

**Closed 2026-11-26:** Steps 17 and 18 moved to `docs/PLAN-IMPROVEMENTS.md` (Phase 15, Steps 51-52). The checklist below was settled while building: the tick box was removed (Step 9), replay defaults to large changes (Step 14), the playbook work is on `main` (brought in through a different branch, so the old `playbooks` branch itself was never merged), and the live runs were done (Steps 5, 7, 10). Step 20's final report is replaced by each phase's own review.

## Summary

Four things change. A run needs only a goal and files. The agent can look at earlier runs of the same playbook and can run programs that come with a skill. The agent decides by itself what to remember from a correction, keeps a small set of past corrections as tests, and replays them before any large change to the instructions. The person sees a score and an Undo, and manages no lists.

## Who does what

| Job | Today | After |
|---|---|---|
| Decide whether a correction is a lasting lesson | Person ticks "Remember for next time" | Agent decides, says what it learned, person can Undo |
| Keep the list of tests | Does not exist | Agent adds, merges and retires them, within a fixed limit |
| Keep the instructions tidy | Person asks for a rewrite | Agent rewrites when lessons pile up, only if the tests still pass |
| Choose what a run needs | Person picks template and month | Person gives goal and files; the rest is optional |

## Key Decisions

- **The agent that fixed the mistake writes the lesson.** It gets a `remember` tool. It knows why it was wrong, so it words the lesson as a general rule. Why: good tools and general principles, not a second model guessing afterwards.
- **A lesson is saved only when the round ends well.** If the fix failed its checks, nothing is remembered. Every lesson is a new version with Undo, as today.
- **One test per lesson.** A test is the run's input files, the commented spots and the values the person accepted. Why: ties every test to the rule it protects, so it can be retired with the rule.
- **A hard limit of 20 tests per playbook, kept by the agent.** A "curator" step runs when instructions change. It retires tests whose lesson is gone, merges duplicates, and drops the oldest idle test when the limit is hit. Every action is logged. The person never has to tidy.
- **Replay costs money, so it runs only on large changes.** A tidy or a rewrite replays the tests in the background. A single new lesson does not. A Settings switch chooses: never, large changes (default), every change.
- **A tidy is applied only if the replay passes.** If any test fails, the tidy is dropped and logged. Why: this is where the tests earn their keep.
- **Skills may contain Python programs.** They run in the agent's sealed workspace, which cannot reach this computer. The skill dialog shows every program before the skill is switched on. Other program types stay refused.
- **Earlier runs are offered, not forced.** The goal card lists the last few finished runs of the playbook. The agent opens one with a tool when it helps. Each opening is recorded.
- **A run without a template still gets checked.** The file decisions, notes and row sources still apply. Only the template comparison is skipped.
- **Settings go on the Settings page.** Limits that protect the app stay as constants in code.

## Pre-Implementation Checklist

- [ ] 🟥 Owner confirms the tick box goes away and the agent decides what to remember
- [ ] 🟥 Owner confirms replay default: large changes only
- [ ] 🟥 Branch `playbooks` pushed and its pull request opened, so this work starts from a reviewed base
- [ ] 🟥 Live (paid) scorecard runs approved for Phases 2, 3, 4 and 5

## Tasks

### Phase 1: A run needs only a goal and files

- [x] 🟩 **Step 1: Prototype the New run form** — goal and files required; template, format and month optional under "More options".
  - **Verify:** owner approves the prototype.
- [x] 🟩 **Step 2: Backend** — `POST /api/jobs` accepts a job with no template and no month; the run name comes from the goal; the save checks skip only the template comparison; outputs of any previewable type are accepted.
  - **Verify:** tests for a run with no template and no month; old runs still open; one scorecard packet runs with no template.
- [x] 🟩 **Step 3: Screens** — build the approved form.
  - **Verify:** every state checked in the browser at desktop and phone width.

### Phase 2: The agent can look at earlier runs

- [x] 🟩 **Step 4: Past-run tools** — the goal card lists up to three finished runs of the same playbook (name, date, open comments). `open_past_run` puts that run's outputs and notes in the workspace. Each opening is logged and stored in the run record.
  - **Verify:** tests for listing, opening, a missing run and a run of another playbook (refused).
- [x] 🟩 **Step 5: Scorecard packet** — a packet that can only be done right by following last month's output.
  - **Verify:** live run with and without the past run; report both scores.

### Phase 3: Skills with programs

- [x] 🟩 **Step 6: Accept Python in a skill ZIP** — same size and safety limits; the skill row shows "contains programs"; the dialog shows each program's text.
  - **Verify:** tests for accept, refuse (other program types), and fingerprint change.
- [x] 🟩 **Step 7: Agent runs them** — the skill card names the programs; the general rule becomes "run a skill's program where it covers the work, do not rewrite it". Runs are found in the recorded scripts and logged as `skill.run`.
  - **Verify:** scorecard packet whose answer only the program can give; live run with and without.

### Phase 4: The agent decides what to remember

- [x] 🟩 **Step 8: `remember` tool** — available during a comment round. The agent passes the lesson and the comment it came from. Saved as a version (source `learned`) when the round closes well. Shown in the conversation as "Learned: …" with Undo.
  - **Verify:** tests for save on success, nothing saved on failure, Undo, duplicate lesson refused.
- [x] 🟩 **Step 9: Remove the tick box** — after the owner approves the changed popup in a prototype.
  - **Verify:** browser check of the comment flow.
- [x] 🟩 **Step 10: Capture a test with each lesson** — input files, spots, accepted values, lesson text. Stored under the playbook, not the run, so deleting a run does not break it.
  - **Verify:** tests for capture and storage; scorecard `payroll_remember` still passes live.

### Phase 5: Replay, curate and score

- [x] 🟩 **Step 11: Replay** — runs a playbook's tests against given instructions through the real engine, in the background, one at a time, and scores each commented spot.
  - **Verify:** offline tests with a fake engine; one live replay.
- [x] 🟩 **Step 12: Curator** — one plain model call on each instructions change: keep, merge or retire each test. Enforces the limit of 20. Logs every action with its reason.
  - **Verify:** tests for retire-with-rule, merge, limit, and a model failure (nothing changes).
- [x] 🟩 **Step 13: Score on the playbook** — prototype, then build: "Past corrections: 14 of 15 still right", last checked date, and a read-only list.
  - **Verify:** owner approves prototype; browser check of every state.
- [x] 🟩 **Step 14: Settings switch** — never, large changes, every change.
  - **Verify:** test for each setting.

### Phase 6: The agent keeps the instructions tidy

- [x] 🟩 **Step 15: Automatic tidy** — when learned lessons pass a count or the text passes a length, the existing rewrite step proposes a consolidated version. It is replayed. Pass: saved as a version (source `tidied`) with Undo. Fail: dropped and logged.
  - **Verify:** tests for pass, fail, and a person's edit arriving mid-replay (tidy dropped as out of date).

### Phase 7: Day-to-day gaps

- [x] 🟩 **Step 16: Activity tab on a playbook** — shows the record that exists today but has no screen. Prototype first.
- [ ] 🟨 **Step 17: Cost and time per run** (time and model usage per job are shown in the Technical log; a money figure and per-playbook totals are not built) — shown on the run and summed per playbook.
- [ ] 🟥 **Step 18: Several runs at once** — a Settings number, default 2; replays share the same limit.
  - **Verify:** tests for two runs side by side, pause and resume of one, restart with both running.
- [x] 🟩 **Step 19: Scanned pages** — scorecard packet `scanned_statements`: two scanned statements (30 to 40 pages each, no text) and three phone-like photos. The scans are real documents: they and their figures stay outside the repository (folder named by `SCANNED_SAMPLES`).
  - **Verify:** live, every figure right in one run with the old prompt and one run with the new rule on harder photos. One run each, so a first reading only.
- [x] 🟩 **Step 19a: When the AI service fails** — a dropped reply is retried after 2, 10 and 30 seconds. If it still fails, the run stops as partly done with its saved work and can be continued. A wrong key, or an address that never answers, still fails with the reason. A replayed test stopped this way counts as not checked, never as wrong. Not run live: the failure cannot be caused on demand.
- [x] 🟩 **Step 19b: Daily token limit** — a Settings number (default 30 million, 0 for none). Every run counts, replayed tests and rewrite proposals included, also a run cut off by a restart. With under 50,000 tokens left, a run waits as paused and says why; no tests are reviewed or replayed. Not counted: the short call that reviews the list of tests. A check the limit interrupts midway is closed as not checked and is not picked up again by itself.

### Phase 8: Review and hand over

- [ ] 🟥 **Step 20** — each phase gets its own ship review as it finishes; this step is the final report: tests, anything not verified, git state.

## What changed while building (22 Nov)

- **Each phase was reviewed by Codex as it was committed.** Every finding was valid and fixed; none rejected.
- **A skill with programs arrives switched off** until the user has read them, and is switched off again when its programs change.
- **"A program ran" is recorded from the sandbox,** by a note the program leaves when it starts, not from the text of the agent's scripts.
- **A lesson is decided on how the job really ended:** a late cancel drops it, an instruction the agent never read defers it.
- **A replayed correction is scored on its own record,** found by a cell no other row shares, not on the cell's position.
- **Live runs made on 22 Nov** (gpt-6-luna, one agent, no scouts or reviewer). See "Live results" below.
- **After the checks pass, the agent now holds its saved work against the house rules, once.** The live runs showed it writing "Everly, Casey" under a rule whose example was "EVERLY, Casey". Wording alone fixed it only half the time; the extra step fixed it in every re-run. It adds roughly half a minute to a run.
- **Prototype approved 22 Nov** (kept locally, not published); the screens were built from it.

## Live results (22 Nov 2026)

| What was tested | Result |
|---|---|
| Six earlier packets, incl. the 120-file one | All complete, cells 100% right. `payroll_noise` found 85% of rows and asked one question it did not need to. |
| Skill with a program | 100% and the program ran; 80% without the skill |
| Earlier run as example | 100% and the run was opened; 60% without it |
| Comment fixes one cell | Proven: only that cell changed |
| "Remember" tick (your words) | First pass 1 of 3 proven; after the fixes 3 of 3 |
| Agent learns by itself | Lesson saved every time (3 of 3 before the last fix, 1 of 1 after). Fresh run right and its test replayed right in 3 of 4. |
| Goal only, no template (payment packet) | Complete, checks passed, 112 of 112 rows sourced; journal lines match the answer key's count |

Each result is from one to three runs, so treat the percentages as a first reading, not a measured rate.

## Live results, second round (22 Nov 2026)

| What was tested | Result |
|---|---|
| Scanned statements, agent as it was | Complete, every figure right, 6.8 minutes. It used OCR only and never looked at a page. |
| Scanned statements, photos made harder (soft, crooked, one sideways), with the new "read it by eye" rule | Complete, every figure right, 6 minutes. It looked at the pages. |
| Independent reviewer on `payroll_noise` | Worked: one real doubt raised and handled. Rows found 92%, one unneeded question. |
| Through the app's screens: comment, lesson, kept test, automatic tidy | All worked. The tidy was saved as a version after its check passed. The agent asked one question the comment already answered. |
| "Check now" on the playbook page | Worked: 1 of 1 still right, and the score updated on the open page by itself. |
| Start run on the scanned statements, from the screen | Complete in 9 minutes, every figure right. A comment sent with the day's limit used up paused the run and showed the reason. |

Found and fixed in this round: a tidy that passed its check was saved, but the playbook still showed "Not checked yet". The review (four reviewers and Codex) then found that an AI outage or the day limit could mark a kept test as wrong, and that a run cut off by a restart was not counted towards the day; both are fixed.

## Risks

- **A wrong lesson spreads to every later run.** Guards: saved only after a passing round, shown with Undo, versioned, and tested on the next large change.
- **Replay cost.** A test is a full run. Guards: limit of 20, large changes only by default, cost shown per playbook.
- **A curator mistake retires a useful test.** Guards: logged with reason; retired tests are kept for 30 days and can be brought back.
- **A skill program does damage.** It can only touch that run's workspace. Guards: programs shown before switching on; outputs still pass the save checks.
- **Several runs at once on one small database.** Guard: Phase 7 tests cover it; the default stays low.

## Rollback Plan

- Each phase is its own commit set on one branch and can be reverted alone.
- Data is only added. Lessons are ordinary versions, so restoring an earlier version undoes any of them.
- The Settings switch set to "never" turns replay off without a code change.

## Not in this plan

- Connectors to mail and shared drives.
- Programs in skills other than Python.
- Starting runs automatically when files arrive.
