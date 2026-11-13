// The conversation: what the person asked, what the agent said, and the work it did in between.
// Built from the job's events on every change, so a reloaded or resumed run reads the same as a live one.

import {
  attr, baseName, btn, duration, esc, icon, inGroup, isActive, isWarning, jobStatus, jobTitle, pill, plural,
  state, STATUS, storedPath,
} from './core.js'; // prettier-ignore

// Engine events that are housekeeping, or that another item already shows.
const QUIET = new Set(['engine.question.answered', 'engine.run.finished', 'engine.instructions.delivered']);
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
    if (e.type === 'user') push({ kind: 'me', ...base, text: e.message });
    else if (e.type === 'question' || e.name === 'engine.question.asked') {
      if (e !== asked) push({ kind: 'say', ...base, text: String(e.message).replace(/^Question for you:\s*/, '') });
    } else if (e.type === 'tool') {
      if (e.status !== 'running') step(toolStep(e)); // earlier-engine runs logged a start and an end per tool
    } else if (e.name === 'engine.code.started') {
      step(base);
      group.code += 1;
      running.push(group);
    } else if (e.name === 'engine.code.ran') {
      step({ ...base, verb: sentence(e.message), files: e.files || [], plain: true });
      const started = running.shift();
      if (started) started.code -= 1;
    } else if (e.type === 'engine') {
      if (!QUIET.has(e.name)) step({ ...base, verb: sentence(e.message), plain: true, flag: isWarning(e), problem: isWarning(e) });
    } else if (ENDINGS.includes(e.name)) {
      if (e.seq !== settled) push({ kind: e.name === 'job.failed' ? 'note' : 'say', ...base, text: e.message });
    } else if (e.name === 'job.paused') push({ kind: 'note', ...base, text: 'Paused.' });
    else if (e.name === 'job.cancelled') push({ kind: 'note', ...base, text: state.job.legacy ? e.message : 'Stopped. Everything so far is kept.' });
    else if (isWarning(e)) step({ ...base, verb: sentence(e.message), plain: true, flag: true });
  }
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
  return `<div class="step ${s.flag ? 'flag' : ''} ${s.plain ? 'plain' : ''} ${fresh(s.seq)}">
    <span>${esc(s.verb)}</span>${files}${more ? `<span class="more">${esc(more)}</span>` : ''}</div>`;
}

function groupHtml(g, live) {
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
  const open = state.toggled.has(g.id) ? !live : live;
  return `<div class="grp ${open ? 'open' : ''} ${fresh(g.id)}">
    <button class="grp-head" data-group="${g.id}" aria-expanded="${open}">${icon('chev')}<span>${title}</span>
      ${flags ? `<span class="flagged">${plural(flags, 'problem')}</span>` : ''}<time>${duration(g.to - g.from)}</time></button>
    <div class="grp-body">${steps.map(stepHtml).join('')}</div></div>`;
}

// What the agent is doing this second, from the latest event.
function liveLine() {
  const status = jobStatus();
  const last = state.events[state.events.length - 1];
  let text = 'Thinking';
  if (status === 'queued') text = 'Waiting to start';
  else if (status === 'pausing') text = 'Finishing the current step';
  else if (last?.name === 'engine.code.started') text = 'Running code in the sandbox';
  else if (['engine.scout.started', 'engine.review.started'].includes(last?.name)) text = sentence(last.message);
  return `<div class="live"><i></i><b>${esc(text)}</b><time data-since="${esc(last?.timestamp || state.job.updated_at || '')}"></time></div>`;
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

function endCard(job) {
  const status = job.status;
  if (status === 'waiting' && job.question) {
    return `<div class="card ask"><p>${esc(job.question.text)}</p>
      <p class="hint">The sandbox waits ${esc(state.settings?.values.question_wait_minutes ?? 10)} minutes for your answer. After that the run rests here and picks up when you reply.</p></div>`;
  }
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

export function threadHtml() {
  const items = threadItems();
  const active = isActive();
  const html = items.map((it, i) => {
    if (it.kind === 'group') return groupHtml(it, active && i === items.length - 1);
    const cls = it.kind === 'say' ? 'say' : it.kind === 'me' ? 'me' : 'say note';
    return `<${it.kind === 'me' ? 'div' : 'p'} class="${cls} ${fresh(it.seq)}">${esc(it.text)}</${it.kind === 'me' ? 'div' : 'p'}>`;
  });
  if (!state.events.length && !active) html.push('<p class="say note">Not started yet.</p>');
  return html.join('') + (active ? liveLine() : '') + endCard(state.job);
}

export function headHtml() {
  const status = jobStatus();
  const [tone, label] = STATUS[status] || ['', status];
  const controls = [
    status === 'ready' ? btn('start', 'Start run', 'primary', state.busy || !state.config?.configured) : '',
    inGroup('pausable', status) ? btn('pause', 'Pause', 'text', state.busy) : '',
    ['paused', 'cancelled'].includes(status) && !state.job.legacy ? btn('resume', 'Resume', 'text', state.busy) : '',
    isActive() || ['paused', 'waiting'].includes(status) ? btn('cancel', 'Stop', 'text', state.busy) : '',
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
  const answering = status === 'waiting' && !!state.job.question;
  const usable = !state.job.legacy && (answering || inGroup('accepts_messages', status));
  const hint = answering ? 'Type your answer' : usable ? 'Message the agent' : status === 'ready' ? 'Start the run to message the agent' : 'This run has ended';
  // The typed text is put back by app.js after drawing, so typing never redraws the box.
  return `<form id="composer">
    <textarea id="msg" rows="1" aria-label="${hint}" placeholder="${hint}" ${attr('disabled', !usable)}></textarea>
    <button class="send" aria-label="Send" disabled>${icon('send')}</button></form>`;
}
