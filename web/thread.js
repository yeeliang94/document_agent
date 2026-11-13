// The conversation: what the person asked, what the agent said, and the work it did in between.
// Built from the job's events on every change, so a reloaded or resumed run reads the same as a live one.

import {
  attr, baseName, btn, duration, esc, icon, inGroup, isActive, isWarning, jobStatus, jobTitle, pill, plural,
  sandboxEngine, state, STATUS, storedPath,
} from './core.js'; // prettier-ignore

// How each classic-engine tool reads in plain words: with the file it worked on, and without one.
const TOOLS = {
  list_files: ['Listed the files'],
  query_inventory: ['Checked the file list'],
  read_document: ['Read', 'Read a file'],
  read_sheet: ['Read', 'Read a file'],
  inspect_workbook: ['Opened', 'Opened a workbook'],
  inspect_source: ['Looked through', 'Looked through a file'],
  inspect_regions: ['Found the tables in', 'Found the tables'],
  extract_region: ['Pulled a table from', 'Pulled out a table'],
  view_page: ['Looked at', 'Looked at a page'],
  resolve_visual: ['Read the image', 'Read an image'],
  search_cells: ['Searched', 'Searched the sheets'],
  query_dataset: ['Looked up the data'],
  transform_dataset: ['Reshaped the data'],
  write_cells: ['Wrote to', 'Wrote cells'],
  populate_template: ['Filled', 'Filled the template'],
  create_output: ['Saved', 'Saved the output'],
  validate_output: ['Checked', 'Checked the output'],
  validate_job: ['Checked the whole job'],
  update_work_plan: ['Updated its plan'],
  read_job_state: ['Reviewed its progress'],
  apply_instructions: ['Applied your instructions'],
  save_memory: ['Made a note'],
  save_interpretation: ['Made a note'],
  record_decisions: ['Recorded its decisions'],
  record_observations: ['Recorded what it found in', 'Recorded what it found'],
  save_output_requirements: ['Noted what is needed in', 'Noted what the output needs'],
  save_report: ['Wrote the summary'],
};
// Sandbox-engine events that are housekeeping, or that another item already shows.
const QUIET = new Set(['engine.question.answered', 'engine.run.finished', 'engine.instructions.delivered']);
const ENDINGS = ['job.completed', 'job.partial', 'job.failed'];

const sentence = (text) => {
  const clean = String(text || '').replace(/_/g, ' ').trim();
  return clean.charAt(0).toUpperCase() + clean.slice(1);
};
const time = (event) => new Date(event.timestamp).getTime();

function toolStep(event) {
  const args = event.arguments || {};
  const [withFile, alone] = TOOLS[event.name] || [sentence(event.message || event.name)];
  const path = args.path || args.filename || '';
  const failed = isWarning(event) || event.status === 'failed';
  const detail = [typeof args.sheet === 'string' ? args.sheet : '', event.name === 'view_page' ? `page ${args.page || 1}` : ''];
  return {
    seq: event.seq,
    at: time(event),
    verb: path || !alone ? withFile : alone,
    file: path,
    // The agent corrects itself after a step is turned down; the reason is in the technical log.
    more: failed ? 'did not go through' : detail.filter(Boolean).join(', '),
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
  const finished = new Set(events.filter((e) => e.type === 'tool' && e.status !== 'running').map((e) => e.span_id));

  for (const e of events) {
    const base = { seq: e.seq, at: time(e) };
    if (e.type === 'user') push({ kind: 'me', ...base, text: e.message });
    else if (e.type === 'question' || e.name === 'engine.question.asked') {
      if (e !== asked) push({ kind: 'say', ...base, text: String(e.message).replace(/^Question for you:\s*/, '') });
    } else if (e.type === 'tool') {
      // A tool logs once when it starts and once when it ends; keep the start only while it is still running.
      if (e.status !== 'running') step(toolStep(e));
      else if (!finished.has(e.span_id)) step(base);
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
    else if (e.name === 'job.cancelled') push({ kind: 'note', ...base, text: 'Stopped. Everything so far is kept.' });
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
  else if (last?.type === 'tool' && last.status === 'running') {
    const s = toolStep(last);
    text = s.file ? `${s.verb} ${baseName(s.file)}` : s.verb;
  } else if (last?.name === 'engine.code.started') text = 'Running code in the sandbox';
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
    const hold = sandboxEngine()
      ? `<p class="hint">The sandbox waits ${esc(state.settings?.values.question_wait_minutes ?? 10)} minutes for your answer. After that the run rests here and picks up when you reply.</p>`
      : '';
    return `<div class="card ask"><p>${esc(job.question.text)}</p>${hold}</div>`;
  }
  if (status === 'failed') {
    return `<div class="card bad"><p>${esc(job.error || 'The run stopped.')}</p>
      <div class="choices">${btn('resume', 'Resume', 'primary', state.busy)}${btn('log', 'Technical log', 'text')}</div></div>`;
  }
  if (!inGroup('finished', status)) return '';
  const issues = job.engine_check && !job.engine_check.passed ? job.engine_check.issues || [] : [];
  const rows = outputRows(job);
  return `${job.summary ? `<p class="say">${esc(job.summary)}</p>` : ''}
    ${rows ? `<div class="card">${rows}</div>` : ''}
    ${issues.length ? `<div class="card ask"><p>Still to fix</p><ul class="todo">${issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul></div>` : ''}
    ${status === 'partial' ? `<div class="choices">${btn('resume', 'Continue', 'primary', state.busy)}</div>` : ''}`;
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
    ['paused', 'cancelled'].includes(status) ? btn('resume', 'Resume', 'text', state.busy) : '',
    isActive() || ['paused', 'waiting'].includes(status) ? btn('cancel', 'Stop', 'text', state.busy) : '',
    `<span class="hide-narrow">${btn('log', 'Technical log', 'text')}</span>`,
    `<span class="only-narrow">${btn('toggle-files', 'Files')}</span>`,
  ].join('');
  return `<h1>${esc(jobTitle(state.job))}</h1>${pill(tone, label)}<div class="controls">${controls}</div>`;
}

// The sandbox engine works, checks, then (if switched on) has a reviewer look. Only this run's events count.
export function stagesHtml() {
  if (!sandboxEngine() || !state.events.some((e) => e.type === 'engine')) return '';
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
  const usable = answering || inGroup('accepts_messages', status);
  const hint = answering ? 'Type your answer' : usable ? 'Message the agent' : status === 'ready' ? 'Start the run to message the agent' : 'This run has ended';
  // The typed text is put back by app.js after drawing, so typing never redraws the box.
  return `<form id="composer">
    <textarea id="msg" rows="1" aria-label="${hint}" placeholder="${hint}" ${attr('disabled', !usable)}></textarea>
    <button class="send" aria-label="Send" disabled>${icon('send')}</button></form>`;
}
