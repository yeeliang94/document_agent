// The pages around a run: starting a new one, the reusable instructions, Settings and the technical log.

import {
  attr, btn, bytes, currentPlaybook, esc, fmtTime, isWarning, playbooks, state,
} from './core.js'; // prettier-ignore

const page = (width, body) => `<div class="page"><div class="${width}">${body}</div></div>`;
const row = (label, control) => `<div class="row"><span>${esc(label)}</span>${control}</div>`;
const playbookSelect = () =>
  `<select class="field" aria-label="Playbook" data-playbook>${playbooks()
    .map((p) => `<option value="${esc(p.id)}" ${attr('selected', p.id === currentPlaybook()?.id)}>${esc(p.name)}</option>`)
    .join('')}</select>`;

// One two-way choice, one on/off switch and one number stepper, used everywhere they appear.
const segment = (name, value, label, on) =>
  `<button type="button" class="${on ? 'on' : ''}" data-segment="${name}" data-value="${esc(value)}" aria-pressed="${on}">${esc(label)}</button>`;
const toggle = (name, label, on) =>
  `<button type="button" id="setting-${name}" class="switch ${on ? 'on' : ''}" data-toggle="${name}" role="switch" aria-checked="${on}" aria-label="${esc(label)}"></button>`;
const stepper = (name, label, value, low, high) => `
  <span class="stepper" role="group" aria-label="${esc(label)}">
    <button type="button" data-step="${name}" data-delta="-1" aria-label="Fewer" ${attr('disabled', value <= low)}>−</button>
    <b>${esc(value)}</b>
    <button type="button" data-step="${name}" data-delta="1" aria-label="More" ${attr('disabled', value >= high)}>+</button>
  </span>`;

export function startBlocked() {
  // Source files and a playbook always; then either a goal in words, or a template or format to work to.
  const goal = (state.instructions[currentPlaybook()?.id] || '').trim();
  return state.busy || !state.files.length || !currentPlaybook() || (!goal && !state.template);
}

export function setupPage() {
  const format = state.deliverable === 'format';
  const size = state.files.reduce((sum, f) => sum + f.size, 0);
  const picked = state.files.length
    ? `<em>${state.files.length === 1 ? esc(state.files[0].name) : `${state.files.length} files`} · ${bytes(size)}</em>`
    : '';
  const deliver = `<div class="seg">${segment('deliverable', 'template', 'Template', !format)}${segment('deliverable', 'format', 'Format', format)}</div>`;
  const chosen = state.template ? esc(state.template.name) : format ? 'Choose format' : 'Choose workbook';
  // A template, a format and a month are for work that has them. What was chosen stays in view, so while one is
  // chosen there is nothing to fold away and the button that folds is not offered.
  const held = Boolean(state.template) || Boolean(state.period);
  const more = state.moreOptions || held;
  return page(
    'narrow',
    `<div class="page-head"><h1>New run</h1></div>
    <div class="box"><textarea id="run-instructions" aria-label="What should the agent produce?" placeholder="What should the agent produce?">${esc(state.instructions[currentPlaybook()?.id] || '')}</textarea></div>
    <div class="rows">
      ${row('Source files', `<div class="val">${picked}${btn('folder', state.files.length ? 'Change folder' : 'Choose folder', 'quiet', false, 'clip')}${btn('files', 'Choose files or ZIP', 'text')}</div>`)}
      ${row('Playbook', playbookSelect())}
    </div>
    ${
      more
        ? `<div class="rows" id="more-options">
      ${row('Deliver as', `<div class="val">${deliver}${btn('template', chosen)}${state.template ? btn('no-template', 'Remove', 'text') : ''}</div>`)}
      ${row('Month', `<input id="period" type="month" aria-label="Month" value="${esc(state.period)}">`)}
    </div>`
        : ''
    }
    <div class="end">
      <button class="btn text" data-action="more-options" aria-expanded="${more}" aria-controls="more-options" ${held ? 'style="display:none"' : ''}>${more ? 'Fewer options' : 'More options'}</button>
      <button class="btn text" data-book="${esc(currentPlaybook()?.id || '')}">Open playbook</button>
      ${btn('start', state.busy ? 'Please wait' : state.config?.configured === false ? 'Upload files' : 'Start run', 'primary', startBlocked())}
    </div>`,
  );
}

export function settingsPage() {
  const head = `<div class="page-head"><h1>Settings</h1>${btn('save-settings', 'Save', 'primary', state.busy || !state.settingsDraft)}</div>`;
  if (state.settingsError) return page('narrow', `${head}<p class="problem">${esc(state.settingsError)}</p>`);
  if (!state.settingsDraft) return page('narrow', `${head}<p class="empty">Loading</p>`);
  const v = state.settingsDraft;
  const options = state.settings.options;
  const select = (name, labels) =>
    `<select id="setting-${name}" data-setting="${name}" aria-label="${esc(options[name].label)}">${options[name].choices
      .map((c) => `<option value="${esc(c)}" ${attr('selected', v[name] === c)}>${esc(labels?.[c] ?? c)}</option>`)
      .join('')}</select>`;
  const number = (name) =>
    `<input id="setting-${name}" type="number" data-setting="${name}" min="${options[name].low}" max="${options[name].high}" value="${esc(v[name])}" aria-label="${esc(options[name].label)}">`;
  const panel = (title, rows) => `<section class="rows"><h2>${esc(title)}</h2>${rows}</section>`;
  const connection = state.settings.connection;
  const agent =
    row('Parallel scouts', stepper('max_scouts', 'Parallel scouts', v.max_scouts, options.max_scouts.low, options.max_scouts.high)) +
    row('Independent reviewer', toggle('reviewer', 'Independent reviewer', v.reviewer)) +
    row('Show the agent its files before it finishes', toggle('look_before_finish', 'Show the agent its files before it finishes', v.look_before_finish)) +
    row('Proof level', select('proof', { row: 'Sources for every row', none: 'None' })) +
    row('Reasoning effort', select('effort', { low: 'Low', medium: 'Medium', high: 'High' })) +
    row('Re-check past corrections', select('replay_tests', { never: 'Never', large: 'After a rewrite or tidy', every: 'After every change' }));
  const limits = panel(
    'Limits',
    row('Turn limit per run', number('max_turns')) +
      row('Steps added by "More steps"', number('more_steps')) +
      row('Time limit per run (minutes)', number('time_limit_minutes')) +
      row('Token limit per run (thousands)', number('token_limit_thousands')) +
      row('Token limit per day, all runs (thousands, 0 for none)', number('daily_token_limit_thousands')) +
      row('Used today (thousands)', `<span class="value">${Math.round((state.settings.tokens_today || 0) / 1000).toLocaleString()}</span>`) +
      row('Hold sandbox for an answer (minutes)', number('question_wait_minutes')) +
      row('Condense history at (thousands of tokens, 0 for never)', number('condense_at_thousands')) +
      row('Versions kept per run', number('versions_kept')) +
      row('Sandbox memory', select('sandbox_memory', { '1g': '1 GB', '4g': '4 GB', '16g': '16 GB' })),
  );
  const link =
    row('Endpoint', `<span class="value">${esc(connection.base_url)}</span>`) +
    row('Model', `<span class="value">${esc(connection.model)}</span>`) +
    row('LiteLLM provider header', `<input id="setting-litellm_provider" data-setting="litellm_provider" value="${esc(v.litellm_provider)}" placeholder="None" aria-label="LiteLLM provider header">`) +
    row('Name the model when creating a sandbox', toggle('model_on_create', 'Name the model when creating a sandbox', v.model_on_create)) +
    row('Check the connection', btn('connection', 'Test', 'quiet', state.busy)) +
    (connection.warning ? `<p class="problem row">${esc(connection.warning)}</p>` : '');
  return page('narrow', head + panel('Agent', agent) + limits + panel('Connection', link));
}

const FILTERS = [
  ['all', 'All'],
  ['engine', 'Agent'],
  ['user', 'You'],
  ['warnings', 'Warnings'],
  ['comments', 'Comments'],
];
const isComment = (event) => /(^|\.)feedback\./.test(event.name || '');

// Lower-cased JSON of each event, built once, so searching thousands of events stays quick.
const searchText = new WeakMap();
function matches(event) {
  const shown = { all: () => true, warnings: isWarning, comments: isComment }[state.filter] || ((e) => e.type === state.filter);
  if (!shown(event)) return false;
  if (!state.search) return true;
  if (!searchText.has(event)) searchText.set(event, JSON.stringify(event).toLowerCase());
  return searchText.get(event).includes(state.search.toLowerCase());
}

function facts(job) {
  if (!job) return '';
  const usage = job.usage || {};
  const team = job.engine_team;
  const runs = job.runs || [];
  const total = (key) => runs.reduce((sum, r) => sum + (r[key] || 0), 0);
  const duration = (s) => (s >= 60 ? `${Math.floor(s / 60)} min ${Math.round(s % 60)} s` : `${Math.round(s)} s`);
  const rows = [
    ['Run', job.id.slice(0, 8)],
    ['Model', state.config?.model],
    team ? ['Team', `Lead${team.max_scouts ? ` + ${team.max_scouts} scouts` : ''}${team.reviewer ? ' + reviewer' : ''}`] : null,
    // Every run of this job counts: the first one and each one your comments or answers started.
    runs.length ? ['Time', duration(total('seconds'))] : null,
    runs.length > 1 ? ['Runs', runs.length] : null,
    ['Model calls', runs.length ? total('requests') : (job.request_count ?? usage.requests)],
    ['Input tokens', (runs.length ? total('input_tokens') : usage.input_tokens)?.toLocaleString()],
    ['Output tokens', (runs.length ? total('output_tokens') : usage.output_tokens)?.toLocaleString()],
    job.engine_questions ? ['Questions asked', job.engine_questions.length] : null,
  ].filter((r) => r && r[1] != null && r[1] !== '');
  return `<div class="facts">${rows.map(([a, b]) => `<div><span>${a}</span>${esc(b)}</div>`).join('')}</div>`;
}

export function logPage() {
  const list = state.events.filter(matches);
  const rows = list
    .map(
      (e) => `
        <tr tabindex="0" data-event="${e.seq}">
          <td class="when">${fmtTime(e.timestamp)}</td>
          <td class="what">${esc(e.name)}<div>${esc(String(e.message ?? '').slice(0, 300))}</div></td>
          <td class="small">${isWarning(e) ? '<span class="pill warn">Warning</span>' : ''}</td>
          <td class="small num">${e.duration_ms == null ? '' : esc(Math.round(e.duration_ms).toLocaleString()) + ' ms'}</td>
        </tr>`,
    )
    .join('');
  return page(
    'wide',
    `<div class="page-head"><h1>Technical log</h1>
      <div class="controls">${state.job ? btn('run', 'Back to run', 'quiet', false, 'back') : ''}${btn('export', 'Export', 'quiet', !state.job, 'down')}</div></div>
    ${facts(state.job)}
    <div class="toolbar">
      <div class="filter">${FILTERS.map(([id, label]) => `<button data-filter="${id}" class="${state.filter === id ? 'on' : ''}">${label}</button>`).join('')}</div>
      <input type="search" id="log-search" aria-label="Search the log" placeholder="Search" value="${esc(state.search)}">
    </div>
    ${
      list.length
        ? `<table class="table"><thead><tr><th>Time</th><th>Event</th><th></th><th class="num">Took</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<p class="empty">${state.job ? 'Nothing matches.' : 'Open a run to see its log.'}</p>`
    }`,
  );
}

