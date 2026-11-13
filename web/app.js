// Draws the app and handles what the person does. State and helpers live in core.js; the screens in thread.js,
// files.js and pages.js.

import {
  $, api, attr, baseName, btn, download, duration, esc, fileUrl, icon, isActive, jobStatus, jobTitle, jobUrl,
  modules, sandboxEngine, state, toast,
} from './core.js'; // prettier-ignore
import { backFile, closeFile, filesHtml, loadMap, openFile, proofHtml, turnPage } from './files.js';
import { instructionsPage, logPage, settingsPage, setupPage, startBlocked } from './pages.js';
import { composerHtml, headHtml, stagesHtml, threadHtml } from './thread.js';

const RUN = `
  <div id="notices"></div>
  <div class="work" id="work">
    <section class="chat">
      <header class="chat-head" id="chat-head"></header>
      <div class="stages" id="stages"></div>
      <div class="scroll" id="scroll"><div class="thread" id="thread" role="log" aria-label="Conversation with the agent" aria-live="polite"></div></div>
      <div class="composer" id="composer-wrap"></div>
    </section>
    <aside class="files" id="files" aria-label="Files"></aside>
  </div>`;

// --- Drawing ---------------------------------------------------------------

// Redraw one region only when its content changed, so typing, scrolling and open previews are left alone.
function paint(selector, html) {
  const el = $(selector);
  if (!el || el.__html === html) return false;
  el.innerHTML = html;
  el.__html = html;
  return true;
}

function runItems() {
  return state.jobs.map((j) => {
    const status = j.id === state.job?.id ? state.job.status : j.status;
    const dot = state.config?.statuses?.active?.includes(status) ? '<i></i>' : status === 'waiting' ? '<i class="ask"></i>' : '';
    const on = j.id === state.job?.id && ['run', 'log'].includes(state.view);
    const day = new Date(j.created_at).toLocaleDateString('en', { day: 'numeric', month: 'short' });
    return `<button class="item ${on ? 'on' : ''}" data-job="${esc(j.id)}"><span>${esc(jobTitle(j))}</span>${dot}<time>${day}</time></button>`;
  });
}

function sideHtml() {
  const link = (view, label) => `<button class="item ${state.view === view ? 'on' : ''}" data-view="${view}"><span>${label}</span></button>`;
  return `
    <div class="brand">Document Intelligence</div>
    ${btn('new', 'New run', 'quiet add', false, 'plus')}
    <nav class="runs" aria-label="Recent runs"><h3>Recent</h3>${runItems().join('') || '<p class="empty item">No runs yet</p>'}</nav>
    <div class="foot">${link('instructions', 'Instructions')}${link('settings', 'Settings')}</div>`;
}

// Narrow screens swap the sidebar for one menu.
function topnavHtml() {
  const here = state.view === 'run' ? (state.job ? 'job:' + state.job.id : 'new') : state.view;
  const option = (value, label) => `<option value="${esc(value)}" ${attr('selected', value === here)}>${esc(label)}</option>`;
  const runs = state.jobs.map((j) =>
    option('job:' + j.id, `${jobTitle(j)}, ${new Date(j.created_at).toLocaleDateString('en', { day: 'numeric', month: 'short' })}`),
  );
  return `<div class="brand">Document Intelligence</div>
    <select aria-label="Go to" data-nav>${option('new', 'New run')}${runs.join('')}${state.job ? option('log', 'Technical log') : ''}${option('instructions', 'Instructions')}${option('settings', 'Settings')}</select>`;
}

function noticesHtml() {
  return [
    state.config && !state.config.configured
      ? `<div class="notice"><span>Add OPENAI_API_KEY to the .env file, then restart the app.</span>${btn('refresh-config', 'Check again', 'text')}</div>`
      : '',
    state.connectionError ? `<div class="notice bad">${esc(state.connectionError)}</div>` : '',
    state.notice ? `<div class="notice bad">${esc(state.notice)}</div>` : '',
    !state.job && state.view === 'run' && state.module === 'ap' && !sandboxEngine()
      ? `<div class="notice"><span>Accounts payable runs on the sandbox engine.</span><button class="btn text" data-view="settings">Open Settings</button></div>`
      : '',
  ].join('');
}

function tick() {
  for (const el of document.querySelectorAll('[data-since]')) {
    const since = new Date(el.dataset.since).getTime();
    if (since) el.textContent = duration(Date.now() - since);
  }
}

function syncComposer() {
  const box = $('#msg');
  if (!box) return;
  const draft = state.drafts[state.job.id] || '';
  if (box.value !== draft) box.value = draft;
  box.style.height = '34px';
  box.style.height = Math.min(140, box.scrollHeight) + 'px';
  $('.send').disabled = box.disabled || !draft.trim();
}

function render() {
  const focused = document.activeElement?.id;
  const selection = focused ? document.activeElement.selectionStart : null;
  paint('#side', sideHtml());
  paint('#topnav', topnavHtml());
  document.title = `${state.job && state.view === 'run' ? jobTitle(state.job) + ' · ' : ''}Document Intelligence`;

  if (state.view === 'run' && state.job) {
    paint('#main', RUN);
    $('#work').className = `work ${state.open ? 'viewing' : ''} ${state.showFiles ? 'show-files' : ''}`;
    paint('#notices', noticesHtml());
    paint('#chat-head', headHtml());
    paint('#stages', stagesHtml());
    const box = $('#scroll');
    const first = box.__job !== state.job.id;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (paint('#thread', threadHtml()) && (first || nearBottom)) box.scrollTop = box.scrollHeight;
    box.__job = state.job.id;
    state.seen = state.events.reduce((n, e) => Math.max(n, e.seq), state.seen);
    paint('#composer-wrap', composerHtml());
    syncComposer();
    paint('#files', filesHtml());
    tick();
  } else {
    const body = { run: setupPage, instructions: instructionsPage, settings: settingsPage, log: logPage }[state.view]();
    paint('#main', noticesHtml() + body);
  }

  const el = focused && $('#' + focused);
  if (el && el !== document.activeElement) {
    el.focus();
    if (typeof selection === 'number' && ['textarea', 'search', 'text'].includes(el.type)) el.setSelectionRange(selection, selection);
  }
}

function detail(event) {
  $('#details').innerHTML = `
    <div class="dialog-head"><h2>${esc(event.name || 'Event')}</h2><button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
    <pre>${esc(JSON.stringify(event, null, 2))}</pre>`;
  $('#details').showModal();
}

// --- Actions ---------------------------------------------------------------

async function refreshSettings() {
  try {
    state.settings = await api('/api/settings');
    state.settingsDraft = { ...state.settings.values };
    state.settingsError = '';
  } catch (e) {
    state.settingsError = `Settings could not be loaded: ${e.message}`;
  }
}

async function refreshConfig() {
  state.config = await api('/api/config');
  for (const m of modules()) if (state.contexts[m.id] === undefined) state.contexts[m.id] = m.context || '';
}

function leaveRun() {
  closeFile();
  Object.assign(state, { events: [], seen: 0, map: null, mapKey: '', mapFilter: 'all', showFiles: false, search: '', filter: 'all' });
  state.toggled.clear();
}

async function loadJob(id) {
  state.loading = true;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    const events = await api(`/api/jobs/${encodeURIComponent(id)}/events?after=0`);
    leaveRun();
    Object.assign(state, { job, events, module: job.module, view: 'run', notice: '' });
    state.seen = events.reduce((n, e) => Math.max(n, e.seq), 0); // an opened run appears whole, without animating in
    await loadMap();
  } finally {
    state.loading = false;
  }
  render();
}

function newRun() {
  leaveRun();
  Object.assign(state, { job: null, files: [], template: null, view: 'run', notice: '' });
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
    const job = await api('/api/jobs', { method: 'POST', body: form });
    leaveRun();
    Object.assign(state, { job, files: [], template: null });
  }
  if (state.config?.configured) await api(jobUrl('/start'), { method: 'POST' });
  else toast('Files uploaded. Add OPENAI_API_KEY to .env and restart to run.');
  state.job = await api(jobUrl());
  state.jobs = await api('/api/jobs');
}

// Actions that only change the screen; they run instantly.
const localActions = {
  new: newRun,
  run: () => (state.view = 'run'),
  log: () => (state.view = 'log'),
  folder: () => $('#folder-picker').click(),
  files: () => $('#files-picker').click(),
  template: () => {
    // A sandbox run can build to a written format (.json) instead of filling a template.
    $('#template-picker').accept = sandboxEngine() && state.deliverable === 'format' ? '.json' : '.xlsx';
    $('#template-picker').click();
  },
  'toggle-files': () => (state.showFiles = !state.showFiles),
  'close-file': closeFile,
};
localActions['back-file'] = () => backFile(render);

// Actions that call the server; buttons are held while they run.
const serverActions = {
  'refresh-config': refreshConfig,
  connection: async () => {
    const result = await api('/api/connection-check', { method: 'POST' });
    toast(`Connected to ${result.model}.`);
    await refreshConfig();
  },
  'save-settings': async () => {
    const draft = { ...state.settingsDraft };
    for (const [name, option] of Object.entries(state.settings.options)) {
      if (typeof option.default === 'number') draft[name] = Number(draft[name]);
    }
    state.settings = await api('/api/settings', { method: 'PUT', body: JSON.stringify({ values: draft }) });
    state.settingsDraft = { ...state.settings.values };
    toast('Settings saved.');
  },
  'save-context': async () => {
    await api(`/api/modules/${encodeURIComponent(state.module)}`, {
      method: 'PUT',
      body: JSON.stringify({ context: state.contexts[state.module] || '' }),
    });
    toast('Instructions saved.');
  },
  start: uploadAndStart,
  export: () => download(jobUrl('/logs'), `${state.job.id}.jsonl`),
};
for (const name of ['pause', 'resume', 'cancel']) {
  serverActions[name] = async () => {
    await api(jobUrl('/' + name), { method: 'POST' });
    await refreshJob();
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
  } finally {
    state.busy = false;
    render();
  }
}

async function openView(view) {
  state.view = view;
  state.notice = '';
  render();
  if (view === 'settings') {
    await refreshSettings();
    render();
  }
}

async function sendText() {
  const text = (state.drafts[state.job.id] || '').trim();
  if (!text) return;
  const kind = jobStatus() === 'waiting' && state.job.question ? 'answer' : 'message';
  state.drafts[state.job.id] = '';
  syncComposer();
  try {
    await api(jobUrl('/' + kind), { method: 'POST', body: JSON.stringify({ [kind]: text }) });
    await refreshJob();
  } catch (e) {
    state.drafts[state.job.id] = text; // keep what they typed
    throw e;
  } finally {
    render();
  }
}

// --- Event wiring ----------------------------------------------------------

document.addEventListener('click', async (e) => {
  const target = (selector) => e.target.closest(selector);
  let el;
  try {
    if (target('[data-close]')) $('#details').close();
    else if ((el = target('[data-action]'))) await action(el.dataset.action);
    else if ((el = target('[data-view]'))) await openView(el.dataset.view);
    else if ((el = target('[data-job]'))) await loadJob(el.dataset.job);
    else if ((el = target('[data-download]'))) await download(fileUrl('file', el.dataset.download), baseName(el.dataset.download));
    else if ((el = target('[data-open]'))) {
      const from = target('.proof') && state.preview;
      const back = from ? { path: from.path, sheet: from.data?.sheet, row: state.row } : null;
      await openFile(el.dataset.open, render, { back });
    } else if ((el = target('[data-sheet]'))) await openFile(state.open, render, { sheet: el.dataset.sheet, back: state.preview?.back });
    else if ((el = target('[data-page]'))) await turnPage(Number(el.dataset.page), render);
    else if ((el = target('tr[data-row]'))) {
      // Mark the row and fill in its sources in place: redrawing the sheet would lose the person's scroll position.
      state.row = Number(el.dataset.row);
      $('.grid tr.sel')?.classList.remove('sel');
      el.classList.add('sel');
      $('#proof').innerHTML = proofHtml();
      $('#files').__html = filesHtml();
    } else if ((el = target('[data-group]'))) {
      const id = Number(el.dataset.group);
      state.toggled.has(id) ? state.toggled.delete(id) : state.toggled.add(id);
      render();
    } else if ((el = target('[data-segment]'))) {
      const { segment: name, value } = el.dataset;
      if (name === 'deliverable') {
        state.deliverable = value;
        state.template = null;
      } else state.settingsDraft[name] = value;
      render();
    } else if ((el = target('[data-toggle]'))) {
      state.settingsDraft[el.dataset.toggle] = !state.settingsDraft[el.dataset.toggle];
      render();
    } else if ((el = target('[data-step]'))) {
      const { step: name, delta } = el.dataset;
      const option = state.settings.options[name];
      state.settingsDraft[name] = Math.min(option.high, Math.max(option.low, state.settingsDraft[name] + Number(delta)));
      render();
    } else if ((el = target('[data-map-filter]'))) {
      state.mapFilter = el.dataset.mapFilter;
      render();
    } else if ((el = target('[data-filter]'))) {
      state.filter = el.dataset.filter;
      render();
    } else if ((el = target('[data-event]'))) {
      const item = state.events.find((x) => String(x.seq) === el.dataset.event);
      if (item) detail(item);
    }
  } catch (err) {
    toast(err.message);
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.matches('tr[data-event], tr[data-row]')) e.target.click();
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.target.id === 'msg') {
    e.preventDefault();
    $('#composer').requestSubmit();
  }
});

let searchTimer;
document.addEventListener('input', (e) => {
  const { id, value } = e.target;
  if (id === 'msg') {
    state.drafts[state.job.id] = value;
    syncComposer();
  }
  if (id === 'run-instructions') state.instructions[state.module] = value;
  if (id === 'module-context') state.contexts[state.module] = value;
  if (id === 'period') {
    state.period = value;
    $('[data-action="start"]').disabled = startBlocked();
  }
  if (e.target.dataset.setting) state.settingsDraft[e.target.dataset.setting] = value;
  if (id === 'log-search') {
    state.search = value;
    // Wait for a pause in typing before filtering the log.
    clearTimeout(searchTimer);
    searchTimer = setTimeout(render, 150);
  }
});

document.addEventListener('change', async (e) => {
  if (e.target.matches('[data-module]')) state.module = e.target.value;
  else if (e.target.matches('[data-nav]')) {
    const value = e.target.value;
    try {
      if (value === 'new') newRun();
      else if (value.startsWith('job:')) return await loadJob(value.slice(4));
      else return await openView(value);
    } catch (err) {
      toast(err.message);
    }
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
  if (e.target.id !== 'composer') return;
  e.preventDefault();
  try {
    await sendText();
  } catch (err) {
    toast(err.message);
  }
});

// --- Polling ---------------------------------------------------------------

async function refreshJob() {
  const id = state.job.id;
  const after = state.events.reduce((n, ev) => Math.max(n, ev.seq), 0);
  const [job, events] = await Promise.all([api(`/api/jobs/${id}`), api(`/api/jobs/${id}/events?after=${after}`)]);
  if (state.job?.id !== id) return;
  const statusChanged = job.status !== state.job.status;
  state.connectionError = '';
  state.job = job;
  const seen = new Set(state.events.map((ev) => ev.seq));
  state.events.push(...events.filter((ev) => !seen.has(ev.seq)));
  await loadMap();
  if (statusChanged) state.jobs = await api('/api/jobs');
}

async function poll() {
  if (state.job && !state.polling && !state.loading) {
    state.polling = true;
    try {
      await refreshJob();
    } catch (e) {
      state.connectionError = e.message;
    } finally {
      state.polling = false;
    }
    render();
  }
  setTimeout(poll, isActive() ? 1500 : 8000);
}

render();
setInterval(tick, 1000);
(async () => {
  try {
    await Promise.all([refreshConfig(), refreshSettings()]); // refreshSettings keeps its own error for its page
    state.jobs = await api('/api/jobs');
  } catch (e) {
    state.notice = e.message;
  }
  render();
  poll();
})();
