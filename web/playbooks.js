// Playbooks: the list, and one playbook's instructions, skills and history.
// A playbook is what the agent follows on every run of one kind of work. Every change to its instructions is a
// version; the agent can propose a rewrite, and nothing is saved until the person accepts it.

import { $, api, attr, btn, esc, hideToast, icon, jobTitle, money, pill, plural, playbooks, state, toast } from './core.js';
import { pageAddress } from './routes.js';
import { micButton, voiceNote } from './voice.js';

const TABS = [
  ['instructions', 'Instructions'],
  ['skills', 'Skills'],
  ['tests', 'Past corrections'],
  ['activity', 'Activity'],
  ['history', 'History'],
];
const url = (rest = '') => `/api/playbooks/${encodeURIComponent(state.book.id)}${rest}`;
const page = (body) => `<div class="page"><div class="narrow">${body}</div></div>`;
export const day = (t) => new Date(t).toLocaleDateString('en', { day: 'numeric', month: 'short' });
const json = (body) => ({ method: 'POST', body: JSON.stringify(body) });

// --- The list ---------------------------------------------------------------

export function playbooksPage() {
  const rows = playbooks()
    .map(
      (p) => `<button class="row link" data-book="${esc(p.id)}" title="${esc(p.name)}"><span class="name"><span>${esc(p.name)}</span>
        <span class="desc">${plural(p.skills, 'skill')} · version ${p.version}</span></span>${icon('chev')}</button>`,
    )
    .join('');
  return page(`<div class="page-head"><h1>Playbooks</h1>${btn('new-playbook', 'New playbook', 'primary', false, 'plus')}</div>
    ${rows ? `<div class="rows">${rows}</div>` : '<p class="empty">No playbooks yet.</p>'}`);
}

// --- One playbook -----------------------------------------------------------

export async function openPlaybook(id, { version = null } = {}) {
  state.view = 'playbook';
  // An edit that was typed and not saved is kept for the visit, so looking at another page does not lose it.
  state.book = state.books[id] ||= { id, tab: 'instructions', data: null, draft: null, ask: '', skills: [], versions: [], tune: null, upload: null, added: '', saved: 0 };
  await loadPlaybook();
  await showVersion(state.book, version); // the address names the version on show, or none
}

// Loads into the playbook the request was made for, which may no longer be the one on screen.
async function loadPlaybook(book = state.book) {
  const at = (rest = '') => `/api/playbooks/${encodeURIComponent(book.id)}${rest}`;
  const [data, skills, versions] = await Promise.all([api(at()), api(at('/skills')), api(at('/versions'))]);
  Object.assign(book, { data, skills, versions });
  await loadTab(book); // a playbook reopened on one of those tabs shows what happened meanwhile
}

const text = () => state.book.draft ?? state.book.data.text;
// Typed and not saved. While it is, the agent is not asked and no version is restored: both would replace it.
export const unsaved = (book = state.book) => book.draft !== null && book.draft !== book.data.text;
// The version a save starts from. A draft keeps the version it was typed over, however often the page reloads the
// playbook, so a change saved meanwhile is reported and not silently replaced.
export const base = (book) => (book.draft !== null && book.base ? book.base : book.data.version);
export const startDraft = (book, value) => {
  if (book.draft === null) book.base = book.data.version;
  book.draft = value;
};

// What changed between two lists: the longest run of items they share stays, the rest is taken out or put in.
function changes(a, b) {
  const shared = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1)
    for (let j = b.length - 1; j >= 0; j -= 1) shared[i][j] = a[i] === b[j] ? shared[i + 1][j + 1] + 1 : Math.max(shared[i + 1][j], shared[i][j + 1]);
  const out = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      out.push(['', a[i]]);
      i += 1;
      j += 1;
    } else if (i < a.length && (j === b.length || shared[i + 1][j] >= shared[i][j + 1])) out.push(['del', a[i++]]);
    else out.push(['add', b[j++]]);
  }
  return out;
}

// A proposed rewrite beside the text it replaces. Paragraphs that stayed are shown plain. A paragraph that was
// rewritten is compared sentence by sentence, so a rule added to a long paragraph shows as that one sentence.
function diffHtml(before, after) {
  const paragraphs = (text) => text.split(/\n{2,}/).filter((p) => p.trim());
  const sentences = (text) => text.split(/(?<=[.!?])\s+/);
  const mark = ([kind, text]) => (kind ? `<span class="${kind}">${esc(text)}</span>` : esc(text));
  const parts = changes(paragraphs(before), paragraphs(after));
  const html = [];
  for (let i = 0; i < parts.length; i += 1) {
    const [kind, text] = parts[i];
    if (kind === 'del' && parts[i + 1]?.[0] === 'add') {
      html.push(`<p>${changes(sentences(text), sentences(parts[i + 1][1])).map(mark).join(' ')}</p>`);
      i += 1;
    } else html.push(`<p>${mark([kind, text])}</p>`);
  }
  return html.join('');
}

function instructionsTab(book) {
  const tune = book.tune;
  if (tune?.record) {
    const { proposal } = tune.record;
    const body = proposal.changed ? diffHtml(book.data.text, proposal.text) : '';
    return `<div class="me">${esc(tune.ask)}</div>
      <p class="say">${esc(proposal.summary)}</p>
      ${proposal.changed ? `<div class="box diff">${body || '<p class="empty">The instructions would be empty.</p>'}</div>` : ''}
      <div class="end">${btn('tune-discard', proposal.changed ? 'Discard' : 'Close', 'text', state.busy)}${proposal.changed ? btn('tune-accept', 'Accept', 'primary', state.busy) : ''}</div>`;
  }
  const waiting = tune?.pending;
  const saved = book.saved === book.data.version && book.saved > 1
    ? `<div class="saved"><span>Saved as version ${book.saved}</span><button class="btn text small" data-restore="${book.saved - 1}">Undo</button></div>`
    : '';
  return `<div class="box tall book"><textarea id="playbook-text" aria-label="Instructions the agent follows on every ${esc(book.data.name)} run" placeholder="What should the agent do on every ${esc(book.data.name)} run?" ${attr('disabled', waiting)}></textarea>${micButton('playbook-text', waiting, 'the instructions')}</div>${voiceNote('playbook-text')}
    ${saved}
    ${waiting ? `<div class="me">${esc(tune.ask)}</div><div class="live"><i></i><b>Rewriting the instructions</b></div>` : ''}
    <form class="ask" id="tune-form"><input id="tune" aria-label="Ask the agent to change the instructions" placeholder="Ask the agent to change the instructions" autocomplete="off" ${attr('disabled', waiting || !state.config?.configured)}>
      ${micButton('tune', waiting || !state.config?.configured, 'the request')}<button class="send" aria-label="Send" ${attr('disabled', waiting || !state.config?.configured)}>${icon('send')}</button></form>${voiceNote('tune')}
    <div class="end">${btn('playbook-save', 'Save', 'primary', state.busy || waiting)}</div>`;
}

function skillsTab(book) {
  const upload = book.upload;
  const note = upload
    ? `<div class="row"><span class="name"><span>${esc(upload.name)}</span>${upload.reason ? `<span class="desc problem">${esc(upload.reason)}</span>` : ''}</span>
        ${upload.reason ? pill('bad', 'Not added') : pill('live', 'Checking')}</div>`
    : '';
  const rows = book.skills
    .map(
      (s) => `<div class="row"><button class="name link" data-skill-open="${esc(s.name)}"><span>${esc(s.name)}</span><span class="desc">${esc(s.description)}${s.programs ? ` · ${plural(s.programs, 'program')}` : ''}</span></button>
        <div class="val">${book.added === s.name ? pill('ok', 'Added') : ''}
          <button class="switch ${s.enabled ? 'on' : ''}" role="switch" aria-checked="${s.enabled}" aria-label="Use ${esc(s.name)}" data-skill-toggle="${esc(s.name)}"></button>
          <button class="icon-btn" aria-label="Remove ${esc(s.name)}" data-skill-remove="${esc(s.name)}">${icon('x')}</button></div></div>`,
    )
    .join('');
  return `${rows || note ? `<div class="rows">${rows}${note}</div>` : '<p class="empty">No skills yet.</p>'}
    <div class="end">${btn('skill-pick', 'Upload skill ZIP', 'primary', state.busy || upload?.reason === '', 'up')}</div>`;
}

// What a version was, in a few words, from how it came to be.
export function versionLabel(v) {
  if (v.source === 'restored') return v.change;
  if (v.source === 'remembered') {
    const job = state.jobs.find((j) => j.id === v.detail?.job_id);
    return `Remembered from ${job ? jobTitle(job) : 'a run'}`;
  }
  if (v.source === 'created') return v.detail?.copied_from ? 'Created as a copy' : 'Created';
  if (v.source === 'learned') {
    const job = state.jobs.find((j) => j.id === v.detail?.job_id);
    return `Learned by the agent from ${job ? jobTitle(job) : 'a run'}`;
  }
  return { edited: 'Edited by you', tuned: 'Tuned with the agent', tidied: 'Tidied by the agent' }[v.source] || v.source;
}

// --- History: every version, and one opened to read before restoring it ---------------------------------------
// Shared with a client's notes (clients.js): `rec` is a playbook page (state.book) or a client page (state.client),
// and `rec.open` the version on show ({ version, look }). Restore is only offered from inside an opened version, so
// nobody brings text back without having seen it. The address names the version (routes.js), so Back, Forward and
// Refresh keep the place; Older and Newer take the place of the version on show rather than adding to Back.

const KINDS = {
  playbook: { path: 'playbooks', text: 'instructions', tab: 'Instructions' },
  client: { path: 'clients', text: 'notes', tab: 'Notes' },
};
const kindOf = (rec) => KINDS[rec.kind || 'playbook'];
const recUrl = (rec, rest) => `/api/${kindOf(rec).path}/${encodeURIComponent(rec.id)}${rest}`;
const shownChange = (v) => (['restored', 'tuned'].includes(v.source) ? '' : v.change);

// Every version, newest first; each row opens its version.
function historyHtml(rec) {
  const latest = rec.data.version;
  const rows = rec.versions
    .map(
      (v) => `<button class="row link" data-version-open="${v.version}" aria-label="Open version ${v.version}"><span class="name"><span>Version ${v.version} · ${esc(versionLabel(v))}</span>
          ${shownChange(v) ? `<span class="desc">${esc(shownChange(v))}</span>` : ''}</span>
        <span class="val"><span class="when">${day(v.created_at)}</span>${v.version === latest ? pill('ok', 'Current') : ''}${icon('chev')}</span></button>`,
    )
    .join('');
  return `<div class="rows">${rows}</div>`;
}

// How a version came to be, with the run it came from as a link when that run is still here.
function howHtml(v) {
  const job = ['remembered', 'learned'].includes(v.source) && state.jobs.find((j) => j.id === v.detail?.job_id);
  if (!job) return esc(versionLabel(v));
  const how = v.source === 'learned' ? 'Learned by the agent from' : 'Remembered from';
  return `${how} <button class="ref" data-job="${esc(job.id)}">${esc(jobTitle(job))}</button>`;
}

const fullHtml = (text, words) =>
  text.trim() ? text.split(/\n{2,}/).filter((p) => p.trim()).map((p) => `<p>${esc(p)}</p>`).join('') : `<p class="empty">No ${words} in this version.</p>`;

function versionHtml(rec) {
  const { text: words, tab } = kindOf(rec);
  const all = rec.versions;
  const at = all.findIndex((v) => v.version === rec.open.version);
  const v = all[at];
  const older = all[at + 1];
  const newer = all[at - 1];
  const isCurrent = v.version === rec.data.version;
  const text = rec.texts[v.version];
  const same = !isCurrent && text === rec.data.text;
  const look = isCurrent ? 'full' : rec.open.look;
  const step = (to, label) => `<button class="btn text small" data-version-step="${to?.version || ''}" ${attr('disabled', !to)}>${label}</button>`;
  const seg = isCurrent
    ? ''
    : `<div class="viewbar"><div class="seg" role="group" aria-label="Show">${[['full', 'Full text'], ['diff', 'What Restore changes']]
        .map(([id, label]) => `<button class="${look === id ? 'on' : ''}" data-version-look="${id}" aria-pressed="${look === id}">${label}</button>`)
        .join('')}</div>${look === 'diff' && !same ? '<div class="legend"><span class="back">Comes back</span><span class="gone">Goes</span></div>' : ''}</div>`;
  const shown = look === 'diff' ? (same ? '<p class="empty">Same as now.</p>' : diffHtml(rec.data.text, text)) : fullHtml(text, words);
  const blocked = unsaved(rec);
  const hint = blocked ? `Save your edits on ${tab} first.` : same ? 'This is the same as the current text.' : '';
  const restore = isCurrent ? pill('ok', 'Current') : `<button class="btn primary" data-version-restore ${attr('disabled', state.busy || blocked || same)}>Restore</button>`;
  const change = shownChange(v);
  return `<div class="version"><div class="version-head">
      <button class="icon-btn" data-version-back aria-label="Back to history" title="Back to history">${icon('back')}</button>
      <div class="title"><h2>Version ${v.version}</h2><p>${howHtml(v)} · ${day(v.created_at)}</p></div>
      <div class="steps">${step(older, 'Older')}${step(newer, 'Newer')}</div></div>
    ${change ? `<p class="why">${esc(change)}</p>` : ''}
    ${v.source === 'tuned' && v.change ? `<p class="why">You asked: “${esc(v.change)}”</p>` : ''}
    ${seg}
    <div class="box diff" aria-label="${esc(tab)} in version ${v.version}">${shown}</div>
    <div class="end">${hint ? `<span class="hint">${esc(hint)}</span>` : ''}<button class="btn text" data-version-back>Back</button>${restore}</div></div>`;
}

export const historyBody = (rec) => (rec.open ? versionHtml(rec) : historyHtml(rec));
const historyTab = (book) => historyBody(book);

// Put version `number` on show (null shows the list). Its text is fetched once: a version never changes.
export async function showVersion(rec, number) {
  if (!number) return void (rec.open = null);
  rec.tab = 'history';
  if (!rec.versions.some((v) => v.version === number)) {
    rec.open = null;
    return toast(`Version ${number} is not in the history.`);
  }
  rec.texts ||= {};
  if (!(number in rec.texts)) rec.texts[number] = number === rec.data.version ? rec.data.text : (await api(recUrl(rec, `/versions/${number}`))).text;
  rec.open = { version: number, look: rec.open?.look || 'full' };
}

// Back to the list: the browser's Back when the list is the page before, so the two never disagree.
export function leaveVersion(rec, draw) {
  if (!rec?.open) return false;
  if (history.state?.fromList) history.back();
  else {
    rec.open = null;
    history.replaceState({ ...history.state, fromList: false }, '', pageAddress());
    draw();
  }
  return true;
}

// Clicks inside History. `restore(version, quiet)` saves on the page's own record and says whether it did.
export async function versionClick(rec, target, draw, restore) {
  let el;
  if ((el = target('[data-version-open]'))) {
    await showVersion(rec, Number(el.dataset.versionOpen));
    if (!rec.open) return draw(), true;
    rec.open.look = 'full';
    // Pushed here, marked as opened from the list, so Back knows the list is the page before.
    history.pushState({ depth: (history.state?.depth || 0) + 1, fromList: true }, '', pageAddress());
  } else if (!rec.open && !target('[data-version-undo]')) return false;
  else if ((el = target('[data-version-step]'))) {
    if (!el.dataset.versionStep) return true;
    await showVersion(rec, Number(el.dataset.versionStep));
    history.replaceState(history.state, '', pageAddress());
  } else if ((el = target('[data-version-look]'))) rec.open.look = el.dataset.versionLook;
  else if (target('[data-version-back]')) return leaveVersion(rec, draw);
  else if (target('[data-version-restore]')) {
    const from = rec.open.version;
    const before = rec.data.version;
    if (!(await restore(from, true))) return draw(), true;
    leaveVersion(rec, draw);
    const undo = `<button class="btn text small" data-version-undo="${before}" data-record="${esc(rec.id)}">Undo</button>`;
    toast(`Restored version ${from} as version ${rec.data.version}.`, undo);
    return true;
  } else if ((el = target('[data-version-undo]'))) {
    hideToast();
    if (el.dataset.record !== rec.id) return true; // the page it was for is no longer on show
    const back = Number(el.dataset.versionUndo);
    if (await restore(back, true)) toast(`Undone. Version ${back}’s text is back.`);
  } else return false;
  draw();
  return true;
}

// The past corrections kept as tests. The agent keeps the list; the person reads the score.
function testsTab(book) {
  const t = book.tests;
  if (!t) return '<p class="empty">Loading</p>';
  if (!t.cases.length && !t.retired.length) return '<p class="empty">No past corrections yet.</p>';
  const verdict = (last) => (!last || last.passed == null ? pill('', 'Not checked yet') : last.passed ? pill('ok', 'Right') : pill('warn', 'Wrong'));
  const detail = (c) => {
    const from = state.jobs.find((j) => j.id === c.job_id);
    const where = `${c.file}, ${c.where}`;
    const brief = (value) => (String(value).length > 120 ? `${String(value).slice(0, 120)}…` : value); // a whole paragraph can be the value
    if (c.last && c.last.passed === false) return `${where} · got ${brief(c.last.got ?? 'nothing')}, expected ${brief(c.expected)}`;
    return `${where}${from ? ` · from ${jobTitle(from)}` : ''}`;
  };
  const rows = t.cases
    .map((c) => `<div class="row"><span class="name"><span>${esc(c.lesson)}</span><span class="desc wrap">${esc(detail(c))}</span></span>${verdict(c.last)}</div>`)
    .join('');
  const retired = t.retired
    .map(
      (c) => `<div class="row"><span class="name"><span>${esc(c.lesson)}</span><span class="desc wrap">Retired ${day(c.retired_at)}: ${esc(c.reason)}</span></span>
        <button class="btn text" data-test-restore="${esc(c.id)}">Bring back</button></div>`,
    )
    .join('');
  const score = t.checked ? `${t.right} of ${t.checked} still right` : `${plural(t.cases.length, 'past correction')}`;
  const when = t.running ? 'Checking now' : t.last_check ? `Checked ${day(t.last_check.finished_at)}${t.last_check.version ? `, version ${t.last_check.version}` : ''}` : '';
  const blocked = state.busy || t.running || !t.cases.length || !state.config?.configured;
  return `<div class="page-head wraps"><h2>${esc(score)}</h2><span class="row-end"><span class="desc">${esc(when)}</span>${btn('tests-check', 'Check now', 'quiet', blocked)}</span></div>
    ${rows ? `<div class="rows">${rows}</div>` : ''}${retired ? `<div class="rows">${retired}</div>` : ''}`;
}

// Everything that happened to this playbook, newest first.
function activityTab(book) {
  if (!book.activity) return '<p class="empty">Loading</p>';
  // What its runs and its checks of past corrections cost, priced now; nothing when no price is set in Settings.
  const cost = book.data.cost;
  const spent = cost
    ? `<div class="facts"><div><span>${plural(cost.runs.count, 'run')}</span><span>${money(cost.runs.usd)}</span></div>
        <div><span>Re-checking past corrections</span><span>${money(cost.replays.usd)}</span></div></div>`
    : '';
  if (!book.activity.length) return `${spent}<p class="empty">Nothing yet.</p>`;
  const stamp = (t) => new Date(t).toLocaleString('en', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  const rows = book.activity
    .map(
      (e) => `<div class="row"><span class="name"><span class="wrap">${esc(e.message)}</span></span>
        <span class="row-end">${e.level === 'WARN' ? pill('warn', 'Check') : ''}<span class="desc">${esc(stamp(e.timestamp))}</span></span></div>`,
    )
    .join('');
  return `${spent}<div class="rows">${rows}</div>`;
}

// The two tabs that show what the app did by itself are loaded when opened and kept fresh while on screen:
// a check or a tidy can start and finish without the person doing anything.
async function loadTab(book = state.book) {
  const at = (rest) => `/api/playbooks/${encodeURIComponent(book.id)}${rest}`;
  if (book.tab === 'tests') book.tests = await api(at('/tests'));
  if (book.tab === 'activity') book.activity = await api(at('/events?recent=100'));
}

export async function refreshPlaybookTab(draw) {
  const book = state.book;
  if (state.view !== 'playbook' || !book || !['tests', 'activity'].includes(book.tab)) return;
  await loadTab(book);
  draw();
}

export function playbookPage() {
  const book = state.book;
  if (!book?.data) return page('<p class="empty">Loading</p>');
  const tabs = TABS.map(([id, label]) => `<button class="${book.tab === id ? 'on' : ''}" data-book-tab="${id}">${label}</button>`).join('');
  const body = { instructions: instructionsTab, skills: skillsTab, tests: testsTab, activity: activityTab, history: historyTab }[book.tab](book);
  return page(`<div class="page-head"><button class="icon-btn" data-back="playbooks" aria-label="Back" title="Back">${icon('back')}</button>
      <h1 title="${esc(book.data.name)}">${esc(book.data.name)}</h1><div class="controls">${btn('rename-playbook', 'Rename', 'text')}${
        playbooks().length > 1 ? btn('delete-playbook', 'Delete', 'text') : '<button class="btn text" disabled title="The only playbook cannot be deleted">Delete</button>'
      }</div></div>
    <div class="filter">${tabs}</div>${body}`);
}

// Put what was typed back after a redraw, and set what depends on it here, so typing never redraws the page.
export function syncPlaybook() {
  const box = $('#playbook-text');
  if (!box) return;
  if (box.value !== text()) box.value = text();
  const ask = $('#tune');
  if (ask.value !== state.book.ask) ask.value = state.book.ask;
  const waiting = state.book.tune?.pending;
  $('[data-action="playbook-save"]').disabled = state.busy || waiting || !unsaved();
  // The agent rewrites the saved text, so it is asked once the person's own edits are saved.
  const blocked = waiting || !state.config?.configured || unsaved();
  ask.disabled = blocked;
  const mic = $('[data-mic="tune"]');
  if (mic && !mic.classList.contains('listening')) mic.disabled = blocked;
  ask.placeholder = unsaved() ? 'Save your edits to ask the agent' : 'Ask the agent to change the instructions';
  $('#tune-form .send').disabled = blocked || !state.book.ask.trim();
}

// --- Small dialogs ----------------------------------------------------------

export function dialog(title, body, action, label) {
  $('#details').innerHTML = `<div class="dialog-head"><h2>${esc(title)}</h2><button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
    <form class="dialog-body" id="book-form" data-does="${action}">${body}
      <div class="end"><button type="button" class="btn text" data-close>Cancel</button><button class="btn primary">${esc(label)}</button></div></form>`;
  $('#details').showModal();
}

// A blank name is caught in the dialog itself (app.js), which says so under the box.
export const nameField = (value = '') => `<label>Name<input class="field" name="name" value="${esc(value)}" data-need-name autocomplete="off"></label>`;

export const playbookDialogs = {
  'new-playbook': () =>
    dialog(
      'New playbook',
      `${nameField()}<label>Start from<select class="field" name="copy_of"><option value="">Blank</option>
        ${playbooks().map((p) => `<option value="${esc(p.id)}">Copy of ${esc(p.name)}</option>`).join('')}</select></label>`,
      'create',
      'Create',
    ),
  'rename-playbook': () => dialog('Rename playbook', nameField(state.book.data.name), 'rename', 'Rename'),
  'delete-playbook': () => {
    // What goes and what stays, said before anything is removed: its runs keep their own copy of what they used.
    const name = state.book.data.name;
    const runs = (state.jobs || []).filter((j) => j.module === state.book.id).length;
    const stays = runs ? `Its ${plural(runs, 'run')} stay, and still open.` : 'It has no runs.';
    $('#details').innerHTML = `<div class="dialog-head"><h2>Delete ${esc(name)}?</h2><button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
      <form class="dialog-body" id="book-form" data-does="delete-playbook"><p class="muted">Its instructions, skills, past corrections and history go, and can't be brought back. ${stays}</p>
        <div class="end"><button type="button" class="btn text" data-close>Cancel</button><button class="btn danger">Delete playbook</button></div></form>`;
    $('#details').showModal();
  },
};

function removeDialog(name) {
  $('#details').innerHTML = `<div class="dialog-head"><h2>Remove ${esc(name)}?</h2><button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
    <form class="dialog-body" id="book-form" data-does="remove-skill" data-name="${esc(name)}">
      <div class="end"><button type="button" class="btn text" data-close>Cancel</button><button class="btn primary">Remove</button></div></form>`;
  $('#details').showModal();
}

async function skillDialog(name) {
  const skill = await api(url(`/skills/${encodeURIComponent(name)}`));
  $('#details').innerHTML = `<div class="dialog-head"><h2>${esc(skill.name)}</h2><button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
    <pre>${esc(skill.text)}</pre>${skill.code.length ? `<p class="note">This skill comes with ${plural(skill.code.length, 'program')}. The agent runs them in its own sealed workspace, never on this computer. ${skill.enabled ? '' : 'The skill is switched off until you have read them: switch it on in the list when you are happy with them.'}</p>` : ''}${skill.code.map((c) => `<h3>${esc(c.path)}</h3><pre>${esc(c.text)}</pre>`).join('')}`;
  $('#details').showModal();
}

// --- Actions ----------------------------------------------------------------

// A save lost a race with another change: show the latest and keep what the person typed. Returns false, so the
// caller knows nothing was saved.
async function stale(error, book) {
  if (error.status !== 409) throw error;
  await loadPlaybook(book);
  book.base = book.data.version; // the person is told, and the latest is on show: their next save is deliberate
  toast(error.message);
  return false;
}

// Save `body` as a new version of `book`. Everything that follows applies to that playbook, whichever page is on
// screen when the answer arrives, and a draft typed meanwhile is kept.
async function save(book, path, body, submitted) {
  let result;
  try {
    result = await api(`/api/playbooks/${encodeURIComponent(book.id)}${path}`, json(body));
  } catch (error) {
    return stale(error, book);
  }
  if (book.draft === submitted) book.draft = null;
  book.saved = result.saved === false ? 0 : result.version;
  await loadPlaybook(book);
  book.base = book.data.version; // anything typed while it saved continues from what was just saved
  const listed = playbooks().find((p) => p.id === book.id);
  if (listed) listed.version = book.data.version; // the list and a run's Undo go by the latest version
  return true;
}

// Buttons that call the server; app.js holds them while they run.
export const playbookActions = {
  'tests-check': async () => {
    await api(url('/tests/check'), { method: 'POST' });
    await loadTab();
    toast('Checking past corrections');
  },
  'playbook-save': () => {
    const book = state.book;
    return save(book, '/versions', { text: text(), based_on: base(book) }, book.draft);
  },
  'tune-accept': async () => {
    const book = state.book;
    const { record } = book.tune;
    book.tune = null;
    await save(book, '/versions', { text: record.proposal.text, based_on: book.data.version, proposal_id: record.id }, null);
  },
  'tune-discard': async () => {
    const { record } = state.book.tune;
    state.book.tune = null;
    await api(url(`/tune/${record.id}/discard`), { method: 'POST' });
  },
};

export async function askTune(ask) {
  const book = state.book;
  book.ask = '';
  book.tune = { ask, pending: true };
  try {
    const record = await api(url('/tune'), json({ ask, based_on: book.data.version }));
    book.tune = { ask, record };
  } catch (error) {
    book.tune = null;
    await stale(error, book);
  }
}

export async function restoreVersion(version, quiet = false) {
  const book = state.book;
  if (!(await save(book, '/restore', { version, based_on: book.data.version }, book.draft))) return false;
  book.saved = 0;
  if (!quiet) toast(`Restored version ${version}.`);
  return true;
}

export async function submitDialog(form) {
  const does = form.dataset.does;
  const values = Object.fromEntries(new FormData(form));
  if (does === 'create') {
    const made = await api('/api/playbooks', json({ name: values.name, copy_of: values.copy_of || null }));
    $('#details').close();
    await openPlaybook(made.id);
  } else if (does === 'rename') {
    await api(url(), { method: 'PATCH', body: JSON.stringify({ name: values.name }) });
    $('#details').close();
    await loadPlaybook();
  } else if (does === 'delete-playbook') {
    const gone = await api(url(), { method: 'DELETE' });
    $('#details').close();
    delete state.books[gone.id];
    state.config = await api('/api/config'); // the playbook list New run offers
    if (state.playbook === gone.id) state.playbook = playbooks()[0]?.id || '';
    // The list takes the deleted playbook's place in history, so Back does not reach a page that is gone.
    history.replaceState(history.state, '', '#/playbooks');
    state.view = 'playbooks';
    toast(`Deleted ${gone.name}. ${gone.runs_kept ? `Its ${plural(gone.runs_kept, 'run')} ${gone.runs_kept === 1 ? 'is' : 'are'} kept.` : ''}`.trim());
  } else if (does === 'remove-skill') {
    await api(url(`/skills/${encodeURIComponent(form.dataset.name)}`), { method: 'DELETE' });
    $('#details').close();
    Object.assign(state.book, { added: '', upload: null });
    await loadPlaybook();
  }
}

export async function uploadSkill(file, draw) {
  const book = state.book;
  book.upload = { name: file.name, reason: '' };
  book.added = '';
  draw();
  const form = new FormData();
  form.append('file', file, file.name);
  try {
    const skill = await api(url('/skills'), { method: 'POST', body: form });
    book.upload = null;
    book.added = skill.name;
    await loadPlaybook();
    if (skill.review) {
      draw(); // it arrived switched off: show its programs straight away, so the user can read them and switch it on
      await skillDialog(skill.name);
    }
  } catch (error) {
    book.upload = { name: file.name, reason: error.message };
  }
}

// Clicks on the playbook pages that are not plain buttons. Returns true when it handled the click.
export async function playbookClick(target, draw) {
  let el;
  if (state.view === 'playbook' && state.book && (await versionClick(state.book, target, draw, restoreVersion))) return true;
  if ((el = target('[data-book]'))) {
    if (!el.dataset.book) return true;
    try {
      await openPlaybook(el.dataset.book);
    } catch (error) {
      state.view = 'playbooks'; // a playbook that cannot be loaded is not left on a page that says "Loading"
      draw();
      throw error;
    }
  } else if ((el = target('[data-book-tab]'))) {
    state.book.tab = el.dataset.bookTab;
    state.book.open = null; // History opens on its list
    state.book.added = '';
    if (state.book.upload?.reason) state.book.upload = null;
    draw();
    await loadTab();
  } else if ((el = target('[data-test-restore]'))) {
    await api(url(`/tests/${encodeURIComponent(el.dataset.testRestore)}/restore`), { method: 'POST' });
    await loadTab();
  } else if ((el = target('[data-restore]'))) await restoreVersion(Number(el.dataset.restore));
  else if ((el = target('[data-skill-toggle]'))) {
    const skill = state.book.skills.find((s) => s.name === el.dataset.skillToggle);
    await api(url(`/skills/${encodeURIComponent(skill.name)}`), { method: 'PATCH', body: JSON.stringify({ enabled: !skill.enabled }) });
    await loadPlaybook();
  } else if ((el = target('[data-skill-remove]'))) removeDialog(el.dataset.skillRemove);
  else if ((el = target('[data-skill-open]'))) await skillDialog(el.dataset.skillOpen);
  else return false;
  draw();
  return true;
}
