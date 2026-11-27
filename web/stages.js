// A run's work cut into its stages (Reading files, Filling in, Checking), and the one line each stage folds to.
// Plain functions of the thread's entries, with no page or app state, so tests/test_run_work.py can run them.

const count = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
export const stepFiles = (s) => (s.file ? [s.file] : s.files || []);

// The block's entries cut where the stage changes. A tick (plan steps done) is kept with the stage it came in.
export function stagesOf(entries) {
  const out = [];
  for (const en of entries) {
    const name = en.stage || 'reading';
    // A tick for an earlier stage (a step ticked once the next stage began) goes back to that stage.
    const earlier = en.kind === 'tick' ? out.findLast((sec) => sec.stage === name) : null;
    if (earlier) {
      earlier.ticked.push(...en.steps);
      continue;
    }
    // An action's time starts when it started, so the stage's time covers its first action too.
    const start = en.at - (en.step?.secs || 0) * 1000;
    let sec = out[out.length - 1];
    if (!sec || sec.stage !== name) {
      sec = { id: en.seq, stage: name, from: start, to: en.at, entries: [], ticked: [] };
      out.push(sec);
    }
    if (en.kind === 'tick') sec.ticked.push(...en.steps);
    else sec.entries.push(en);
    sec.from = Math.min(sec.from, start);
    sec.to = Math.max(sec.to, en.at);
  }
  return out.filter((sec) => sec.entries.length || sec.ticked.length);
}

// Which stage each plan step belongs to: the stage of the first action taken after the agent said it was on the
// step. It says so just before it starts, while the run's stage still names the stage before (the stage moves on
// only with the first action of the next kind). An action is work: a script or a save, not a status line. A step
// ticked with no action since takes the stage it was ticked in.
export function stepStages() {
  const known = new Map();
  let waiting = [];
  return {
    on(steps) {
      for (const s of steps) if (!known.has(s) && !waiting.includes(s)) waiting.push(s);
    },
    acted(stage) {
      for (const s of waiting) known.set(s, stage);
      waiting = [];
    },
    // The stage a step just ticked off counts towards; it no longer waits for an action.
    done(step, fallback) {
      if (waiting.includes(step)) {
        waiting = waiting.filter((s) => s !== step);
        known.set(step, fallback);
      }
      return known.get(step) || fallback;
    },
  };
}

const asSentence = (text) => (/[.!?]$/.test(text) ? text : text + '.');

// A stage in a line: the plan steps ticked off in it, or with none ticked, how much work the stage held. The step the
// agent is on now shows on the plan, not here as well. Costs nothing: no AI is asked for it.
export function stageSummary(sec) {
  if (sec.ticked.length) return sec.ticked.map(asSentence).join(' ');
  const steps = sec.entries.filter((en) => en.kind === 'step').map((en) => en.step);
  const files = new Set(steps.flatMap(stepFiles)).size;
  return `${count(steps.length, 'action')}${files ? ` on ${count(files, 'file')}` : ''}`;
}
