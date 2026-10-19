const $ = (selector) => document.querySelector(selector);
const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
const attr = (name, on) => (on ? name : '');

const icons = {
  folder: 'M3 6h6l2 2h10v12H3z',
  file: 'M14 2H5v20h14V7z M14 2v6h5 M8 12h8 M8 16h6',
  spark: 'M12 3l2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z',
  clock: 'M12 8v5l3 2 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  terminal: 'M4 7l5 5-5 5 M13 17h7',
  sliders: 'M4 7h16 M4 17h16 M8 4v6 M16 14v6',
  check: 'M5 12l4 4L19 6',
  up: 'M12 19V5 M7 10l5-5 5 5',
  down: 'M12 3v13 M7 11l5 5 5-5 M4 20h16',
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icons[name] || icons.file}"/></svg>`;

const monthParts = new Intl.DateTimeFormat('en', { year: 'numeric', month: '2-digit' }).formatToParts(new Date());
const currentPeriod =
  monthParts.find((p) => p.type === 'year').value + '-' + monthParts.find((p) => p.type === 'month').value;

const PAGES = [
  ['workspace', 'spark', 'Workspace'],
  ['instructions', 'sliders', 'Instructions'],
  ['history', 'clock', 'History'],
  ['diagnostics', 'terminal', 'Diagnostics'],
];
const PAGE_TITLES = { instructions: 'Instructions', history: 'Run history', diagnostics: 'Diagnostics' };
const FILTERS = [
  ['all', 'All'],
  ['model', 'Model'],
  ['tool', 'Tools'],
  ['warnings', 'Warnings'],
];

const state = {
  drafts: {},
  view: 'workspace',
  module: 'payroll',
  config: null,
  job: null,
  jobs: [],
  events: [],
  files: [],
  template: null,
  period: currentPeriod,
  instructions: {},
  contexts: {},
  filter: 'all',
  search: '',
  busy: false,
  polling: false,
  loading: false,
  connectionError: '',
  notice: '',
  token: sessionStorage.getItem('di-token') || '',
};

// --- Derived state ---------------------------------------------------------

const modules = () => state.config?.modules || [];
const currentModule = () => modules().find((m) => m.id === state.module) || modules()[0];
const jobStatus = () => state.job?.status || 'ready';
// Status groups come from the server (/api/config) so the buttons always match what the API allows.
const inGroup = (group, status = jobStatus()) => !!state.config?.statuses?.[group]?.includes(status);
const isActive = () => inGroup('active');
const isWarning = (event) => ['warn', 'error'].includes(String(event.level).toLowerCase());
const runInstructions = () => state.job?.instructions || state.instructions[state.module] || '';

const fmtTime = (t) =>
  t ? new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
const bytes = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.ceil((n || 0) / 1024)} KB`);
const btn = (action, label, kind = '', disabled = false) =>
  `<button class="btn ${kind}" data-action="${action}" ${attr('disabled', disabled)}>${esc(label)}</button>`;

// --- Network ---------------------------------------------------------------

function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.add('visible');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 6000);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  let res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    const token = window.prompt('Application access token');
    if (token) {
      state.token = token;
      sessionStorage.setItem('di-token', token);
      headers.set('Authorization', `Bearer ${token}`);
      res = await fetch(path, { ...options, headers });
    }
  }
  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch {}
    throw new Error(typeof detail === 'string' ? detail : `Request failed (${res.status})`);
  }
  return res.status === 204 ? null : res.json();
}

async function download(path, name) {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, { headers });
  if (!res.ok) throw new Error(`Download failed (${res.status})`);
  const url = URL.createObjectURL(await res.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

// --- Views -----------------------------------------------------------------

function statusHtml(status) {
  const tone = ['waiting', 'failed'].includes(status) ? 'attention' : status === 'running' ? 'running' : '';
  return `<span class="status ${tone}"><i class="dot"></i>${esc(status)}</span>`;
}

function nav() {
  const options = modules()
    .map(
      (m) => `
        <option value="${esc(m.id)}" ${attr('selected', m.id === state.module)}>
          ${esc(m.label)}${m.id === 'ap' ? ' · preview' : ''}
        </option>`,
    )
    .join('');
  const select = `<select class="module-select" aria-label="Module" data-module>${options}</select>`;
  const pageButtons = (mobile) =>
    PAGES.map(
      ([id, iconName, label]) => `
        <button class="${state.view === id ? 'active' : ''}" data-view="${id}" ${mobile ? `aria-label="${label}"` : ''}>
          ${icon(iconName)}${label}
        </button>`,
    ).join('');
  return `
    <aside class="sidebar">
      <div class="brand">
        <span class="brandmark"><i></i><i></i><i></i><i></i></span>
        <div>Document<small>Intelligence</small></div>
      </div>
      <div class="module-block"><span class="section-label">Module</span>${select}</div>
      <nav class="nav" aria-label="Main">${pageButtons(false)}</nav>
      <div class="user">
        <span class="avatar">DI</span>
        <div>Shared agent runtime<small>${esc(state.config?.model || 'Connecting…')}</small></div>
      </div>
    </aside>
    <div class="mobile-navigation">${select}${pageButtons(true)}</div>`;
}

function startButton() {
  const label = state.busy ? 'Please wait…' : state.job || state.config?.configured ? 'Run agent' : 'Upload files';
  const notReady = state.job
    ? jobStatus() !== 'ready' || !state.config?.configured
    : !state.files.length || !state.template || !state.period;
  return btn('start', label, 'primary', state.busy || state.module === 'ap' || notReady);
}

function inputs() {
  const locked = !!state.job;
  const manifest = state.job?.files || [];
  const files = locked
    ? manifest.filter((f) => f.role !== 'template')
    : state.files.map((f) => ({ path: f.webkitRelativePath || f.name, size: f.size }));
  const templateName = state.template?.name || manifest.find((f) => f.role === 'template')?.path || 'Choose workbook';
  const fileTree = files.length
    ? `<div class="filetree">${files
        .map(
          (f) => `
            <div class="tree-row" title="${esc(f.path)}">
              ${icon('file')}<span>${esc(f.path)}</span><span class="number">${bytes(f.size)}</span>
            </div>`,
        )
        .join('')}</div>`
    : '';
  return `
    <section class="panel">
      <div class="panel-head">
        <h2>${icon('folder')} Inputs</h2>
        ${locked ? btn('new', 'New run', 'small', state.busy) : ''}
      </div>
      <div class="panel-body">
        <div class="field">
          <label for="period">Payroll period</label>
          <input id="period" type="month" value="${esc(state.job?.period || state.period)}" ${attr('disabled', locked)}>
        </div>
        <div class="field">
          <label>Source files <small>${files.length}</small></label>
          <button class="dropzone" data-action="folder" ${attr('disabled', locked)}>
            ${icon('folder')}<strong>${state.files.length ? 'Change folder' : 'Choose folder'}</strong>
          </button>
          <div class="file-options">${btn('files', 'Choose files / ZIP', 'small ghost', locked)}</div>
          ${fileTree}
        </div>
        <div class="field">
          <label>Output template</label>
          <button class="template-file" data-action="template" ${attr('disabled', locked)}>
            <span class="file-icon">XLSX</span><strong>${esc(templateName)}</strong>
          </button>
        </div>
        <div class="field">
          <label for="run-instructions">Run instructions</label>
          <textarea id="run-instructions" ${attr('disabled', locked)}>${esc(runInstructions())}</textarea>
        </div>
        <button class="context-link" data-view="instructions">${icon('sliders')} Module context</button>
      </div>
      <div class="inputs-footer">${startButton()}</div>
    </section>`;
}

function eventHtml(event) {
  return `
    <div class="event ${isWarning(event) ? 'warn' : ''}">
      <div class="event-icon">${icon(event.type === 'model' ? 'spark' : 'check')}</div>
      <div>
        <div class="event-heading">
          <button class="event-title" data-event="${event.seq}">${esc(event.message || event.name)}</button>
          <time>${fmtTime(event.timestamp)}</time>
        </div>
      </div>
    </div>`;
}

// Hide a tool's "started" event once its result has arrived.
function activityEvents() {
  const finished = new Set(
    state.events.filter((e) => e.type === 'tool' && e.status !== 'running').map((e) => e.span_id),
  );
  return state.events.filter((e) => !(e.type === 'tool' && e.status === 'running' && finished.has(e.span_id)));
}

function activityControls(status) {
  return [
    inGroup('pausable', status) ? btn('pause', 'Pause', 'small', state.busy) : '',
    inGroup('startable', status) && status !== 'ready'
      ? btn('resume', status === 'partial' ? 'Continue' : 'Resume', 'small', state.busy)
      : '',
    isActive() || ['paused', 'waiting'].includes(status) ? btn('cancel', 'Cancel', 'small', state.busy) : '',
  ].join('');
}

function questionForm(question) {
  return `
    <form id="answer-form" class="question-box">
      <strong>${esc(question.text)}</strong>
      <div class="field">
        <label for="answer" class="sr-only">Your answer</label>
        <textarea id="answer" required>${esc(state.drafts[state.job.id + ':answer'] || '')}</textarea>
      </div>
      <button class="btn primary small" type="submit">Send answer</button>
    </form>`;
}

function messageForm(status) {
  const disabled = attr('disabled', !inGroup('accepts_messages', status));
  return `
    <div class="activity-footer">
      <form class="steer" id="message-form">
        <input id="message" value="${esc(state.drafts[state.job.id + ':message'] || '')}" aria-label="Message the agent"
          placeholder="Message the agent…" required ${disabled}>
        <button type="submit" aria-label="Send" ${disabled}>${icon('up')}</button>
      </form>
    </div>`;
}

function activity() {
  const status = jobStatus();
  const question = status === 'waiting' ? state.job?.question : null;
  const body = state.events.length
    ? activityEvents().map(eventHtml).join('')
    : `<div class="quiet-empty"><div class="agent-logo">${icon('spark')}</div><p>No activity yet.</p></div>`;
  return `
    <section class="panel activity">
      <div class="panel-head">
        <h2>${icon('spark')} Activity</h2>
        <div class="controls">${activityControls(status)}</div>
      </div>
      <div class="stream-bar">
        <strong>${esc(state.job ? state.job.id : 'Ready')}</strong>
        <span class="live">${isActive() ? 'LIVE UPDATES' : ''}</span>
      </div>
      ${state.job?.work_plan ? `<div class="work-focus" aria-label="Current work"><strong>${esc(state.job.work_plan.focus)}</strong>${state.job.progress_checkpoint?.message ? `<p>${esc(state.job.progress_checkpoint.message)}</p>` : ''}${state.job.work_plan.items.filter(item => item.status === 'blocked').map(item => `<p class="error-message">${esc(item.blocker || item.objective)}</p>`).join('')}</div>` : ''}
      <div class="activity-body" id="activity-scroll" role="log" aria-label="Agent activity" aria-live="polite">
        ${body}${question ? questionForm(question) : ''}
      </div>
      ${state.job ? messageForm(status) : ''}
    </section>`;
}

const detailRow = ([label, value]) =>
  `<div class="session-row"><span>${label}</span><strong>${esc(value)}</strong></div>`;

// Coverage and context figures belong with the trace, not on the task screen.
function harnessDetails(job) {
  if (!job?.coverage) return '';
  return [
    ['Accounting version', job.coverage.accounting_version ?? 1],
    ['Discovered regions', Object.values(job.coverage.regions || {}).reduce((total, count) => total + count, 0)],
    ['Context estimate method', job.context_usage?.method ?? '—'],
    ['Fixed context cost', job.context_usage?.fixed_tokens ?? '—'],
    ['Recovery turn', job.supervision?.recovery ?? '—'],
    ['Source observations', job.coverage.source_observations],
    ['Rows included', job.coverage.records.included],
    ['Open coverage items', job.coverage.open_items],
    ['Context estimate', job.context_usage?.estimated_tokens ?? '—'],
    ['Context input budget', job.context_usage?.budget ?? '—'],
  ]
    .map(detailRow)
    .join('');
}

function output() {
  const job = state.job;
  const usage = job?.usage || {};
  const artifacts = job?.artifacts?.length
    ? job.artifacts
        .map(
          (a, i) => `<button class="btn artifact-download" data-artifact="${i}">${icon('down')}${esc(a.name)}</button>`,
        )
        .join('')
    : '<div class="artifact-meta">No output yet.</div>';
  const details = [
    ['Model', state.config?.model || '—'],
    ['Sources', job ? job.files.filter((f) => f.role !== 'template').length : state.files.length],
    ['Model calls', usage.requests ?? '—'],
    ['Input tokens', usage.input_tokens ?? '—'],
    ['Output tokens', usage.output_tokens ?? '—'],
    ['Rows to resolve', job?.coverage ? job.coverage.records.pending + job.coverage.records.unresolved : '—'],
  ]
    .map(detailRow)
    .join('');
  return `
    <aside class="right-stack">
      <section class="panel">
        <div class="panel-head"><h2>${icon('file')} Output</h2></div>
        <div class="panel-body">
          ${job?.error ? `<p class="error-message">${esc(job.error)}</p>` : ''}
          ${job?.summary ? `<p class="output-summary">${esc(job.summary)}</p>` : ''}
          ${artifacts}
        </div>
      </section>
      <section class="panel session">
        <div class="panel-head"><h2>${icon('terminal')} Run details</h2></div>
        <div class="panel-body">${details}${btn('diagnostics', 'Diagnostics')}</div>
      </section>
    </aside>`;
}

// Lower-cased JSON of each event, built once, so searching thousands of events stays quick.
const searchText = new WeakMap();
function matchesSearch(event) {
  if (!state.search) return true;
  if (!searchText.has(event)) searchText.set(event, JSON.stringify(event).toLowerCase());
  return searchText.get(event).includes(state.search.toLowerCase());
}

function matchesFilter(event) {
  if (state.filter === 'warnings') return isWarning(event);
  return state.filter === 'all' || event.type === state.filter;
}

function diagnostics() {
  const list = state.events.filter((e) => matchesFilter(e) && matchesSearch(e));
  const filterButtons = FILTERS.map(
    ([id, label]) => `<button data-filter="${id}" class="${state.filter === id ? 'active' : ''}">${label}</button>`,
  ).join('');
  const rows = list
    .map(
      (e) => `
        <tr tabindex="0" role="button" data-event="${e.seq}">
          <td class="mono">${fmtTime(e.timestamp)}</td>
          <td class="trace-name">${esc(e.name)}<div class="trace-message">${esc(e.message)}</div></td>
          <td>${esc(e.level || 'info')}</td>
          <td>${e.duration_ms == null ? '—' : esc(e.duration_ms) + ' ms'}</td>
        </tr>`,
    )
    .join('');
  return `
    <section class="panel page-panel">
      <div class="panel-head">
        <h2>${icon('terminal')} Execution trace</h2>
        ${btn('export', 'Export JSONL', 'small', !state.job)}
      </div>
      <div class="diagnostic-toolbar">
        <div class="filter-buttons">${filterButtons}</div>
        <input type="search" id="log-search" aria-label="Search diagnostics" placeholder="Search events…"
          value="${esc(state.search)}">
      </div>
      ${harnessDetails(state.job)}
      <div class="preview-table-wrap">
        <table class="trace-table">
          <thead><tr><th>Time</th><th>Operation</th><th>Level</th><th>Duration</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
        ${list.length ? '' : '<div class="empty-state">No events.</div>'}
      </div>
    </section>`;
}

function instructions() {
  const context = state.contexts[state.module] ?? currentModule()?.context ?? '';
  return `
    <div class="page-panel instructions-grid">
      <section class="panel instruction-card">
        <h3>Module context</h3>
        <div class="field">
          <label class="sr-only" for="module-context">Reusable module context</label>
          <textarea id="module-context">${esc(context)}</textarea>
        </div>
        <div class="instruction-foot">${btn('save-context', 'Save', 'primary', state.busy)}</div>
      </section>
      <section class="panel instruction-card">
        <h3>This run</h3>
        <div class="field">
          <label class="sr-only" for="run-instructions">Run instructions</label>
          <textarea id="run-instructions" ${attr('disabled', !!state.job)}>${esc(runInstructions())}</textarea>
        </div>
        <div class="instruction-foot">${btn('workspace', 'Done')}</div>
      </section>
    </div>`;
}

function history() {
  const rows = state.jobs.length
    ? state.jobs
        .map(
          (j) => `
            <div class="history-row">
              <div>
                ${esc(j.period || '')} · ${esc(j.module)}
                <small>${esc(j.id)} · ${esc(new Date(j.created_at).toLocaleString())}</small>
              </div>
              ${statusHtml(j.status)}
              <button class="btn small" data-job="${esc(j.id)}">Open</button>
            </div>`,
        )
        .join('')
    : '<div class="empty-state">No runs yet.</div>';
  return `
    <section class="panel page-panel">
      <div class="panel-head"><h2>${icon('clock')} Runs</h2>${btn('refresh-history', 'Refresh', 'small')}</div>
      ${rows}
    </section>`;
}

function notices() {
  return [
    state.config && !state.config.configured
      ? `<div class="notice">Set OPENAI_API_KEY in .env, then restart. ${btn('refresh-config', 'Refresh', 'small')}</div>`
      : '',
    state.connectionError ? `<div class="notice error-message">${esc(state.connectionError)}</div>` : '',
    state.notice ? `<div class="notice error-message">${esc(state.notice)}</div>` : '',
    state.module === 'ap' ? '<div class="notice">Accounts payable · coming soon</div>' : '',
  ].join('');
}

function page() {
  if (state.view === 'workspace') return `<div class="workspace-grid">${inputs()}${activity()}${output()}</div>`;
  if (state.view === 'instructions') return instructions();
  if (state.view === 'diagnostics') return diagnostics();
  return history();
}

function pageTitle() {
  if (state.view !== 'workspace') return PAGE_TITLES[state.view];
  return state.module === 'payroll' ? 'Monthly payroll ingestion' : 'Accounts payable';
}

// Re-render the whole app, keeping focus, caret position and the activity scroll position.
function render() {
  const focused = document.activeElement?.id;
  const selection = focused ? document.activeElement.selectionStart : null;
  const scroller = $('#activity-scroll');
  const scroll = scroller?.scrollTop;
  const nearBottom = !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;

  $('#app').innerHTML = `
    <div class="shell">
      ${nav()}
      <main class="main">
        <header class="topbar">
          <div class="crumb">Document Intelligence <span>/</span><b>${esc(currentModule()?.label)}</b></div>
          <div class="mobile-brand">Document Intelligence</div>
          <div class="topmeta">
            <span>${esc(state.config?.model || '')}</span>
            ${btn('connection', 'Test OpenAI', 'small', state.busy)}
          </div>
        </header>
        <div class="content">
          ${notices()}
          <div class="heading">
            <h1>${esc(pageTitle())}</h1>
            <div class="heading-right">${state.view === 'workspace' ? statusHtml(jobStatus()) : ''}</div>
          </div>
          ${page()}
        </div>
      </main>
    </div>`;

  const el = focused && $('#' + focused);
  if (el) {
    el.focus();
    if (typeof selection === 'number' && ['textarea', 'search', 'text'].includes(el.type)) {
      el.setSelectionRange(selection, selection);
    }
  }
  const newScroller = $('#activity-scroll');
  if (newScroller) newScroller.scrollTop = nearBottom ? newScroller.scrollHeight : scroll || 0;
}

function detail(event) {
  $('#details').innerHTML = `
    <div class="dialog-head">
      <h2>${esc(event.name || 'Event')}</h2>
      <button data-close aria-label="Close">×</button>
    </div>
    <pre>${esc(JSON.stringify(event, null, 2))}</pre>`;
  $('#details').showModal();
}

// --- Actions ---------------------------------------------------------------

async function refreshConfig() {
  state.config = await api('/api/config');
  for (const m of modules()) if (state.contexts[m.id] === undefined) state.contexts[m.id] = m.context || '';
}

async function loadJob(id) {
  state.loading = true;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    state.job = job;
    state.module = job.module;
    state.events = await api(`/api/jobs/${encodeURIComponent(id)}/events?after=0`);
    state.view = 'workspace';
  } finally {
    state.loading = false;
  }
  render();
}

function resetRun() {
  state.job = null;
  state.events = [];
  state.files = [];
  state.template = null;
}

async function uploadAndStart() {
  if (!state.job) {
    const form = new FormData();
    form.append('module', state.module);
    form.append('period', state.period);
    form.append('instructions', state.instructions[state.module] || '');
    form.append('context', state.contexts[state.module] || '');
    for (const f of state.files) form.append('files', f, f.webkitRelativePath || f.name);
    form.append('template', state.template, state.template.name);
    state.job = await api('/api/jobs', { method: 'POST', body: form });
    state.events = [];
  }
  if (state.config?.configured) await api(`/api/jobs/${state.job.id}/start`, { method: 'POST' });
  else toast('Files uploaded. Add OPENAI_API_KEY to .env and restart to run.');
  state.job = await api(`/api/jobs/${state.job.id}`);
  state.jobs = await api('/api/jobs');
}

// Actions that only change the screen; they run instantly.
const localActions = {
  workspace: () => (state.view = 'workspace'),
  diagnostics: () => (state.view = 'diagnostics'),
  folder: () => $('#folder-picker').click(),
  files: () => $('#files-picker').click(),
  template: () => $('#template-picker').click(),
  new: resetRun,
};

// Actions that call the server; the page shows "busy" while they run.
const serverActions = {
  'refresh-config': refreshConfig,
  connection: async () => {
    detail({ name: 'OpenAI connection', ...(await api('/api/connection-check', { method: 'POST' })) });
    await refreshConfig();
  },
  'refresh-history': async () => (state.jobs = await api('/api/jobs')),
  'save-context': async () => {
    await api(`/api/modules/${encodeURIComponent(state.module)}`, {
      method: 'PUT',
      body: JSON.stringify({ context: state.contexts[state.module] || '' }),
    });
    toast('Module context saved.');
  },
  start: uploadAndStart,
  export: () => download(`/api/jobs/${state.job.id}/logs`, `${state.job.id}.jsonl`),
};
for (const name of ['pause', 'resume', 'cancel']) {
  serverActions[name] = async () => {
    await api(`/api/jobs/${state.job.id}/${name}`, { method: 'POST' });
    state.job = await api(`/api/jobs/${state.job.id}`);
  };
}

async function action(name) {
  if (localActions[name]) {
    localActions[name]();
    if (!['folder', 'files', 'template'].includes(name)) render();
    return;
  }
  state.busy = true;
  state.notice = '';
  render();
  try {
    await serverActions[name]();
  } catch (e) {
    state.notice = e.message;
    toast(e.message);
  } finally {
    state.busy = false;
    render();
  }
}

async function openView(view) {
  state.view = view;
  if (view === 'history') {
    try {
      state.jobs = await api('/api/jobs');
    } catch (e) {
      toast(e.message);
    }
  }
  render();
}

async function downloadArtifact(index) {
  const artifact = state.job.artifacts[index];
  await download(`/api/jobs/${state.job.id}/artifacts/${encodeURIComponent(artifact.name)}`, artifact.name);
}

async function sendText(form) {
  const input = form.querySelector('textarea,input');
  const text = input.value.trim();
  if (!text) return;
  const kind = form.id === 'answer-form' ? 'answer' : 'message';
  await api(`/api/jobs/${state.job.id}/${kind}`, { method: 'POST', body: JSON.stringify({ [kind]: text }) });
  input.value = '';
  state.drafts[state.job.id + ':' + kind] = '';
  state.job = await api(`/api/jobs/${state.job.id}`);
  if (kind === 'message') toast('Instruction sent.');
  render();
}

// --- Event wiring ----------------------------------------------------------

document.addEventListener('click', async (e) => {
  const target = (selector) => e.target.closest(selector);
  try {
    if (target('[data-close]')) $('#details').close();
    else if (target('[data-action]')) await action(target('[data-action]').dataset.action);
    else if (target('[data-view]')) await openView(target('[data-view]').dataset.view);
    else if (target('[data-filter]')) {
      state.filter = target('[data-filter]').dataset.filter;
      render();
    } else if (target('[data-event]')) {
      const item = state.events.find((x) => String(x.seq) === target('[data-event]').dataset.event);
      if (item) detail(item);
    } else if (target('[data-job]')) await loadJob(target('[data-job]').dataset.job);
    else if (target('[data-artifact]')) await downloadArtifact(Number(target('[data-artifact]').dataset.artifact));
  } catch (err) {
    toast(err.message);
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.matches('tr[data-event]')) e.target.click();
});

let searchTimer;
document.addEventListener('input', (e) => {
  const { id, value } = e.target;
  if (id === 'message' || id === 'answer') state.drafts[state.job?.id + ':' + id] = value;
  if (id === 'run-instructions') state.instructions[state.module] = value;
  if (id === 'module-context') state.contexts[state.module] = value;
  if (id === 'period') state.period = value;
  if (id === 'log-search') {
    state.search = value;
    // Wait for a pause in typing before filtering the trace.
    clearTimeout(searchTimer);
    searchTimer = setTimeout(render, 150);
  }
});

document.addEventListener('change', (e) => {
  if (e.target.matches('[data-module]')) {
    state.module = e.target.value;
    resetRun();
  } else if (e.target.id === 'folder-picker' || e.target.id === 'files-picker') {
    state.files = Array.from(e.target.files);
    e.target.value = '';
  } else if (e.target.id === 'template-picker') {
    state.template = e.target.files[0] || null;
    e.target.value = '';
  } else return;
  render();
});

document.addEventListener('submit', async (e) => {
  if (!['answer-form', 'message-form'].includes(e.target.id)) return;
  e.preventDefault();
  try {
    await sendText(e.target);
  } catch (err) {
    toast(err.message);
  }
});

// --- Polling ---------------------------------------------------------------

async function refreshJob() {
  const id = state.job.id;
  const after = state.events.reduce((n, e) => Math.max(n, e.seq), 0);
  const [job, events] = await Promise.all([api(`/api/jobs/${id}`), api(`/api/jobs/${id}/events?after=${after}`)]);
  if (state.job?.id !== id) return;
  const changed = JSON.stringify(job) !== JSON.stringify(state.job) || events.length > 0 || !!state.connectionError;
  state.connectionError = '';
  state.job = job;
  const seen = new Set(state.events.map((e) => e.seq));
  state.events.push(...events.filter((e) => !seen.has(e.seq)));
  if (changed) render();
}

async function poll() {
  if (state.job && !state.polling && !state.loading) {
    state.polling = true;
    try {
      await refreshJob();
    } catch (e) {
      if (state.connectionError !== e.message) {
        state.connectionError = e.message;
        render();
      }
    } finally {
      state.polling = false;
    }
  }
  setTimeout(poll, isActive() ? 1500 : 8000);
}

render();
(async () => {
  try {
    await refreshConfig();
    state.jobs = await api('/api/jobs');
  } catch (e) {
    state.notice = e.message;
  }
  render();
  poll();
})();
