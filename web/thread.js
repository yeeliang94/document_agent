// The conversation: what the person asked, what the agent said, and the work it did in between.
// Built from the job's events on every change, so a reloaded or resumed run reads the same as a live one.

import {
  attr, baseName, btn, duration, esc, icon, inGroup, isActive, isWarning, jobStatus, jobTitle, pill, playbooks,
  plural, state, STATUS, storedPath,
} from './core.js'; // prettier-ignore
import { learnedHtml, roundHtml } from './comments.js';

// Engine events that are housekeeping, or that another item already shows.
const QUIET = new Set([
  'engine.question.answered', 'engine.run.finished', 'engine.instructions.delivered', 'engine.model.retry',
]); // prettier-ignore
const ENDINGS = ['job.completed', 'job.partial', 'job.failed'];

const sentence = (text) => {
  const clean = String(text || '').replace(/_/g, ' ').trim();
  return clean.charAt(0).toUpperCase() + clean.slice(1);
};
const time = (event) => new Date(event.timestamp).getTime();

// Runs made before the sandbox engine logged one event per tool, with the file it worked on.
function toolStep(event) {
  const failed = isWarning(event) || event.status === 'failed';
  return {
    seq: event.seq,
    at: time(event),
    verb: sentence(event.message || event.name),
    file: event.arguments?.path || event.arguments?.filename || '',
    more: failed ? 'did not go through' : '',
    flag: failed,
  };
}

// Events in, conversation items out: { kind: 'me' | 'say' | 'note' | 'group', ... }.
export function threadItems() {
  const { job, events } = state;
  const items = [];
  let group = null;
  const running = []; // work blocks holding a script that has started but not yet reported what it was for
  let script = null; // the script running now, as far as it has streamed in
  const push = (item) => {
    group = null;
    items.push(item);
  };
  const step = (s) => {
    if (!group) {
      group = { kind: 'group', id: s.seq, seq: s.seq, from: s.at, steps: [], code: 0 };
      items.push(group);
    }
    if (s.verb) group.steps.push(s);
    group.to = s.at;
    group.seq = s.seq;
  };
  if (job.instructions) items.push({ kind: 'me', seq: 0, text: job.instructions });

  // The run's last ending is drawn as the result card, and the question being asked as the question card.
  const ending = events.findLast((e) => ENDINGS.includes(e.name));
  const settled = ending && ending.name === 'job.' + job.status ? ending.seq : -1;
  const asked = job.status === 'waiting' ? events.findLast((e) => e.type === 'question' || e.name === 'engine.question.asked') : null;

  for (const e of events) {
    const base = { seq: e.seq, at: time(e) };
    // A script cut short: the run stopped, or OpenAI dropped the reply it was part of.
    if (['engine.run.started', 'engine.model.retry', 'job.paused', 'job.cancelled', ...ENDINGS].includes(e.name)) {
      script = null;
      running.length = 0;
    }
    if (e.name === 'feedback.sent') push({ kind: 'round', ...base, event: e });
    else if (e.name === 'feedback.done') {
      // What the agent's work did to the spots the comments pointed at, once it has finished.
      const asked = e.changed + e.unchanged;
      const also = e.elsewhere ? ` ${plural(e.elsewhere, 'other change')} in the files.` : '';
      push({ kind: 'note', ...base, text: `Changed ${e.changed} of ${plural(asked, 'commented spot')}.${also}` });
    } else if (e.name === 'lesson.saved') push({ kind: 'learned', ...base, lesson: e.lesson, version: e.version });
    else if (e.type === 'user') push({ kind: 'me', ...base, text: e.message, files: e.files || [], id: e.message_id });
    else if (e.name === 'engine.instructions.delivered' && e.interrupted) push({ kind: 'note', ...base, text: 'Stopped the current step to read your message.' });
    else if (e.type === 'question' || e.name === 'engine.question.asked') {
      if (e !== asked) push({ kind: 'say', ...base, text: String(e.message).replace(/^Question for you:\s*/, '') });
    } else if (e.type === 'tool') {
      if (e.status !== 'running') step(toolStep(e)); // earlier-engine runs logged a start and an end per tool
    } else if (e.name === 'engine.code.started') {
      step(base);
      group.code += 1;
      running.push(group);
      script = { at: base.at, since: e.timestamp, text: '', files: [] };
    } else if (e.name === 'engine.code.writing' || e.name === 'engine.code.running') {
      // The script streaming in: what it is for, then the files it names. Shown on the live line, not as a step.
      if (script) {
        if (e.name === 'engine.code.writing' || !script.text || e.message !== 'Reading') script.text = e.message;
        if (e.files) script.files = e.files;
      }
    } else if (e.name === 'engine.code.finished') {
      const secs = script ? Math.round((base.at - script.at) / 1000) : undefined;
      step({ ...base, verb: sentence(e.message), files: e.files || [], plain: true, secs });
      const started = running.shift();
      if (started) started.code -= 1;
      script = null;
    } else if (e.name === 'engine.code.ran') {
      step({ ...base, verb: sentence(e.message), files: e.files || [], plain: true });
      const started = running.shift();
      if (started) started.code -= 1;
    } else if (e.type === 'engine') {
      if (!QUIET.has(e.name)) step({ ...base, verb: sentence(e.message), plain: true, flag: isWarning(e), problem: isWarning(e) });
    } else if (ENDINGS.includes(e.name)) {
      if (e.seq !== settled) push({ kind: e.name === 'job.failed' ? 'note' : 'say', ...base, text: e.message });
    } else if (e.name === 'job.paused') push({ kind: 'note', ...base, text: e.level === 'WARN' ? e.message : 'Paused.' });
    else if (e.name === 'job.cancelled') push({ kind: 'note', ...base, text: state.job.legacy ? e.message : 'Stopped. Everything so far is kept.' });
    else if (isWarning(e)) step({ ...base, verb: sentence(e.message), plain: true, flag: true });
  }
  items.script = script;
  return items;
}

const fresh = (seq) => (seq > state.seen ? 'fresh' : '');

function fileButton(path) {
  const stored = storedPath(path);
  const name = esc(baseName(path));
  return stored
    ? `<button class="file" data-open="${esc(stored)}" title="${esc(path)}">${name}</button>`
    : `<span class="file" title="${esc(path)}">${name}</span>`;
}

function stepHtml(s) {
  const files = s.file ? fileButton(s.file) : (s.files || []).slice(0, 2).map(fileButton).join('');
  const extra = (s.files || []).length > 2 ? `and ${s.files.length - 2} more` : '';
  const more = [s.more, extra, s.times > 1 ? `${s.times} times` : ''].filter(Boolean).join(', ');
  const took = s.secs !== undefined && !(s.times > 1) ? `<time>${duration(s.secs * 1000)}</time>` : '';
  return `<div class="step ${s.flag ? 'flag' : ''} ${s.plain ? 'plain' : ''} ${fresh(s.seq)}">
    <span>${esc(s.verb)}</span>${files}${more ? `<span class="more">${esc(more)}</span>` : ''}${took}</div>`;
}

// live: the live line, when this is the block of work going on now; it sits under the last step, as one list.
function groupHtml(g, live = '') {
  // Repeats of the same plain step ("Made a note") fold into one line.
  const steps = [];
  for (const s of g.steps) {
    const last = steps[steps.length - 1];
    if (last && !s.file && !s.files?.length && !s.more && !last.file && last.verb === s.verb && !s.flag) {
      last.times = (last.times || 1) + 1;
      last.seq = s.seq;
    } else steps.push({ ...s });
  }
  // Scripts whose purpose was not reported (older runs, or a reply still in progress).
  if (g.code > 0 && !live) steps.push({ seq: g.seq, verb: 'Ran code in the sandbox', times: g.code, plain: true });
  if (!steps.length && !live) return '';
  const names = new Set(g.steps.flatMap((s) => (s.file ? [s.file] : s.files || [])));
  const flags = g.steps.filter((s) => s.problem).length;
  const title = names.size ? `Worked through ${plural(names.size, 'file')}` : plural(Math.max(steps.length, 1), 'step');
  const open = state.toggled.has(g.id) ? !live : Boolean(live);
  return `<div class="grp ${open ? 'open' : ''} ${fresh(g.id)}">
    <button class="grp-head" data-group="${g.id}" aria-expanded="${open}">${icon('chev')}<span>${title}</span>
      ${flags ? `<span class="flagged">${plural(flags, 'problem')}</span>` : ''}<time>${duration(g.to - g.from)}</time></button>
    <div class="grp-body">${steps.map(stepHtml).join('')}${open ? live : ''}</div></div>${open ? '' : live}`;
}

// What the agent is doing this second, from the latest event.
// A running script shows what it is for in the agent's words and the file it opened, timed from when it started.
function liveLine(script) {
  const status = jobStatus();
  const last = state.events[state.events.length - 1];
  let text = 'Thinking';
  let since = last?.timestamp || state.job.updated_at || '';
  let file = '';
  if (status === 'queued') text = 'Waiting to start';
  else if (status === 'pausing') text = 'Finishing the current step';
  else if (script) {
    text = script.text || 'Running code in the sandbox';
    since = script.since;
    file = (script.files || []).slice(0, 1).map(fileButton).join('');
  } else if (['engine.scout.started', 'engine.review.started'].includes(last?.name)) text = sentence(last.message);
  return `<div class="live"><i></i><b>${esc(text)}</b>${file}<time data-since="${esc(since)}"></time></div>`;
}

function outputRows(job) {
  const shown = (job.artifacts || []).filter((a) => !['document_map.json', 'provenance.json'].includes(a.name));
  return shown
    .map(
      (a) => `<div class="filerow"><span class="tag">${esc(a.name.split('.').pop())}</span>
        <button class="name" data-open="${esc(a.path)}">${esc(a.name)}</button>
        ${a.status === 'validated' ? pill('ok', 'Checked') : a.status === 'draft' ? pill('warn', 'Draft') : ''}
        <button class="icon-btn" aria-label="Download ${esc(a.name)}" data-download="${esc(a.path)}">${icon('down')}</button></div>`,
    )
    .join('');
}

// What the person sends when they leave the choice to the agent; its instructions say what deciding involves.
const LET_AGENT_DECIDE = 'You decide.';

// The agent's question: the evidence behind it, choices to pick with one click or a number key, and a box for the
// person's own answer. The box is the run's message box, moved into the card while the question is open.
function questionCard(q) {
  const locked = state.busy || !!state.job.answer; // answered: the run picks the answer up within a second
  const choices = (q.choices || []).filter((c) => c && c.label);
  const evidence = (q.evidence || []).filter((e) => e && (e.value || e.file));
  const fact = (e) =>
    `<li><b>${esc(e.value)}</b>${e.file ? fileButton(e.file) : ''}${e.where ? `<span class="where">${esc(e.where)}</span>` : ''}</li>`;
  const facts = evidence.length ? `<ul class="q-facts">${evidence.map(fact).join('')}</ul>` : '';
  const picks = choices
    .map(
      (c, i) => `<button class="opt" data-answer="${esc(c.label)}" ${attr('disabled', locked)}><kbd>${i + 1}</kbd>
        <span class="opt-body"><span>${esc(c.label)}</span>${c.detail ? `<span class="detail">${esc(c.detail)}</span>` : ''}</span>
        ${c.suggested ? pill('live', 'Suggested') : ''}</button>`,
    )
    .join('');
  const hint = choices.length ? 'Something else' : 'Type your answer';
  const own = `<form id="composer" class="own"><kbd>${choices.length ? choices.length + 1 : '›'}</kbd>
    <textarea id="msg" rows="1" aria-label="${hint}" placeholder="${hint}" ${attr('disabled', locked)}></textarea>
    <button class="send" aria-label="Send" disabled>${icon('send')}</button></form>`;
  const keys = choices.length ? `<span class="keys">Press 1–${choices.length + 1} to choose</span>` : '';
  return `<section class="question" aria-label="Question from the agent"><h2>${esc(q.text)}</h2>${facts}
    <div class="opts" role="group" aria-label="Choices">${picks}${own}</div>
    <div class="q-foot">${keys}<button class="skip" data-answer="${esc(LET_AGENT_DECIDE)}" ${attr('disabled', locked)}>Let the agent decide</button></div></section>`;
}

function endCard(job) {
  const status = job.status;
  if (status === 'waiting' && job.question) return questionCard(job.question);
  if (status === 'failed') {
    return `<div class="card bad"><p>${esc(job.error || 'The run stopped.')}</p>
      <div class="choices">${job.legacy ? '' : btn('resume', 'Resume', 'primary', state.busy)}${btn('log', 'Technical log', 'text')}</div></div>`;
  }
  if (!inGroup('finished', status)) return '';
  const issues = job.engine_check && !job.engine_check.passed ? job.engine_check.issues || [] : [];
  const rows = outputRows(job);
  return `${job.summary ? `<p class="say">${esc(job.summary)}</p>` : ''}
    ${rows ? `<div class="card">${rows}</div>` : ''}
    ${issues.length ? `<div class="card ask"><p>Still to fix</p><ul class="todo">${issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul></div>` : ''}
    ${status === 'partial' && !job.legacy ? `<div class="choices">${btn('resume', 'Continue', 'primary', state.busy)}</div>` : ''}`;
}

// The messages the agent has not read yet: it reads them at its next step, or at once after Send now.
function waitingIds() {
  const job = state.job;
  if (!inGroup('active', job.status)) return new Set(); // a run that is not going reads them when it starts
  return new Set((job.pending_messages || []).map((m) => m?.id).filter(Boolean));
}

function meHtml(it, waiting, last) {
  const files = (it.files || []).map((f) => `<button class="chip" data-open="${esc(f.path)}" title="${esc(f.packet)}"><span>${esc(baseName(f.path))}</span></button>`);
  const queued = waiting.has(it.id);
  const bubble = `<div class="me ${queued ? 'queued' : ''} ${fresh(it.seq)}">${it.text ? esc(it.text) : ''}${files.length ? `<div class="att">${files.join('')}</div>` : ''}</div>`;
  if (!queued || !last) return bubble;
  const now = state.job.interrupt
    ? pill('live', 'Sending now')
    : `${pill('', 'Queued')}${jobStatus() === 'running' ? btn('interrupt', 'Send now', 'text small', state.busy) : ''}`;
  return `${bubble}<div class="queue-foot">${now}</div>`;
}

export function threadHtml() {
  const items = threadItems();
  const waiting = waitingIds();
  const lastWaiting = items.findLast((it) => it.kind === 'me' && waiting.has(it.id));
  const active = isActive();
  const line = active ? liveLine(items.script) : '';
  const inGroupLine = items[items.length - 1]?.kind === 'group';
  const html = items.map((it, i) => {
    if (it.kind === 'group') return groupHtml(it, i === items.length - 1 ? line : '');
    if (it.kind === 'round') return roundHtml(it.event, fresh(it.seq));
    // Undo sits on the last lesson of a version: one version holds every lesson of a round.
    if (it.kind === 'learned') return learnedHtml(it, !items.slice(i + 1).some((x) => x.kind === 'learned' && x.version === it.version), fresh(it.seq));
    if (it.kind === 'me') return meHtml(it, waiting, it === lastWaiting);
    const cls = it.kind === 'say' ? 'say' : 'say note';
    return `<p class="${cls} ${fresh(it.seq)}">${esc(it.text)}</p>`;
  });
  if (!state.events.length && !active) html.push('<p class="say note">Not started yet.</p>');
  return html.join('') + (inGroupLine ? '' : line) + endCard(state.job);
}

export function headHtml() {
  const status = jobStatus();
  const [tone, label] = STATUS[status] || ['', status];
  const playbook = playbooks().find((p) => p.id === (state.job.playbook?.id || state.job.module)); // one that still exists
  const controls = [
    status === 'ready' ? btn('start', 'Start run', 'primary', state.busy || !state.config?.configured) : '',
    inGroup('pausable', status) ? btn('pause', 'Pause', 'text', state.busy) : '',
    ['paused', 'cancelled'].includes(status) && !state.job.legacy ? btn('resume', 'Resume', 'text', state.busy) : '',
    isActive() || ['paused', 'waiting'].includes(status) ? btn('cancel', 'Stop', 'text', state.busy) : '',
    playbook ? `<button class="btn text hide-narrow" data-book="${esc(playbook.id)}">${esc(playbook.name)}</button>` : '',
    `<span class="hide-narrow">${btn('log', 'Technical log', 'text')}</span>`,
    `<span class="only-narrow">${btn('toggle-files', 'Files')}</span>`,
  ].join('');
  return `<h1>${esc(jobTitle(state.job))}</h1>${pill(tone, label)}<div class="controls">${controls}</div>`;
}

// The agent works, checks, then (if switched on) has a reviewer look. Only this run's events count.
export function stagesHtml() {
  if (!state.events.some((e) => e.type === 'engine')) return '';
  const engine = state.events.filter((e) => e.type === 'engine');
  const run = Math.max(0, ...engine.map((e) => e.run || 0));
  const current = engine.filter((e) => (e.run || 0) === run);
  const saved = current.some((e) => e.name === 'engine.outputs.saved');
  const reviewing = current.some((e) => e.name === 'engine.review.started');
  const names = ['Work', 'Check', ...(state.job.engine_team?.reviewer ? ['Review'] : [])];
  const at = jobStatus() === 'completed' ? names.length : reviewing ? 2 : saved ? 1 : 0;
  return names.map((n, i) => `<span class="${i < at ? 'done' : i === at ? 'now' : ''}">${n}</span>`).join('<span>›</span>');
}

export function composerHtml() {
  const status = jobStatus();
  if (status === 'waiting' && state.job.question) return ''; // the question card holds the message box while it is open
  const usable = !state.job.legacy && inGroup('accepts_messages', status);
  // A message to a run that has stopped, a finished one included, starts it again.
  const hint = !usable
    ? status === 'ready' ? 'Start the run to message the agent' : 'This run has ended'
    : isActive() ? 'Message the agent' : 'Ask for changes or a follow-up';
  const files = usable ? state.attachments[state.job.id] || [] : [];
  const chips = files.length
    ? `<div class="chips">${files.map((f, i) => `<span class="chip" title="${esc(f.name)}"><span>${esc(f.name)}</span><button type="button" aria-label="Remove ${esc(f.name)}" data-unattach="${i}">${icon('x')}</button></span>`).join('')}</div>`
    : '';
  // The typed text is put back by app.js after drawing, so typing never redraws the box.
  return `<form id="composer">${chips}<div class="line">
    <button type="button" class="icon-btn" data-action="attach" aria-label="Add files" title="Add files" ${attr('disabled', !usable)}>${icon('clip')}</button>
    <textarea id="msg" rows="1" aria-label="${hint}" placeholder="${hint}" ${attr('disabled', !usable)}></textarea>
    <button class="send" aria-label="Send" disabled>${icon('send')}</button></div></form>`;
}
