// Draws the app and handles what the person does. State and helpers live in core.js; the screens in thread.js,
// files.js and pages.js.

import {
  $, api, attr, baseName, btn, currentPlaybook, download, duration, esc, fileUrl, icon, isActive, jobStatus, jobTitle,
  jobUrl, state, toast,
} from './core.js'; // prettier-ignore
import {
  MAX_BLOCK, blockCells, columnName, commentAction, commenting, dropComment, editPin, forget, keepComment, loadRounds,
  popHtml, sendComments, syncPop, writeAt,
} from './comments.js'; // prettier-ignore
import { backFile, closeFile, filesHtml, loadMap, openFile, proofHtml, turnPage } from './files.js';
import { logPage, settingsPage, setupPage, startBlocked } from './pages.js';
import {
  applyPlace, canFind, closeFind, foundFor, loadChanges, locateRow, openFind, revealMarks, stepFind, typed,
} from './review.js'; // prettier-ignore
import {
  askTune, playbookActions, playbookClick, playbookDialogs, playbookPage, playbooksPage, refreshPlaybookTab, startDraft,
  submitDialog, syncPlaybook, uploadSkill,
} from './playbooks.js'; // prettier-ignore
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
  const here = state.view === 'playbook' ? 'playbooks' : state.view;
  const link = (view, label) => `<button class="item ${here === view ? 'on' : ''}" data-view="${view}"><span>${label}</span></button>`;
  return `
    <div class="brand">Document Intelligence</div>
    ${btn('new', 'New run', 'quiet add', false, 'plus')}
    <nav class="runs" aria-label="Recent runs"><h3>Recent</h3>${runItems().join('') || '<p class="empty item">No runs yet</p>'}</nav>
    <div class="foot">${link('playbooks', 'Playbooks')}${link('settings', 'Settings')}</div>`;
}

// Narrow screens swap the sidebar for one menu.
function topnavHtml() {
  const view = state.view === 'playbook' ? 'playbooks' : state.view;
  const here = view === 'run' ? (state.job ? 'job:' + state.job.id : 'new') : view;
  const option = (value, label) => `<option value="${esc(value)}" ${attr('selected', value === here)}>${esc(label)}</option>`;
  const runs = state.jobs.map((j) =>
    option('job:' + j.id, `${jobTitle(j)}, ${new Date(j.created_at).toLocaleDateString('en', { day: 'numeric', month: 'short' })}`),
  );
  return `<div class="brand">Document Intelligence</div>
    <select aria-label="Go to" data-nav>${option('new', 'New run')}${runs.join('')}${state.job ? option('log', 'Technical log') : ''}${option('playbooks', 'Playbooks')}${option('settings', 'Settings')}</select>`;
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
    // A redraw of the viewer (a pin added, a row marked) must not lose the person's place in a long sheet.
    const places = ['.files-body', '.sheet', '.shot'].map((sel) => [sel, $(sel)?.scrollTop, $(sel)?.scrollLeft]);
    if (paint('#files', filesHtml())) for (const [sel, top, left] of places) $(sel)?.scrollTo(left || 0, top || 0);
    revealMarks($('#files'));
    tick();
  } else {
    const pages = { run: setupPage, playbooks: playbooksPage, playbook: playbookPage, settings: settingsPage, log: logPage };
    paint('#main', noticesHtml() + pages[state.view]());
    syncPlaybook();
  }
  paint('#pop-root', popHtml());
  syncPop();

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
  state.playbook = currentPlaybook()?.id || '';
}

function leaveRun() {
  closeFile();
  Object.assign(state, { events: [], seen: 0, map: null, mapKey: '', mapFilter: 'all', showFiles: false, search: '', filter: 'all' });
  Object.assign(state, { commentDraft: null, rounds: [], roundsKey: '' });
  state.toggled.clear();
}

async function loadJob(id) {
  state.loading = true;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    const events = await api(`/api/jobs/${encodeURIComponent(id)}/events?after=0`);
    leaveRun();
    Object.assign(state, { job, events, view: 'run', notice: '' });
    state.seen = events.reduce((n, e) => Math.max(n, e.seq), 0); // an opened run appears whole, without animating in
    await Promise.all([loadMap(), loadRounds()]);
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
    // The server copies the playbook's instructions and skills into the run; only its id is sent.
    form.append('playbook_id', currentPlaybook().id);
    if (state.period) form.append('period', state.period);
    form.append('instructions', state.instructions[currentPlaybook().id] || '');
    for (const f of state.files) form.append('files', f, f.webkitRelativePath || f.name);
    if (state.template) form.append('template', state.template, state.template.name);
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
  'close-file': closeFile,
  'skill-pick': () => $('#skill-picker').click(),
  ...playbookDialogs,
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
  ...playbookActions,
  'send-comments': async () => {
    await sendComments();
    await Promise.all([refreshJob(), refreshConfig()]);
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
    if (!['folder', 'files', 'template', 'skill-pick'].includes(name)) render();
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
  if (view === 'settings') await refreshSettings();
  else if (view === 'playbooks') await refreshConfig();
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
    let spot;
    if (target('[data-close]')) $('#details').close();
    else if ((el = target('[data-comment]'))) {
      commentAction(el.dataset.comment);
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
        await forget(Number(el.dataset.forget));
      } finally {
        await refreshConfig(); // refused or not, show what the playbook's latest version now is
        render();
      }
    } else if ((el = target('[data-action]'))) await action(el.dataset.action);
    else if (await playbookClick(target, render)) return;
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
      render();
      if (name === 'on') await loadChanges(state.preview, render);
    }
    else if (commenting() && target('[data-line]') && (spot = spotAt(e, target))) {
      // In Comment mode a click on a line starts a comment there; cells and pages are picked by pointer below.
      writeAt(spot, e.clientX, e.clientY);
      render();
    }
    else if ((el = target('[data-answer]'))) await answerWith(el.dataset.answer);
    else if ((el = target('[data-view]'))) await openView(el.dataset.view);
    else if ((el = target('[data-job]'))) await loadJob(el.dataset.job);
    else if ((el = target('[data-download]'))) await download(fileUrl('file', el.dataset.download), baseName(el.dataset.download));
    else if ((el = target('[data-open]'))) {
      const from = target('.proof') && state.preview;
      const back = from ? { path: from.path, sheet: from.data?.sheet, row: state.row } : null;
      // A source opened from a row opens where the row's values are, with them outlined.
      const found = from ? foundFor(el.dataset.open) : null;
      await openFile(el.dataset.open, render, { back, found, page: found?.page || 1 });
    } else if ((el = target('[data-sheet]'))) await openFile(state.open, render, { sheet: el.dataset.sheet, back: state.preview?.back });
    else if ((el = target('[data-page]'))) await turnPage(Number(el.dataset.page), render);
    else if ((el = target('tr[data-row]'))) markRow(el);
    else if ((el = target('[data-group]'))) {
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
  if (e.key === 'Escape' && (state.commentDraft || drag)) {
    dropPick();
    state.commentDraft = null;
    return render();
  }
  // C flips the preview between View and Comment, unless the person is typing.
  const writing = e.target instanceof Element && e.target.matches('input, textarea, select, [contenteditable]');
  if (e.key?.toLowerCase() === 'c' && !writing && !e.metaKey && !e.ctrlKey && !e.altKey && $('.modes') && !state.commentDraft) {
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
  if (id === 'find-q' && state.find) typed(value, turnPage, render);
  if (id === 'playbook-text') startDraft(state.book, value);
  if (id === 'tune') state.book.ask = value;
  if (id === 'playbook-text' || id === 'tune') syncPlaybook();
  if (id === 'period') {
    state.period = value;
    $('[data-action="start"]').disabled = startBlocked();
    $('[data-action="more-options"]').style.display = value || state.template ? 'none' : ''; // nothing to fold away
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
  if (e.target.matches('[data-playbook]')) state.playbook = e.target.value;
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
  } else if (e.target.id === 'template-picker') {
    state.template = e.target.files[0] || null;
    e.target.value = '';
  } else return;
  render();
});

document.addEventListener('submit', async (e) => {
  const form = e.target.id;
  if (!['composer', 'comment-form', 'tune-form', 'book-form'].includes(form)) return;
  e.preventDefault();
  try {
    if (form === 'composer') return await sendText();
    if (form === 'comment-form') keepComment();
    else if (form === 'book-form') await submitDialog(e.target);
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
  if (fresh.some((ev) => ev.name === 'lesson.saved')) state.config = await api('/api/config');
  await Promise.all([loadMap(), loadRounds()]);
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
  try {
    await refreshPlaybookTab(render); // a check of past corrections fills in while its tab is open
  } catch {
    // the next round tries again
  }
  // A sent answer is picked up within seconds, so look again soon rather than leave the answered card showing.
  setTimeout(poll, isActive() || (jobStatus() === 'waiting' && state.job?.answer) ? 1500 : state.book?.tests?.running ? 3000 : 8000);
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
