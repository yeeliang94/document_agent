// Draws the app and handles what the person does. State and helpers live in core.js; the screens in thread.js,
// files.js and pages.js.

import {
  $, api, attr, baseName, btn, currentPlaybook, download, duration, esc, fileUrl, icon, inGroup, isActive, jobStatus, jobTitle, telling,
  jobUrl, state, toast,
} from './core.js'; // prettier-ignore
import {
  MAX_BLOCK, blockCells, chooseRemember, columnName, commentAction, commenting, dropComment, editPin, forget, keepComment,
  loadRounds, placePop, popHtml, resolveComment, sendComments, sendReply, sentComment, syncPop, syncReply, writeAt,
} from './comments.js'; // prettier-ignore
import {
  chooseRunClient, clientActions, clientClick, clientDialogs, clientPage, clientsPage, offerHtml, offerLocal, offerServer,
  openClient, refreshClients,
  submitClientDialog, suggestClient, syncClient, typeNotes,
} from './clients.js'; // prettier-ignore
import { backFile, closeFile, filesHtml, loadMap, openFile, openSpot, proofHtml, turnPage } from './files.js';
import { loadLinks } from './links.js';
import { applyWidths } from './panels.js';
import { logPage, settingProblem, settingsChanged, settingsPage, settingsValues, setupPage, startBlocked } from './pages.js';
import {
  applyPlace, canFind, closeFind, foundFor, loadChanges, locateRow, openFind, revealMarks, stepChange, stepFind, typed,
} from './review.js'; // prettier-ignore
import {
  askTune, leaveVersion, openPlaybook, playbookActions, playbookClick, playbookDialogs, playbookPage, playbooksPage, refreshPlaybookTab,
  startDraft, submitDialog, syncPlaybook, uploadSkill,
} from './playbooks.js'; // prettier-ignore
import { askPermission, clearMark, noticeRuns, tabTitle } from './alerts.js';
import { pageAddress, parseAddress } from './routes.js';
import { confirmStartOver, deleteRun, renameRun, runItems, submitRun } from './runs.js';
import { composerHtml, headHtml, nearLimits, planHtml, scriptKey, stagesHtml, threadHtml } from './thread.js';
import { continueFrom, loadVersions } from './versions.js';
import { dismissVoiceNote, initVoice, rememberCaret, syncVoice, toggleVoice } from './voice.js';
import { applyZoom, canZoom, wheelZoom, zoomAction } from './zoom.js';

const RUN = `
  <div id="notices"></div>
  <div class="work" id="work">
    <section class="chat">
      <header class="chat-head" id="chat-head"></header>
      <div class="stages" id="stages"></div>
      <div class="plan-wrap" id="plan"></div>
      <div id="offer"></div>
      <div class="scroll" id="scroll"><div class="thread" id="thread" role="log" aria-label="Conversation with the agent" aria-live="polite"></div></div>
      <div class="composer" id="composer-wrap"></div>
    </section>
    <div class="grip" id="files-grip" role="separator" aria-orientation="vertical" aria-label="Files panel width" tabindex="0"></div>
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

function sideHtml() {
  const here = listView();
  const link = (view, label) => `<button class="item ${here === view ? 'on' : ''}" data-view="${view}"><span>${label}</span></button>`;
  return `
    <div class="brand">Document Intelligence</div>
    ${btn('new', 'New run', 'quiet add', false, 'plus')}
    <nav class="runs" aria-label="Recent runs"><h3>Recent</h3>${runItems().join('') || '<p class="empty item">No runs yet</p>'}</nav>
    <div class="foot">${link('clients', 'Clients')}${link('playbooks', 'Playbooks')}${link('settings', 'Settings')}</div>`;
}

// A page of one playbook or client belongs to its list in the navigation.
const listView = () => ({ playbook: 'playbooks', client: 'clients' })[state.view] || state.view;

// Narrow screens swap the sidebar for one menu.
function topnavHtml() {
  const view = listView();
  const here = view === 'run' ? (state.job ? 'job:' + state.job.id : 'new') : view;
  const option = (value, label) => `<option value="${esc(value)}" ${attr('selected', value === here)}>${esc(label)}</option>`;
  const when = telling(state.jobs);
  const runs = state.jobs.map((j) =>
    option('job:' + j.id, `${jobTitle(j)}, ${when[j.id] || new Date(j.created_at).toLocaleDateString('en', { day: 'numeric', month: 'short' })}`),
  );
  return `<div class="brand">Document Intelligence</div>
    <select aria-label="Go to" data-nav>${option('new', 'New run')}${runs.join('')}${state.job ? option('log', 'Technical log') : ''}${option('clients', 'Clients')}${option('playbooks', 'Playbooks')}${option('settings', 'Settings')}</select>`;
}

function noticesHtml() {
  return [
    state.config && !state.config.configured
      ? `<div class="notice"><span>Add OPENAI_API_KEY to the .env file, then restart the app.</span>${btn('refresh-config', 'Check again', 'text')}</div>`
      : '',
    state.connectionError ? `<div class="notice bad">${esc(state.connectionError)}</div>` : '',
    state.notice ? `<div class="notice bad">${esc(state.notice)}</div>` : '',
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
  $('.send').disabled = box.disabled || !(draft.trim() || attached().length);
}

// Files waiting to go with the next message to the agent.
const attached = () => (state.attachments[state.job?.id] ||= []);

function attach(files) {
  const list = attached();
  for (const file of files) {
    if (/\.zip$/i.test(file.name)) toast('Add the files inside a ZIP one by one.');
    else if (!list.some((f) => f.name === file.name && f.size === file.size)) list.push(file);
  }
  render();
  $('#msg')?.focus();
}

function render() {
  const focused = document.activeElement?.id;
  const selection = focused ? document.activeElement.selectionStart : null;
  paint('#side', sideHtml());
  paint('#topnav', topnavHtml());
  document.body.classList.toggle('loading', !!state.loading);
  document.title = tabTitle(`${state.job && state.view === 'run' ? jobTitle(state.job) + ' · ' : ''}Document Intelligence`);

  if (state.view === 'run' && state.job) {
    paint('#main', RUN);
    $('#work').className = `work ${state.open ? 'viewing' : ''} ${state.showFiles ? 'show-files' : ''}`;
    paint('#notices', noticesHtml());
    paint('#chat-head', headHtml());
    paint('#stages', stagesHtml());
    paint('#plan', planHtml());
    paint('#offer', offerHtml());
    const box = $('#scroll');
    const first = box.__job !== state.job.id;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (paint('#thread', threadHtml()) && (first || nearBottom)) box.scrollTop = box.scrollHeight;
    box.__job = state.job.id;
    state.seen = state.events.reduce((n, e) => Math.max(n, e.seq), state.seen);
    paint('#composer-wrap', composerHtml());
    syncComposer();
    // A redraw of the viewer (a pin added, a row marked) must not lose the person's place in a long sheet.
    const places = ['.files-body', '.sheet', '.shot', '.rail'].map((sel) => [sel, $(sel)?.scrollTop, $(sel)?.scrollLeft]);
    const drawn = paint('#files', filesHtml());
    if (drawn) for (const [sel, top, left] of places) $(sel)?.scrollTo(left || 0, top || 0);
    applyZoom(drawn);
    revealMarks($('#files'));
    tick();
    loadOpenScript();
  } else if (starting) {
    paint('#main', '<div class="page"><p class="empty">Loading</p></div>');
  } else {
    const pages = {
      run: setupPage, playbooks: playbooksPage, playbook: playbookPage, clients: clientsPage, client: clientPage,
      settings: settingsPage, log: logPage,
    }; // prettier-ignore
    paint('#main', noticesHtml() + pages[state.view]());
    syncPlaybook();
    syncClient();
  }
  applyWidths(); // the dividers' kept widths, fitted to the window
  paint('#pop-root', popHtml());
  syncPop();
  syncReply();
  syncVoice(); // the recording clock, and the box it writes into locked while it listens
  loadLinks(render); // cells and files named in what was just drawn become links once the server has checked them

  const el = focused && $('#' + focused);
  if (el && el !== document.activeElement) {
    el.focus();
    if (typeof selection === 'number' && ['textarea', 'search', 'text'].includes(el.type)) el.setSelectionRange(selection, selection);
  }
  syncAddress();
}

function detail(event) {
  $('#details').innerHTML = `
    <div class="dialog-head"><h2>${esc(event.name || 'Event')}</h2><button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
    <pre>${esc(JSON.stringify(event, null, 2))}</pre>`;
  $('#details').showModal();
}

// --- Actions ---------------------------------------------------------------

// Save sits at the top of a long page, so a refused value is brought into view and its field focused, once the
// page has been drawn with the message beside it.
function showSettingError() {
  setTimeout(() => {
    const name = Object.keys(state.settingsErrors || {})[0];
    const field = name && ($(`#setting-${name}`) || $(`#setting-${name}-error`));
    if (!field) return;
    field.scrollIntoView({ block: 'center' });
    field.focus?.({ preventScroll: true });
  });
}

// An action opened in full, or closed again.
function openAct(el) {
  const seq = Number(el.dataset.act);
  state.act = state.act === seq ? null : seq;
  state.actCode = false;
  render();
}

// The open action's code and what it printed, fetched after each drawing until they are in. A script still being
// saved by a run that is going (its name comes a moment after it finishes), or a failed request, is asked for again a
// few seconds later; only a finished run's 404 settles it as not kept.
const RETRY_MS = 3000;
async function loadOpenScript() {
  const el = $('.step.act.on');
  const { scriptRun: run, scriptName: name, scriptIndex: index, scriptOf: of } = el?.dataset || {};
  if (!run) return;
  const job = state.job.id;
  const key = scriptKey(job, { run, name, index, of });
  const had = state.scripts[key];
  if (had && (had.code !== undefined || had.final || had.pending || Date.now() - had.at < RETRY_MS)) return;
  state.scripts[key] = { ...had, pending: true };
  const query = new URLSearchParams(name ? { run, name } : { run, index, of });
  try {
    state.scripts[key] = await api(jobUrl(`/script?${query}`));
  } catch (e) {
    const gone = e.status === 404 && !isActive();
    const error = gone ? 'The code of this step was not kept.' : e.status === 404 ? 'Not ready yet.' : `It could not be loaded: ${e.message}`;
    state.scripts[key] = { error, final: gone, at: Date.now() };
    if (!gone) setTimeout(() => state.job?.id === job && render(), RETRY_MS);
  }
  if (state.job?.id === job) render();
}

async function refreshSettings() {
  try {
    state.settings = await api('/api/settings');
    state.settingsDraft = { ...state.settings.values };
    state.settingsErrors = {};
    state.settingsError = '';
  } catch (e) {
    state.settingsError = `Settings could not be loaded: ${e.message}`;
  }
}

async function refreshConfig() {
  state.config = await api('/api/config');
  state.playbook = currentPlaybook()?.id || '';
}

function leaveRun() {
  closeFile();
  Object.assign(state, { events: [], seen: 0, map: null, mapKey: '', mapFilter: 'all', showFiles: false, search: '', filter: 'all' });
  Object.assign(state, { fileQuery: '', openFolders: new Set(), checksOpen: false });
  Object.assign(state, { commentDraft: null, rounds: [], roundsKey: '', versions: [], versionsKey: '', showVersions: false });
  Object.assign(state, { reply: null, activeComment: '', showResolved: false, menu: null });
  Object.assign(state.changes, { since: null, step: 0, jump: false }); // version numbers belong to one run
  state.toggled.clear();
  Object.assign(state, { act: null, actCode: false, scripts: {}, planOpen: null });
}

let loads = 0; // each run opened counts one; a load another page or run has overtaken is dropped

async function loadJob(id) {
  const ticket = ++loads;
  const from = state.view;
  state.loading = true;
  render(); // the page on show fades while the run loads, so a click is seen to have done something
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    const events = await api(`/api/jobs/${encodeURIComponent(id)}/events?after=0`);
    if (ticket !== loads || state.view !== from) return; // the person went elsewhere meanwhile
    leaveRun();
    Object.assign(state, { job, events, view: 'run', notice: '' });
    state.seen = events.reduce((n, e) => Math.max(n, e.seq), 0); // an opened run appears whole, without animating in
    await Promise.all([loadMap(), loadRounds(), loadVersions()]);
  } finally {
    if (ticket === loads) state.loading = false;
    render(); // also after a failure, so the page does not stay faded
  }
}

function newRun() {
  leaveRun();
  Object.assign(state, { job: null, files: [], template: null, view: 'run', notice: '' });
  Object.assign(state, { runClient: '', clientSuggestion: null, clientDecided: false });
}

async function uploadAndStart() {
  if (!state.job) {
    const form = new FormData();
    // The server copies the playbook's instructions and skills into the run; only its id is sent.
    form.append('playbook_id', currentPlaybook().id);
    if (state.period) form.append('period', state.period);
    form.append('instructions', state.instructions[currentPlaybook().id] || '');
    if (state.runClient) form.append('client_id', state.runClient); // only a client the person chose
    for (const f of state.files) form.append('files', f, f.webkitRelativePath || f.name);
    if (state.template) form.append('template', state.template, state.template.name);
    const job = await api('/api/jobs', { method: 'POST', body: form });
    leaveRun();
    Object.assign(state, { job, files: [], template: null, runClient: '', clientSuggestion: null, clientDecided: false });
  }
  if (state.config?.configured) await api(jobUrl('/start'), { method: 'POST' });
  else toast('Files uploaded. Add OPENAI_API_KEY to .env and restart to run.');
  state.job = await api(jobUrl());
  state.jobs = await api('/api/jobs');
}

// --- Page addresses: Back, Forward and Refresh go by them (routes.js) ---------------------------------------------

let restoring = true; // until the page the app was opened at is on screen, and while going to an address
let starting = true; // until then, the page says Loading rather than showing New run
let replaceNext = false; // the next page takes the place of this one in history (a run just deleted)

// A new page on screen gets its own entry in the browser's history; `depth` counts the app's own entries, so a back
// arrow can tell whether there is a page of this app to go back to.
function syncAddress() {
  if (restoring) return;
  const want = pageAddress();
  if (!want || location.hash === want) return;
  if (replaceNext) history.replaceState(history.state, '', want);
  else history.pushState({ depth: (history.state?.depth || 0) + 1 }, '', want);
  replaceNext = false;
}

async function goTo({ view, id, version }) {
  if (view === 'run' || view === 'log') {
    if (state.job?.id !== id) await loadJob(id);
    state.view = view;
  } else if (view === 'playbook') await openPlaybook(id, { version });
  else if (view === 'client') await openClient(id, { keepTab: true, version });
  else if (view === 'new') newRun();
  else await openView(view);
}

// Opens the page the address names. One that is gone (a deleted run or playbook) opens its list, or New run.
async function followAddress(first = false) {
  restoring = true;
  const page = parseAddress(location.hash);
  try {
    await goTo(page);
  } catch (error) {
    const list = { playbook: 'playbooks', client: 'clients' }[page.view];
    if (list) await openView(list).catch(() => newRun());
    else newRun();
    toast(page.view === 'run' || page.view === 'log' ? 'That run could not be opened. It may have been deleted.' : error.message);
  } finally {
    // The address now names the page on show, without adding a step to Back.
    if (first || pageAddress() !== location.hash) history.replaceState({ ...history.state, depth: history.state?.depth || 0 }, '', pageAddress() || '#/new');
    restoring = starting = false;
    render();
  }
}
addEventListener('popstate', () => followAddress());

// A back arrow goes to the page before, as the browser's Back does; opened straight at this page, to its list.
async function goBack(list) {
  if (history.state?.depth > 0) history.back();
  else await openView(list);
}

// Actions that only change the screen; they run instantly.
const localActions = {
  'start-over': () => confirmStartOver(state.job.id),
  new: newRun,
  'more-options': () => (state.moreOptions = !state.moreOptions),
  'no-template': () => (state.template = null),
  run: () => (state.view = 'run'),
  log: () => (state.view = 'log'),
  folder: () => $('#folder-picker').click(),
  files: () => $('#files-picker').click(),
  template: () => {
    // A run can build to a written format (.json) instead of filling a template.
    $('#template-picker').accept = state.deliverable === 'format' ? '.json' : '.xlsx';
    $('#template-picker').click();
  },
  'toggle-files': () => (state.showFiles = !state.showFiles),
  'toggle-rail': () => (state.rail = !state.rail),
  'toggle-checks': () => (state.checksOpen = !state.checksOpen),
  'allow-notify': () => askPermission(render),
  'close-file': closeFile,
  'skill-pick': () => $('#skill-picker').click(),
  attach: () => $('#attach-picker').click(),
  'wrap-up': () => state.wrapped.add(`${state.job.id}:${nearLimits()[0]?.seq}`),
  'show-versions': () => (state.showVersions = true),
  'hide-versions': () => (state.showVersions = false),
  ...playbookDialogs,
  ...clientDialogs,
  ...offerLocal,
};
localActions['back-file'] = () => backFile(render);

// Actions that call the server; buttons are held while they run.
const serverActions = {
  ...offerServer,
  'refresh-config': refreshConfig,
  connection: async () => {
    const result = await api('/api/connection-check', { method: 'POST' });
    toast(`Connected to ${result.model}.`);
    await refreshConfig();
  },
  'save-settings': async () => {
    // A wrong value is named beside its field, here before sending and from the server's answer after.
    const errors = {};
    for (const name of Object.keys(state.settingsDraft)) {
      const problem = settingProblem(name, state.settingsDraft[name]);
      if (problem) errors[name] = problem;
    }
    state.settingsErrors = errors;
    if (Object.keys(errors).length) return showSettingError();
    try {
      state.settings = await api('/api/settings', { method: 'PUT', body: JSON.stringify({ values: settingsValues() }) });
    } catch (error) {
      if (!error.field) throw error;
      state.settingsErrors = { [error.field]: error.message };
      return showSettingError();
    }
    state.settingsDraft = { ...state.settings.values };
    await refreshConfig(); // the run screen names the More steps amount from it
    toast('Settings saved.');
  },
  ...playbookActions,
  ...clientActions,
  'send-comments': async () => {
    await sendComments();
    await Promise.all([refreshJob(), refreshConfig(), refreshClients()]); // a remembered comment adds a version
  },
  start: uploadAndStart,
  interrupt: async () => {
    await api(jobUrl('/interrupt'), { method: 'POST' });
    await refreshJob();
  },
  'more-steps': async () => {
    await api(jobUrl('/more-steps'), { method: 'POST' });
    await refreshJob();
  },
  export: () => download(jobUrl('/logs'), `${state.job.id}.jsonl`),
  // A stopped run carries on from a kept version, or starts over; either way in a fresh sandbox.
  continue: async (number) => {
    await continueFrom(number);
    state.showVersions = false;
    await refreshJob();
    // Continued from the older version on show: it is now the latest.
    if (state.preview?.version) openFile(state.preview.path, render, { sheet: state.preview.data?.sheet, back: state.preview.back });
  },
  'send-reply': async () => {
    await sendReply();
    await refreshJob();
  },
};
for (const name of ['pause', 'resume', 'cancel']) {
  serverActions[name] = async () => {
    await api(jobUrl('/' + name), { method: 'POST' });
    await refreshJob();
  };
}

async function action(name, arg) {
  if (localActions[name]) {
    localActions[name]();
    if (!['folder', 'files', 'template', 'skill-pick', 'attach'].includes(name)) render();
    return;
  }
  state.busy = true;
  state.notice = '';
  render();
  try {
    await serverActions[name](arg);
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
  if (view === 'settings') await refreshSettings();
  else if (view === 'playbooks') await refreshConfig();
  else if (view === 'clients') [state.jobs] = await Promise.all([api('/api/jobs'), refreshClients()]);
  else return;
  render();
}

// Mark a row of an output sheet and fill in its sources in place: redrawing the sheet would lose the scroll position.
function markRow(tr) {
  state.row = Number(tr.dataset.row);
  $('.grid tr.sel')?.classList.remove('sel');
  tr.classList.add('sel');
  showProof(true);
  locateRow(showProof, placeFound); // fills in the page of each PDF source as the server finds it
}

// Redraw the row's sources in place. `done` also brings the panel's record of its drawing up to date, once, so the
// next redraw does not start over; doing it on every answer would rebuild the whole sheet each time.
function showProof(done) {
  const proof = $('#proof');
  if (proof) proof.innerHTML = proofHtml();
  if (done && proof) $('#files').__html = filesHtml();
}

// A source opened before the server found the row in it: outline what it found, at its page.
function placeFound(path, place) {
  const view = state.preview;
  if (!applyPlace(view, path, place)) return;
  if (place.page && place.page !== view.page) turnPage(place.page, render);
  else render();
}

// The line of a text file a click landed on, as the server wants a comment's spot described. Cells and places on a
// page are picked by pointer (see "Picking in the preview").
function spotAt(e, target) {
  const line = target('[data-line]');
  return line ? { path: state.preview.path, line: Number(line.dataset.line) } : null;
}

// Show an output as a kept version holds it, or the latest ('latest'). Comparing starts again from the plain file.
async function pickVersion(value) {
  const view = state.preview;
  if (!view) return;
  Object.assign(state.changes, { on: false, since: null, step: 0 });
  state.reply = null;
  await openFile(view.path, render, { sheet: view.data?.sheet || null, version: value === 'latest' ? null : Number(value), back: view.back });
}

// Open a sent comment's spot, from its row in the chat or in the file panel's list.
function openComment(key) {
  const [round, n] = key.split('.').map(Number);
  const sent = state.events.find((e) => e.name === 'feedback.sent' && e.round === round)?.comments?.find((c) => c.n === n);
  const c = sentComment(key)?.c || sent;
  if (!c) return;
  state.activeComment = key;
  const spot = Object.fromEntries(['path', 'sheet', 'cell', 'line', 'page', 'x', 'y', 'w', 'h'].filter((k) => c[k] != null).map((k) => [k, c[k]]));
  return openSpot(spot, render);
}

// Answer the agent's question with one click: a choice it offered, or leaving the choice to it.
async function answerWith(text) {
  if (state.busy || jobStatus() !== 'waiting' || !state.job.question || state.job.answer) return;
  state.busy = true;
  render();
  try {
    await api(jobUrl('/answer'), { method: 'POST', body: JSON.stringify({ answer: text }) });
    state.drafts[state.job.id] = ''; // a half-typed answer is no longer wanted
    await refreshJob();
  } finally {
    state.busy = false;
    render();
  }
}

async function sendText() {
  const id = state.job.id;
  const text = (state.drafts[id] || '').trim();
  const answering = jobStatus() === 'waiting' && state.job.question;
  // An answer carries no files: those added before the question stay, to go with the next message.
  const files = answering ? [] : attached().slice();
  if (!text && !files.length) return;
  state.drafts[id] = '';
  if (!answering) state.attachments[id] = [];
  render();
  try {
    if (answering) await api(jobUrl('/answer'), { method: 'POST', body: JSON.stringify({ answer: text }) });
    else {
      // A message to a run that has stopped, a finished one included, starts it again.
      const form = new FormData();
      form.append('message', text);
      for (const f of files) form.append('files', f, f.name);
      await api(jobUrl('/message'), { method: 'POST', body: form });
    }
    await refreshJob();
    state.jobs = await api('/api/jobs');
  } catch (e) {
    state.drafts[id] = text; // keep what they typed and added
    if (!answering) state.attachments[id] = files;
    throw e;
  } finally {
    render();
  }
}

// --- Event wiring ----------------------------------------------------------

document.addEventListener('click', async (e) => {
  const target = (selector) => e.target.closest(selector);
  let el;
  // A dropdown closes on any click outside it.
  if (state.menu && !target('.menu-wrap')) {
    state.menu = null;
    render();
  }
  try {
    let spot;
    if (target('[data-close]')) $('#details').close();
    else if ((el = target('[data-mic]'))) await toggleVoice(el.dataset.mic);
    else if (target('[data-voice-dismiss]')) dismissVoiceNote();
    else if ((el = target('[data-menu]'))) {
      state.menu = state.menu === el.dataset.menu ? null : el.dataset.menu;
      render();
    } else if ((el = target('[data-run-rename]')) || (el = target('[data-run-delete]'))) {
      state.menu = null;
      render();
      if (el.dataset.runRename) renameRun(el.dataset.runRename);
      else deleteRun(el.dataset.runDelete);
    } else if ((el = target('[data-pick-version]'))) await pickVersion(el.dataset.pickVersion);
    else if ((el = target('[data-compare]'))) {
      Object.assign(state.changes, { since: el.dataset.compare ? Number(el.dataset.compare) : null, step: 0, jump: true });
      state.menu = null;
      render();
      await loadChanges(state.preview, render);
    } else if ((el = target('[data-change-step]'))) {
      stepChange(Number(el.dataset.changeStep));
      render();
    } else if ((el = target('[data-goto]'))) await openComment(el.dataset.goto);
    else if ((el = target('[data-link]'))) {
      const { link: path, linkSheet: sheet, linkCell: cell } = el.dataset;
      await openSpot({ path, ...(sheet ? { sheet } : {}), ...(cell ? { cell } : {}) }, render);
    } else if ((el = target('[data-reply]'))) {
      state.reply = { to: el.dataset.reply, text: '', round_id: null };
      state.activeComment = el.dataset.reply;
      render();
      $('#reply-text')?.focus();
    } else if (target('[data-reply-cancel]')) {
      state.reply = null;
      render();
    } else if ((el = target('[data-resolve]')) || (el = target('[data-reopen]'))) {
      await resolveComment(el.dataset.resolve || el.dataset.reopen, !!el.dataset.resolve);
      render();
    } else if (target('[data-show-resolved]')) {
      state.showResolved = !state.showResolved;
      render();
    }
    else if ((el = target('[data-comment]'))) {
      commentAction(el.dataset.comment);
      render();
    } else if ((el = target('[data-remember]'))) {
      chooseRemember(el.dataset.remember);
      render();
    } else if (target('#pop-root')) return;
    else if ((el = target('[data-pin]'))) {
      editPin(el.dataset.pin, e.clientX, e.clientY);
      render();
    } else if ((el = target('[data-drop-comment]'))) {
      dropComment(el.dataset.dropComment);
      render();
    } else if ((el = target('[data-forget]'))) {
      try {
        await forget(Number(el.dataset.forget), el.dataset.scope);
      } finally {
        await Promise.all([refreshConfig(), refreshClients()]); // refused or not, show what the latest version now is
        render();
      }
    } else if ((el = target('[data-zoom]'))) {
      if (zoomAction(el.dataset.zoom)) render();
    } else if ((el = target('[data-unattach]'))) {
      attached().splice(Number(el.dataset.unattach), 1);
      render();
    } else if ((el = target('[data-action]'))) await action(el.dataset.action);
    else if ((el = target('[data-continue]'))) await action('continue', Number(el.dataset.continue));
    else if (await playbookClick(target, render)) return;
    else if (await clientClick(target, render)) return;
    else if ((el = target('[data-use-client]'))) {
      chooseRunClient(el.dataset.useClient);
      render();
    }
    else if ((el = target('[data-mode]'))) setMode(el.dataset.mode);
    else if (target('[data-find]')) {
      if (state.find) closeFind();
      else openFind();
      render();
      $('#find-q')?.focus();
    } else if (target('[data-find-close]')) {
      closeFind();
      render();
    }
    else if ((el = target('[data-find-step]'))) await stepFind(Number(el.dataset.findStep), turnPage, render);
    else if ((el = target('[data-changes]'))) {
      const name = el.dataset.changes;
      state.changes[name] = !state.changes[name];
      if (name === 'on') Object.assign(state.changes, { step: 0, jump: state.changes.on });
      render();
      if (name === 'on') await loadChanges(state.preview, render);
    }
    else if (commenting() && target('[data-line]') && (spot = spotAt(e, target))) {
      // In Comment mode a click on a line starts a comment there; cells and pages are picked by pointer below.
      writeAt(spot, e.clientX, e.clientY);
      render();
    }
    else if ((el = target('[data-answer]'))) await answerWith(el.dataset.answer);
    else if ((el = target('[data-back]'))) await goBack(el.dataset.back);
    else if ((el = target('[data-view]'))) await openView(el.dataset.view);
    else if ((el = target('[data-job]'))) await loadJob(el.dataset.job);
    else if ((el = target('[data-download]'))) {
      const version = el.dataset.fileVersion ? `&version=${el.dataset.fileVersion}` : ''; // an older version on show
      await download(fileUrl('file', el.dataset.download, version), baseName(el.dataset.download));
    }
    else if ((el = target('[data-open]'))) {
      const from = target('.proof') && state.preview;
      const back = from ? { path: from.path, sheet: from.data?.sheet, row: state.row } : null;
      // A source opened from a row opens where the row's values are, with them outlined, at the spot of the value
      // the link stands for when it is known.
      const path = el.dataset.open;
      const found = from ? foundFor(path) : null;
      const spot = el.dataset.spot ? JSON.parse(el.dataset.spot) : {};
      const ring = spot.cell || spot.line ? { path, ...spot } : null;
      await openFile(path, render, { back, found, ring, sheet: spot.sheet || null, page: spot.page || found?.page || 1 });
    } else if ((el = target('[data-sheet]'))) await openFile(state.open, render, { sheet: el.dataset.sheet, back: state.preview?.back, version: state.preview?.version });
    else if ((el = target('[data-page]'))) await turnPage(Number(el.dataset.page), render);
    else if ((el = target('tr[data-row]'))) markRow(el);
    else if ((el = target('[data-group]'))) {
      const id = Number(el.dataset.group);
      state.toggled.has(id) ? state.toggled.delete(id) : state.toggled.add(id);
      render();
    } else if ((el = target('[data-act]'))) openAct(el);
    else if ((el = target('[data-act-code]'))) {
      state.actCode = true;
      render();
    } else if ((el = target('[data-plan]'))) {
      state.planOpen = el.getAttribute('aria-expanded') !== 'true';
      render();
    } else if ((el = target('[data-segment]'))) {
      const { segment: name, value } = el.dataset;
      if (name === 'deliverable') {
        state.deliverable = value;
        state.template = null;
      } else state.settingsDraft[name] = value;
      render();
    } else if ((el = target('[data-toggle]'))) {
      const name = el.dataset.toggle;
      const [on, off] = state.settings.options[name].choices; // a two-way choice shown as a switch
      const v = state.settingsDraft;
      v[name] = on === undefined ? !v[name] : v[name] === on ? off : on;
      render();
      if (name === 'notify' && v[name]) await askPermission(render); // the browser asks once, on this click
    } else if ((el = target('[data-step]'))) {
      const { step: name, delta } = el.dataset;
      const option = state.settings.options[name];
      state.settingsDraft[name] = Math.min(option.high, Math.max(option.low, state.settingsDraft[name] + Number(delta)));
      render();
    } else if ((el = target('[data-fold]'))) {
      const name = el.dataset.fold;
      state.openFolders.has(name) ? state.openFolders.delete(name) : state.openFolders.add(name);
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
  // Cmd/Ctrl+F searches the file on show, in place of the browser's own search of the whole page.
  if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key?.toLowerCase() === 'f' && state.view === 'run' && canFind(state.preview)) {
    e.preventDefault();
    openFind();
    render();
    $('#find-q')?.select();
    return;
  }
  if (e.target?.id === 'find-q' && (e.key === 'Enter' || e.key === 'Escape')) {
    e.preventDefault();
    if (e.key === 'Escape') {
      closeFind();
      return render();
    }
    return stepFind(e.shiftKey ? -1 : 1, turnPage, render);
  }
  if (e.key === 'Escape' && state.menu) {
    const was = state.menu;
    state.menu = null;
    render();
    return $(`[data-menu="${was}"]`)?.focus(); // back to the button that opened it
  }
  if (e.key === 'Escape' && e.target?.id === 'reply-text') {
    state.reply = null;
    return render();
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.target?.id === 'reply-text') {
    e.preventDefault();
    return $('#reply-form').requestSubmit();
  }
  if (e.key === 'Escape' && (state.commentDraft || drag)) {
    dropPick();
    state.commentDraft = null;
    return render();
  }
  if (e.key === 'Escape' && e.target?.id === 'files-q' && state.fileQuery) {
    state.fileQuery = ''; // Esc in the find box clears it first
    return render();
  }
  if (e.key === 'Escape' && state.full && state.view === 'run' && !$('#details').open) {
    state.full = false;
    return render();
  }
  // Esc on an opened version of a playbook's or client's History goes back to the list, as Back does.
  const rec = { playbook: state.book, client: state.client }[state.view];
  if (e.key === 'Escape' && rec?.tab === 'history' && rec.open && !$('#details').open) return void leaveVersion(rec, render);
  // Ctrl/Cmd with +, - or 0 zooms the file on show, in place of the browser's zoom of the whole page.
  const zoomKey = { '=': 'in', '+': 'in', '-': 'out', 0: 'reset' }[e.key];
  if (zoomKey && (e.metaKey || e.ctrlKey) && !e.altKey && state.view === 'run' && state.open && canZoom()) {
    e.preventDefault();
    if (zoomAction(zoomKey)) render();
    return;
  }
  // C flips the preview between View and Comment, unless the person is typing.
  const writing = e.target instanceof Element && e.target.matches('input, textarea, select, [contenteditable]');
  if (e.key?.toLowerCase() === 'c' && !writing && !e.metaKey && !e.ctrlKey && !e.altKey && $('.comment-btn') && !state.commentDraft) {
    return setMode(state.previewMode === 'view' ? 'comment' : 'view');
  }
  if (!(e.target instanceof Element)) return;
  if (e.key === 'Enter' && e.target.matches('tr[data-event], tr[data-row]')) e.target.click();
  // Enter sends a comment or a request to the agent; Shift+Enter starts a new line in a comment.
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.target.id === 'note') {
    e.preventDefault();
    $('#comment-form').requestSubmit();
  }
  // While the agent's question is open, a number picks that choice; the next number goes to the answer box.
  const picks = [...document.querySelectorAll('.question .opt')];
  const typing = e.target.matches('input, textarea, select') || $('#details').open || state.commentDraft || e.metaKey || e.ctrlKey || e.altKey;
  if (picks.length && /^[1-9]$/.test(e.key) && !typing) {
    const n = Number(e.key);
    if (n <= picks.length) picks[n - 1].click();
    else if (n === picks.length + 1) {
      e.preventDefault();
      $('#msg')?.focus();
    }
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.target.id === 'msg') {
    e.preventDefault();
    $('#composer').requestSubmit();
  }
});

// --- Picking in the preview ---------------------------------------------------
// In Comment mode a press on a cell starts a block that follows the pointer (shift-click extends the last one), and a
// press on a page or picture draws a box; letting go opens the comment box for what was picked. A press that does
// not move picks one cell, or a point on the page. While the pointer moves, only the picked cells' classes or the
// box's size change: redrawing a 500-row sheet on every move would lag behind the pointer.

function setMode(mode) {
  if (state.commentDraft) commentAction('away'); // a comment being typed is kept, as a click elsewhere keeps it
  state.previewMode = mode === 'comment' ? 'comment' : 'view';
  dropPick();
  render();
}

let drag = null;
let anchor = null; // the cell a shift-click block starts from, on this file and sheet
const EDGES = ['sel', 't', 'b', 'l', 'r'];

// The block from one cell to another, as C3 or C3:E5, or null when it would be larger than a comment can cover.
function block(from, to) {
  const cells = blockCells(`${from}:${to}`);
  if (cells.length > MAX_BLOCK) return null;
  const [[r1, c1], [r2, c2]] = [cells[0], cells[cells.length - 1]];
  const name = (r, c) => `${columnName(c)}${r}`;
  return cells.length === 1 ? name(r1, c1) : `${name(r1, c1)}:${name(r2, c2)}`;
}

function showBlock(cell) {
  for (const td of drag.marked) td.classList.remove(...EDGES);
  const cells = blockCells(cell);
  const [[top, left], [bottom, right]] = [cells[0], cells[cells.length - 1]];
  drag.marked = cells.map(([r, c]) => drag.tds.get(`${columnName(c)}${r}`)).filter(Boolean);
  for (const td of drag.marked) {
    const [, letters, row] = /^([A-Z]+)(\d+)$/.exec(td.dataset.cell);
    const r = Number(row);
    const c = blockCells(`${letters}1`)[0][1];
    td.classList.add('sel', ...[r === top && 't', r === bottom && 'b', c === left && 'l', c === right && 'r'].filter(Boolean));
  }
}

function showBox(pick) {
  Object.assign(drag.area.style, { left: `${pick.x * 100}%`, top: `${pick.y * 100}%`, width: `${pick.w * 100}%`, height: `${pick.h * 100}%` });
}

function stageShare(e, stage) {
  const box = stage.querySelector('img').getBoundingClientRect();
  const share = (at, from, size) => Math.min(1, Math.max(0, (at - from) / size));
  return [share(e.clientX, box.left, box.width), share(e.clientY, box.top, box.height)];
}

// Take the drag's own marks off the page: they were drawn straight onto it, not by a redraw.
function endDrag() {
  for (const td of drag.marked || []) td.classList.remove(...EDGES);
  drag.area?.remove();
  const ended = drag;
  drag = null;
  return ended;
}

function dropPick() {
  if (drag) endDrag();
}

document.addEventListener('pointerdown', (e) => {
  if (e.button !== 0 || !commenting() || state.commentDraft || !state.preview?.data || e.target.closest('.pin')) return;
  const view = state.preview;
  const td = e.target.closest('td[data-cell]');
  const stage = !td && e.target.closest('[data-stage]');
  if (!td && !stage) return;
  e.preventDefault(); // no text selection while dragging
  try {
    e.target.setPointerCapture(e.pointerId); // the drag ends here even if the pointer leaves the window
  } catch {
    // a pointer the browser no longer tracks: the drag still works while it stays over the preview
  }
  drag = { pointer: e.pointerId, view };
  if (td) {
    const here = td.dataset.cell;
    const place = `${view.path}!${view.data.sheet || ''}`;
    const from = e.shiftKey && anchor?.place === place && block(anchor.cell, here) ? anchor.cell : here;
    anchor = { place, cell: from };
    Object.assign(drag, { kind: 'cells', from, marked: [], tds: new Map([...document.querySelectorAll('td[data-cell]')].map((el) => [el.dataset.cell, el])) });
    drag.pick = { cell: block(from, here) };
    showBlock(drag.pick.cell);
  } else {
    const [x, y] = stageShare(e, stage);
    Object.assign(drag, { kind: 'box', x, y, stage, area: document.createElement('span') });
    drag.area.className = 'area live';
    stage.append(drag.area);
    drag.pick = { x, y, w: 0, h: 0 };
    showBox(drag.pick);
  }
});

document.addEventListener('pointermove', (e) => {
  if (!drag || e.pointerId !== drag.pointer) return;
  if (drag.kind === 'cells') {
    const td = document.elementFromPoint(e.clientX, e.clientY)?.closest('td[data-cell]');
    const cell = td && block(drag.from, td.dataset.cell); // past the size limit the block stops growing
    if (cell && cell !== drag.pick.cell) showBlock((drag.pick = { cell }).cell);
    return;
  }
  const [x, y] = stageShare(e, drag.stage);
  drag.pick = { x: Math.min(drag.x, x), y: Math.min(drag.y, y), w: Math.abs(x - drag.x), h: Math.abs(y - drag.y) };
  showBox(drag.pick);
});

document.addEventListener('pointerup', (e) => {
  if (!drag || e.pointerId !== drag.pointer) return;
  const { view, kind, pick } = endDrag();
  if (state.preview !== view) return render(); // the file changed under the drag
  const round = (v) => Math.round(v * 10000) / 10000;
  let spot;
  if (kind === 'cells') spot = { path: view.path, cell: pick.cell, ...(view.data.sheet ? { sheet: view.data.sheet } : {}) };
  else {
    const box = pick.w > 0.01 || pick.h > 0.01; // a press that barely moved is a point, as a click always was
    spot = { path: view.path, x: round(pick.x), y: round(pick.y), ...(box ? { w: round(pick.w), h: round(pick.h) } : {}) };
    if (view.data.kind === 'pages') spot.page = view.page;
  }
  writeAt(spot, e.clientX, e.clientY);
  render();
});

// A drag the browser takes over (a touch scroll, a context menu) leaves nothing picked behind.
document.addEventListener('pointercancel', dropPick);
document.addEventListener('contextmenu', dropPick);

let searchTimer;
// Full screen is offered only on a wide window. A window made narrow while in full screen leaves it, so the panel
// gets its Close and Back again.
const narrow = matchMedia('(max-width: 1000px)');
function leaveFullWhenNarrow() {
  if (narrow.matches && state.full) {
    state.full = false;
    render();
  }
}
narrow.addEventListener('change', leaveFullWhenNarrow);
addEventListener('resize', leaveFullWhenNarrow);
addEventListener('resize', placePop); // the comment box keeps beside its spot, or becomes a sheet below 560 wide

document.addEventListener('input', (e) => {
  const { id, value } = e.target;
  if (id === 'msg') {
    state.drafts[state.job.id] = value;
    syncComposer();
  }
  if (id === 'run-instructions') {
    state.instructions[currentPlaybook()?.id] = value;
    const start = $('[data-action="start"]'); // a goal in words is enough to start, so the button follows the typing
    if (start) start.disabled = startBlocked();
  }
  if (id === 'note' && state.commentDraft) state.commentDraft.text = value;
  if (id === 'reply-text' && state.reply) {
    state.reply.text = value;
    syncReply();
  }
  if (id === 'find-q' && state.find) typed(value, turnPage, render);
  if (id === 'files-q') {
    state.fileQuery = value;
    render();
  }
  if (id === 'playbook-text') startDraft(state.book, value);
  if (id === 'client-notes') typeNotes(value);
  if (id === 'tune') state.book.ask = value;
  if (id === 'playbook-text' || id === 'tune') syncPlaybook();
  if (id === 'period') {
    state.period = value;
    $('[data-action="start"]').disabled = startBlocked();
    $('[data-action="more-options"]').style.display = value || state.template ? 'none' : ''; // nothing to fold away
  }
  if (e.target.dataset.setting) {
    state.settingsDraft[e.target.dataset.setting] = value;
    const save = $('[data-action="save-settings"]'); // Save is lit once something differs from what is saved
    if (save) save.disabled = state.busy || !settingsChanged();
  }
  if (id === 'log-search') {
    state.search = value;
    // Wait for a pause in typing before filtering the log.
    clearTimeout(searchTimer);
    searchTimer = setTimeout(render, 150);
  }
});

document.addEventListener('change', async (e) => {
  if (e.target.matches('[data-playbook]')) state.playbook = e.target.value;
  else if (e.target.matches('[data-run-client]')) chooseRunClient(e.target.value);
  else if (e.target.dataset.setting && state.settingsErrors) {
    // A number is checked once it has been typed, and its message shown beside it.
    const name = e.target.dataset.setting;
    const problem = settingProblem(name, e.target.value);
    if (problem) state.settingsErrors[name] = problem;
    else delete state.settingsErrors[name];
  }
  else if (e.target.id === 'skill-picker') {
    const file = e.target.files[0];
    e.target.value = '';
    if (file) await uploadSkill(file, render);
  }
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
    render();
    await suggestClient(state.files); // the names may point to a known client, offered beside the client box
  } else if (e.target.id === 'attach-picker') {
    const files = Array.from(e.target.files);
    e.target.value = '';
    return attach(files);
  } else if (e.target.id === 'template-picker') {
    state.template = e.target.files[0] || null;
    e.target.value = '';
  } else return;
  render();
});

document.addEventListener('submit', async (e) => {
  const form = e.target.id;
  if (!['composer', 'comment-form', 'tune-form', 'book-form', 'reply-form', 'run-form'].includes(form)) return;
  e.preventDefault();
  if (form === 'reply-form') return action('send-reply');
  try {
    if (form === 'composer') return await sendText();
    if (form === 'run-form') {
      const starting = e.target.dataset.does === 'start-over';
      if ((await submitRun(e.target)) === state.job?.id) {
        newRun(); // the run on show was deleted: New run takes its place in history, so Back does not reach it
        replaceNext = true;
      }
      if (starting) {
        state.showVersions = false;
        await refreshJob();
      }
      return render();
    }
    if (form === 'comment-form') keepComment();
    else if (form === 'book-form') (await submitClientDialog(e.target)) || (await submitDialog(e.target));
    else {
      const ask = state.book.ask.trim();
      if (!ask) return;
      const asking = askTune(ask);
      render(); // show the request and that the agent is working on it
      await asking;
    }
    if (form === 'book-form') await refreshConfig();
  } catch (err) {
    toast(err.message);
  }
  render();
});

// --- Zoom with the wheel, and files dropped on the conversation ----------------

// Ctrl/Cmd with the scroll wheel, or a pinch on a trackpad, zooms the file on show, not the page.
document.addEventListener(
  'wheel',
  (e) => {
    if (!(e.ctrlKey || e.metaKey) || !e.target.closest?.('#files .files-body') || !canZoom()) return;
    e.preventDefault();
    if (wheelZoom(e.deltaY)) render();
  },
  { passive: false },
);

let resizing;
window.addEventListener('resize', () => {
  clearTimeout(resizing);
  resizing = setTimeout(() => state.zoom.fit && applyZoom(), 100); // Fit follows the window
});

const canAttach = () => state.view === 'run' && !!$('#composer [data-action="attach"]:not(:disabled)');
let dragDepth = 0;
const dropShown = (on) => $('.chat')?.classList.toggle('dropping', on);
document.addEventListener('dragenter', (e) => {
  if (!e.dataTransfer?.types?.includes('Files') || !e.target.closest?.('.chat') || !canAttach()) return;
  dragDepth += 1;
  dropShown(true);
});
document.addEventListener('dragleave', (e) => {
  if (!e.target.closest?.('.chat')) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) dropShown(false);
});
document.addEventListener('dragover', (e) => {
  if (e.target.closest?.('.chat') && canAttach() && e.dataTransfer?.types?.includes('Files')) e.preventDefault();
});
document.addEventListener('drop', (e) => {
  if (!e.target.closest?.('.chat') || !canAttach()) return;
  e.preventDefault();
  dragDepth = 0;
  dropShown(false);
  attach(Array.from(e.dataTransfer.files || []));
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
  const fresh = events.filter((ev) => !seen.has(ev.seq));
  state.events.push(...fresh);
  // The agent added a lesson to the playbook: its new version is needed at once, to offer Undo beside the lesson.
  if (fresh.some((ev) => ev.name === 'lesson.saved')) [state.config] = await Promise.all([api('/api/config'), refreshClients()]);
  await Promise.all([loadMap(), loadRounds(), loadVersions()]);
  if (statusChanged) state.jobs = await api('/api/jobs');
}

let listedAt = 0; // when the run list was last fetched for other runs going meanwhile

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
  // Other runs going meanwhile are looked at too, so one that asks, finishes or stops can say so (alerts.js).
  try {
    const others = state.jobs?.some((j) => j.id !== state.job?.id && (inGroup('active', j.status) || j.status === 'waiting'));
    if (others && Date.now() - listedAt > 8000) {
      listedAt = Date.now(); // at most every 8 seconds, however often the open run is looked at
      state.jobs = await api('/api/jobs');
    }
    await noticeRuns(state.jobs, loadJob, render);
  } catch {
    // the next round looks again
  }
  try {
    await refreshPlaybookTab(render); // a check of past corrections fills in while its tab is open
  } catch {
    // the next round tries again
  }
  // A sent answer is picked up within seconds, so look again soon rather than leave the answered card showing.
  setTimeout(poll, isActive() || (jobStatus() === 'waiting' && state.job?.answer) ? 1500 : state.book?.tests?.running ? 3000 : 8000);
}

initVoice(render);
// Back on the tab: the mark a run left on it clears.
document.addEventListener('visibilitychange', () => clearMark(render));
addEventListener('focus', () => clearMark(render));
// The microphone button takes the focus on a click, so the cursor's place in the box is noted first.
document.addEventListener('pointerdown', (e) => {
  const mic = e.target.closest?.('[data-mic]');
  if (mic) rememberCaret(mic.dataset.mic);
}, true); // prettier-ignore
// Ctrl+M in a box with a microphone starts or stops speaking into it.
document.addEventListener('keydown', (e) => {
  if (!e.ctrlKey || e.metaKey || e.altKey || e.key.toLowerCase() !== 'm' || !e.target.id) return;
  if (!document.getElementById(`mic-${e.target.id}`)) return;
  e.preventDefault();
  e.stopPropagation();
  toggleVoice(e.target.id);
}, true); // prettier-ignore
render();
setInterval(tick, 1000);
(async () => {
  try {
    await Promise.all([refreshConfig(), refreshSettings(), refreshClients()]); // refreshSettings keeps its own error for its page
    state.jobs = await api('/api/jobs');
  } catch (e) {
    state.notice = e.message;
  }
  await followAddress(true); // the page the address names: after a Refresh, the one that was on show
  poll();
})();
