// The pages around a run: starting a new one, the reusable instructions, Settings and the technical log.

import {
  attr, btn, bytes, currentPlaybook, esc, fmtTime, icon, isWarning, playbooks, state,
} from './core.js'; // prettier-ignore
import { notifyNote } from './alerts.js';
import { micButton, voiceNote } from './voice.js';
import { clientRow } from './clients.js';

const page = (width, body) => `<div class="page"><div class="${width}">${body}</div></div>`;
const row = (label, control) => `<div class="row"><span>${esc(label)}</span>${control}</div>`;
// The full name shows on hover; the box keeps the Client box's width (.pick), cutting a long name at its end.
const playbookSelect = () =>
  `<select class="field pick" aria-label="Playbook" title="${esc(currentPlaybook()?.name || '')}" data-playbook>${playbooks()
    .map((p) => `<option value="${esc(p.id)}" ${attr('selected', p.id === currentPlaybook()?.id)}>${esc(p.name)}</option>`)
    .join('')}</select>`;

// One two-way choice, one on/off switch and one number stepper, used everywhere they appear.
const segment = (name, value, label, on) =>
  `<button type="button" class="${on ? 'on' : ''}" data-segment="${name}" data-value="${esc(value)}" aria-pressed="${on}">${esc(label)}</button>`;
const toggle = (name, label, on, described = '') =>
  `<button type="button" id="setting-${name}" class="switch ${on ? 'on' : ''}" data-toggle="${name}" role="switch" aria-checked="${on}" aria-label="${esc(label)}" ${described ? `aria-describedby="${described}"` : ''}></button>`;
const stepper = (name, label, value, low, high) => `
  <span class="stepper" role="group" aria-label="${esc(label)}">
    <button type="button" data-step="${name}" data-delta="-1" aria-label="Fewer" ${attr('disabled', value <= low)}>−</button>
    <b>${esc(value)}</b>
    <button type="button" data-step="${name}" data-delta="1" aria-label="More" ${attr('disabled', value >= high)}>+</button>
  </span>`;

export function startBlocked() {
  // Source files and a playbook always; then either a goal in words, or a template or format to work to.
  return state.busy || Boolean(startWhy());
}

// Why Start run is greyed out, in a line beside it; '' once it can be pressed.
export function startWhy() {
  const goal = (state.instructions[currentPlaybook()?.id] || '').trim();
  if (!state.files.length) return 'Choose source files to start.';
  if (!currentPlaybook()) return 'Make a playbook to start.';
  if (!goal && !state.template) return 'Say what to produce, or choose a template.';
  return '';
}

export function setupPage() {
  const format = state.deliverable === 'format';
  const size = state.files.reduce((sum, f) => sum + f.size, 0);
  const picked = state.files.length
    ? `<em>${state.files.length === 1 ? esc(state.files[0].name) : `${state.files.length} files`}, ${bytes(size)}</em>`
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
    <div class="box"><textarea id="run-instructions" aria-label="What should the agent produce?" placeholder="What should the agent produce?">${esc(state.instructions[currentPlaybook()?.id] || '')}</textarea>${micButton('run-instructions')}</div>${voiceNote('run-instructions')}
    <div class="rows">
      ${row('Source files', `<div class="val">${picked}${btn('folder', state.files.length ? 'Change folder' : 'Choose folder', 'quiet', false, 'clip')}${btn('files', 'Choose files or ZIP', 'text')}</div>`)}
      ${row('Playbook', playbookSelect())}
      ${clientRow(row)}
    </div>
    ${
      more
        ? `<div class="rows" id="more-options">
      ${row('Deliver as', `<div class="val">${deliver}${btn('template', chosen)}${state.template ? btn('no-template', 'Remove', 'text') : ''}</div>`)}
      ${row('Month', `<input id="period" type="month" aria-label="Month" value="${esc(state.period)}">`)}
    </div>`
        : ''
    }
    <div class="end start-row">
      <span class="hint" id="start-why">${esc(startWhy())}</span>
      <button class="btn text" data-action="more-options" aria-expanded="${more}" aria-controls="more-options" ${held ? 'style="display:none"' : ''}>${more ? 'Fewer options' : 'More options'}</button>
      <button class="btn text" data-book="${esc(currentPlaybook()?.id || '')}">Open playbook</button>
      <button class="btn primary" data-action="start" aria-describedby="start-why" ${attr('disabled', startBlocked())}>${esc(state.busy ? 'Please wait' : state.config?.configured === false ? 'Upload files' : 'Start run')}</button>
    </div>`,
  );
}

// --- Settings ----------------------------------------------------------------

// Numbers typed on the Settings page: the unit shown beside each, and how many stored units make one shown unit
// (token limits are kept in thousands and shown in millions).
const UNITS = {
  max_turns: ['steps'],
  more_steps: ['steps'],
  time_limit_minutes: ['minutes'],
  token_limit_thousands: ['million tokens', 1000],
  question_wait_minutes: ['minutes'],
  condense_at_thousands: ['thousand tokens'],
  versions_kept: ['versions'],
  daily_token_limit_thousands: ['million tokens', 1000],
  voice_max_minutes: ['minutes'],
  voice_settle_seconds: ['seconds'],
  // Prices are kept in ten-thousandths of a dollar, so a price can have four decimal places.
  price_input_per_million: ['$', 10000],
  price_cached_per_million: ['$', 10000],
  price_output_per_million: ['$', 10000],
};
const scaleOf = (name) => UNITS[name]?.[1] || 1;
const shownNumber = (n) => n.toLocaleString('en', { maximumFractionDigits: 4 });

// A typed number, in the units the server keeps; NaN when it is not a number.
function storedValue(name, value) {
  if (!UNITS[name] || typeof value !== 'string') return value;
  const text = value.replace(/,/g, '').trim();
  const n = text === '' ? NaN : Number(text);
  return Number.isFinite(n) ? Math.round(n * scaleOf(name) * 1e6) / 1e6 : NaN;
}

// What is wrong with a typed number, as a sentence for beside the field, or ''.
export function settingProblem(name, value) {
  const option = state.settings?.options?.[name];
  if (!option || !UNITS[name] || typeof value !== 'string') return '';
  const scale = scaleOf(name);
  const range = `from ${shownNumber(option.low / scale)} to ${shownNumber(option.high / scale)}`;
  const n = storedValue(name, value);
  if (Number.isNaN(n)) return `Enter a number ${range}.`;
  if (!Number.isInteger(n)) return scale === 1 ? `Choose a whole number ${range}.` : `Use at most ${Math.log10(scale)} decimal places.`;
  if (n < option.low || n > option.high) return `Choose a number ${range}.`;
  return '';
}

// The draft as the server takes it.
export const settingsValues = () =>
  Object.fromEntries(Object.entries(state.settingsDraft || {}).map(([name, value]) => [name, storedValue(name, value)]));
export const settingsChanged = () =>
  Object.entries(settingsValues()).some(([name, value]) => !Object.is(value, state.settings.values[name]));

export function settingsPage() {
  const changed = !!state.settingsDraft && settingsChanged();
  const head = `<div class="page-head"><h1>Settings</h1>${btn('save-settings', 'Save', 'primary', state.busy || !changed)}</div>`;
  if (state.settingsError) return page('narrow', `${head}<p class="problem">${esc(state.settingsError)}</p>`);
  if (!state.settingsDraft) return page('narrow', `${head}<p class="empty">Loading</p>`);
  const v = state.settingsDraft;
  const options = state.settings.options;
  const errors = state.settingsErrors || {};
  // A row whose value was refused shows why, beside the field.
  const field = (name, label, control, note = '') => {
    const error = errors[name];
    return `<div class="row keep ${error ? 'err' : ''}"><span>${esc(label)}</span>${control}${error ? `<p class="problem" id="setting-${name}-error">${esc(error)}</p>` : ''}${note}</div>`;
  };
  const invalid = (name) => (errors[name] ? `aria-invalid="true" aria-describedby="setting-${name}-error"` : '');
  const number = (name, label, note = '') => {
    const [unit] = UNITS[name]; // a dollar sign goes before the price, other units after the number
    const value = typeof v[name] === 'string' ? v[name] : shownNumber(v[name] / scaleOf(name)).replace(/,/g, '');
    return field(name, label, `<label class="unit ${errors[name] ? 'bad' : ''}">${unit === '$' ? '<span>$</span>' : ''}<input id="setting-${name}" data-setting="${name}" inputmode="decimal" autocomplete="off" value="${esc(value)}" aria-label="${esc(label)}" ${invalid(name)}>${unit === '$' ? '' : `<span>${esc(unit)}</span>`}</label>`, note);
  };
  const choice = (name, label, labels) =>
    field(name, label, `<div class="seg" role="group" aria-label="${esc(label)}">${options[name].choices.map((c) => segment(name, c, labels[c] ?? c, v[name] === c)).join('')}</div>`);
  const steps = (name, label) => field(name, label, stepper(name, label, v[name], options[name].low, options[name].high));
  // A two-way choice reads as on or off: its first choice is on.
  const onOff = (name, label, note = '') => {
    const choices = options[name].choices;
    const on = choices.length ? v[name] === choices[0] : v[name];
    return field(name, label, toggle(name, label, on, note ? `setting-${name}-note` : ''), note);
  };
  const text = (name, label, placeholder = '') =>
    field(name, label, `<label class="unit ${errors[name] ? 'bad' : ''}"><input id="setting-${name}" data-setting="${name}" value="${esc(v[name])}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false" aria-label="${esc(label)}" ${invalid(name)}></label>`);
  const panel = (title, rows) => `<section class="rows"><h2>${esc(title)}</h2>${rows}</section>`;
  const millions = (thousands) => shownNumber(Math.round(thousands / 100) / 10);
  const daily = state.settings.values.daily_token_limit_thousands;
  const today = millions((state.settings.tokens_today || 0) / 1000);
  const connection = state.settings.connection;
  return page(
    'narrow',
    head +
      panel(
        'Agent',
        steps('max_scouts', 'Helper agents for large folders') +
          onOff('reviewer', 'Second agent reviews the result') +
          onOff('proof', 'Show where each row came from') +
          onOff('look_before_finish', 'Agent checks its files before finishing') +
          choice('effort', 'Thinking depth', { low: 'Low', medium: 'Medium', high: 'High' }) +
          choice('thinking_summaries', "Show the agent's thinking", { off: 'Off', short: 'Short', detailed: 'Detailed' }) +
          steps('max_questions', 'Questions per run') +
          steps('runs_at_once', 'Runs at once') +
          choice('replay_tests', 'Test playbook changes on past corrections', { never: 'Never', large: 'Big changes', every: 'Every change' }),
      ) +
      panel(
        'Limits per run',
        number('max_turns', 'Steps') +
          number('more_steps', 'Steps added by More steps') +
          number('time_limit_minutes', 'Time') +
          number('token_limit_thousands', 'AI usage') +
          number('question_wait_minutes', 'Wait for your answer') +
          number('condense_at_thousands', "Summarise the agent's earlier work at (0 for never)") +
          number('versions_kept', 'Output versions kept') +
          choice('sandbox_memory', "Agent's computer memory", { '1g': '1 GB', '4g': '4 GB', '16g': '16 GB' }),
      ) +
      panel(
        'Limits per day',
        number('daily_token_limit_thousands', 'AI usage (0 for no limit)') +
          field('', 'Used today', `<span class="value">${daily ? `${today} of ${millions(daily)} million` : `${today} million`}</span>`),
      ) +
      panel(
        'Price per million tokens (0 hides costs)',
        number('price_input_per_million', 'Input tokens') +
          number('price_cached_per_million', 'Cached input tokens (0: same as input)') +
          number('price_output_per_million', 'Output tokens'),
      ) +
      panel(
        'Notifications',
        onOff('notify', 'Tell me when a run asks, finishes or stops', notifyNote(v.notify)),
      ) +
      panel(
        'Voice',
        // Live words are hidden unless the service offers Realtime transcription; off, words appear after you stop.
        onOff('voice_live', 'Show words while you speak') +
          (v.voice_live
            ? choice('voice_mode', 'Words appear', { live: 'As you speak', after: 'After you stop' }) + text('voice_live_model', 'Live voice model') +
              (v.voice_mode === 'live' ? number('voice_settle_seconds', 'Wait for the last words after you stop') : '')
            : '') +
          text('voice_file_model', 'Voice model for recordings') +
          number('voice_max_minutes', 'Longest recording'),
      ) +
      panel(
        'Connection',
        field('', 'Model', `<span class="value">${esc(connection.model)}</span>`) +
          field('', 'Service address', `<span class="value">${esc(connection.base_url)}</span>`) +
          text('litellm_provider', 'Provider name for the gateway', 'None') +
          onOff('model_on_create', 'Send the model name when a workspace starts') +
          field('', 'Check the connection', btn('connection', 'Test', 'quiet small', state.busy)) +
          (connection.warning ? `<p class="problem row">${esc(connection.warning)}</p>` : ''),
      ),
  );
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
    team ? ['Agents', `Main${team.max_scouts ? ` + ${team.max_scouts} ${team.max_scouts === 1 ? 'helper' : 'helpers'}` : ''}${team.reviewer ? ' + reviewer' : ''}`] : null,
    // Every round of this run counts: the first one and each one your comments or answers started.
    runs.length ? ['Time', duration(total('seconds'))] : null,
    runs.length > 1 ? ['Rounds', runs.length] : null,
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
      <div class="controls">${state.job ? `<button class="btn quiet" data-back="run">${icon('back')}Back to run</button>` : ''}${btn('export', 'Export', 'quiet', !state.job, 'down')}</div></div>
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

