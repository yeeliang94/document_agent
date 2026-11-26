// Clients: the list, and one client's notes, runs and history; the client box on the New run page; and taking a
// lesson or a remembered comment back out of a client's notes from the run.
// A client's notes apply to all of their work, on top of the playbook. They are kept as versions the way a
// playbook's instructions are, so this page reuses the playbook page's pieces (playbooks.js).

import { $, api, attr, btn, esc, icon, inGroup, jobTitle, jobUrl, plural, state, toast } from './core.js';
import { micButton, voiceNote } from './voice.js';
import { base, dialog, historyBody, nameField, showVersion, startDraft, unsaved, versionClick, versionLabel } from './playbooks.js';

const TABS = [
  ['notes', 'Notes'],
  ['runs', 'Runs'],
  ['history', 'History'],
];
const at = (id, rest = '') => `/api/clients/${encodeURIComponent(id)}${rest}`;
const page = (body) => `<div class="page"><div class="narrow">${body}</div></div>`;
const json = (body) => ({ method: 'POST', body: JSON.stringify(body) });
const runsOf = (id) => state.jobs.filter((j) => j.client_id === id);

export const clients = () => state.clients || [];

export async function refreshClients() {
  state.clients = await api('/api/clients');
}

// --- The list ---------------------------------------------------------------

export function clientsPage() {
  const rows = clients()
    .map((c) => {
      const runs = runsOf(c.id);
      const books = [...new Set(runs.map((j) => j.playbook).filter(Boolean))].join(', ');
      return `<button class="row link" data-client="${esc(c.id)}"><span class="name"><span>${esc(c.name)}</span>
        <span class="desc">${esc([books, plural(runs.length, 'run')].filter(Boolean).join(' · '))}</span></span>${icon('chev')}</button>`;
    })
    .join('');
  return page(`<div class="page-head"><h1>Clients</h1>${btn('new-client', 'New client', 'primary', false, 'plus')}</div>
    ${rows ? `<div class="rows">${rows}</div>` : '<p class="empty">No clients yet.</p>'}`);
}

// --- One client -------------------------------------------------------------

export async function openClient(id, { keepTab = false, version = null } = {}) {
  state.view = 'client';
  // Notes typed and not saved are kept for the visit, as a playbook's are.
  state.client = state.clientPages[id] ||= { id, kind: 'client', tab: 'notes', data: null, draft: null, versions: [], saved: 0 };
  if (!keepTab) state.client.tab = 'notes'; // a client opens on its notes, from the list or a run; Back keeps its tab
  await loadClient();
  await showVersion(state.client, version); // the address names the version on show, or none
}

async function loadClient(client = state.client) {
  const [data, versions] = await Promise.all([api(at(client.id)), api(at(client.id, '/versions'))]);
  Object.assign(client, { data, versions });
  const listed = clients().find((c) => c.id === client.id);
  if (listed) Object.assign(listed, { name: data.name, version: data.version }); // a run's Undo goes by the latest
}

const notesText = (client) => client.draft ?? client.data.text;
const short = (text, n = 90) => (text.length > n ? `${text.slice(0, n).trimEnd()}…` : text);

// The latest version, when a run added it: where it came from, what it added, and Undo.
function addedHtml(client) {
  const latest = client.versions[0];
  if (!latest || !['remembered', 'learned'].includes(latest.source) || client.saved === latest.version) return '';
  const job = state.jobs.find((j) => j.id === latest.detail?.job_id);
  const from = job ? `Added from ${jobTitle(job)}` : versionLabel(latest);
  return `<div class="card deliver"><div class="filerow"><span class="name">${esc(from)}: “${esc(short(latest.change || ''))}”</span>
    <button class="btn text small" data-client-restore="${latest.version - 1}" ${attr('disabled', state.busy || unsaved(client))}>Undo</button></div></div>`;
}

function notesTab(client) {
  const saved =
    client.saved === client.data.version && client.saved > 1
      ? `<div class="saved"><span>Saved as version ${client.saved}</span><button class="btn text small" data-client-restore="${client.saved - 1}">Undo</button></div>`
      : '';
  return `<div class="box tall book"><textarea id="client-notes" aria-label="Notes for ${esc(client.data.name)}" placeholder="What to follow for ${esc(client.data.name)}"></textarea>${micButton('client-notes')}</div>${voiceNote('client-notes')}
    ${saved}${addedHtml(client)}
    <div class="end">${btn('client-save', 'Save', 'primary', state.busy || !unsaved(client))}</div>`;
}

function runsTab(client) {
  const rows = runsOf(client.id)
    .map(
      (j) => `<button class="row link" data-job="${esc(j.id)}"><span class="name"><span>${esc(jobTitle(j))}</span>
        <span class="desc">${esc([j.playbook, new Date(j.created_at).toLocaleDateString('en', { day: 'numeric', month: 'short' })].filter(Boolean).join(' · '))}</span></span>${icon('chev')}</button>`,
    )
    .join('');
  return rows ? `<div class="rows">${rows}</div>` : '<p class="empty">No runs yet.</p>';
}

export function clientPage() {
  const client = state.client;
  if (!client?.data) return page('<p class="empty">Loading</p>');
  const tabs = TABS.map(([id, label]) => `<button class="${client.tab === id ? 'on' : ''}" data-client-tab="${id}">${label}</button>`).join('');
  const body = { notes: notesTab, runs: runsTab, history: historyBody }[client.tab](client);
  return page(`<div class="page-head"><button class="icon-btn" data-back="clients" aria-label="Back" title="Back">${icon('back')}</button>
      <h1>${esc(client.data.name)}</h1>${btn('rename-client', 'Rename', 'text')}</div>
    <div class="filter">${tabs}</div>${body}`);
}

// Put the typed notes back after a redraw, and light Save once they differ, so typing never redraws the page.
export function syncClient() {
  const box = $('#client-notes');
  if (!box) return;
  if (box.value !== notesText(state.client)) box.value = notesText(state.client);
  $('[data-action="client-save"]').disabled = state.busy || !unsaved(state.client);
}

export function typeNotes(value) {
  startDraft(state.client, value);
  syncClient();
}

// Save a new version of a client's notes, or bring an earlier one back. A save that lost a race with another change
// shows the latest and keeps what was typed.
async function save(client, path, body, submitted) {
  let result;
  try {
    result = await api(at(client.id, path), json(body));
  } catch (error) {
    if (error.status !== 409) throw error;
    await loadClient(client);
    client.base = client.data.version;
    toast(error.message);
    return false;
  }
  if (client.draft === submitted) client.draft = null;
  client.saved = result.saved === false ? 0 : result.version;
  await loadClient(client);
  client.base = client.data.version;
  return true;
}

async function restore(version, quiet = false) {
  const client = state.client;
  if (!(await save(client, '/restore', { version, based_on: client.data.version }, client.draft))) return false;
  client.saved = 0;
  if (!quiet) toast(`Restored version ${version}.`);
  return true;
}

export const clientActions = {
  'client-save': () => {
    const client = state.client;
    return save(client, '/versions', { text: notesText(client), based_on: base(client) }, client.draft);
  },
};

export const clientDialogs = {
  'new-client': () => dialog('New client', nameField(), 'create-client', 'Create'),
  'rename-client': () => dialog('Rename client', nameField(state.client.data.name), 'rename-client', 'Rename'),
};

// The dialogs above share the playbook dialog's form. Returns false for a form that is not a client's.
export async function submitClientDialog(form) {
  const does = form.dataset.does;
  if (!['create-client', 'rename-client'].includes(does)) return false;
  const name = new FormData(form).get('name');
  if (does === 'create-client') {
    const made = await api('/api/clients', json({ name }));
    $('#details').close();
    await refreshClients();
    await openClient(made.id);
  } else {
    await api(at(state.client.id), { method: 'PATCH', body: JSON.stringify({ name }) });
    $('#details').close();
    await loadClient();
    state.jobs = await api('/api/jobs'); // the runs carry the client's name
  }
  return true;
}

// Clicks on the client pages. Returns true when it handled the click.
export async function clientClick(target, draw) {
  let el;
  if (state.view === 'client' && state.client && (await versionClick(state.client, target, draw, restore))) return true;
  if ((el = target('[data-client]'))) {
    try {
      await openClient(el.dataset.client);
    } catch (error) {
      state.view = 'clients'; // a client that cannot be loaded is not left on a page that says "Loading"
      draw();
      throw error;
    }
  } else if ((el = target('[data-client-tab]'))) Object.assign(state.client, { tab: el.dataset.clientTab, open: null });
  else if ((el = target('[data-client-restore]'))) await restore(Number(el.dataset.clientRestore));
  else return false;
  draw();
  return true;
}

// --- The client box on New run ------------------------------------------------
// Chosen files whose folder or names point to a known client bring a suggestion. It is only ever shown: the person
// takes it with Use, or picks another client or none, and a run never follows a client nobody chose.

export function clientRow(row) {
  if (!clients().length) return '';
  const chosen = state.runClient;
  const hint = state.clientSuggestion && !chosen && !state.clientDecided ? state.clientSuggestion : null;
  const options = [['', 'No client'], ...clients().map((c) => [c.id, c.name])]
    .map(([id, name]) => `<option value="${esc(id)}" ${attr('selected', id === chosen)}>${esc(name)}</option>`)
    .join('');
  const suggestion = hint
    ? `<span class="when">Suggested: ${esc(hint.name)}</span><button class="btn quiet small" data-use-client="${esc(hint.id)}">Use</button>`
    : '';
  return row('Client', `<div class="val wraps">${suggestion}<select class="field pick" aria-label="Client" data-run-client>${options}</select></div>`);
}

export function chooseRunClient(id) {
  state.runClient = id;
  state.clientDecided = true;
}

// Ask which client the chosen files point to. The answer is for the files still chosen when it arrives.
export async function suggestClient(files) {
  state.clientSuggestion = null;
  if (!files.length || !clients().length) return;
  const paths = files.slice(0, 5000).map((f) => f.webkitRelativePath || f.name);
  try {
    const { client } = await api('/api/clients/suggest', json({ paths }));
    if (state.files === files) state.clientSuggestion = client;
  } catch {
    // no suggestion; the person can still choose a client
  }
}

// --- On the run -----------------------------------------------------------------

// The run follows a client's notes: said once in the conversation, with the way to open them.
export function followingHtml(job) {
  if (!job.client) return '';
  const known = clients().some((c) => c.id === job.client.id);
  return `<div class="card deliver"><div class="filerow"><span class="name">Following the notes for ${esc(job.client.name)}</span>
    ${known ? `<button class="btn text small" data-client="${esc(job.client.id)}">Open</button>` : ''}</div></div>`;
}

// The run's client as listed, for its latest version.
export const runClient = (job = state.job) => clients().find((c) => c.id === job?.client?.id);

// --- After a client's first run --------------------------------------------------------------------------------
// What the agent found worth keeping for a client with no notes yet (the run's report.for_next_time), offered in
// one slim line at the top of the run; opened, it lists them, and one click saves them all as the client's notes.

const hidden = new Set(); // runs whose "Saved" line was closed
const opened = new Set(); // runs whose offer is open

export function offerHtml() {
  const job = state.job;
  const keep = job?.report?.for_next_time || [];
  const offer = job?.offer || {};
  if (!keep.length || !job.client?.id || offer.dismissed || !inGroup('finished', job.status) || hidden.has(job.id)) return '';
  // Offered only onto the notes the run was made with: once they changed (another run, a person), the person adds
  // what they want by hand.
  const now = clients().find((c) => c.id === job.client.id);
  if (!offer.saved && now && job.client.version && now.version !== job.client.version) return '';
  const name = esc(job.client.name) + (/s$/i.test(job.client.name) ? "'" : "'s"); // Bluefin Foods', Acme's
  if (offer.saved) {
    return `<div class="offer"><div class="offer-line"><span class="what">Saved to ${name} notes.</span>
      <button class="btn text small" id="offer-undo" data-action="offer-undo" ${attr('disabled', state.busy)}>Undo</button><button class="btn text small" data-client="${esc(job.client.id)}">View notes</button>
      <button class="icon-btn" data-action="offer-hide" aria-label="Close">${icon('x')}</button></div></div>`;
  }
  const open = opened.has(job.id);
  const body = open
    ? `<div class="offer-body"><ul>${keep.map((line) => `<li>${esc(line)}</li>`).join('')}</ul>
        <div class="end">${btn('offer-save', 'Save to notes', 'primary small', state.busy)}${btn('offer-dismiss', 'Not now', 'text small', state.busy)}</div></div>`
    : '';
  return `<div class="offer"><div class="offer-line">
      <button class="open" id="offer-toggle" data-action="offer-toggle" aria-expanded="${open}">${icon('chev')}<span>Keep ${plural(keep.length, 'thing')} for ${name} next run?</span></button>
      <button class="icon-btn" data-action="offer-dismiss" aria-label="Not now" title="Not now">${icon('x')}</button></div>${body}</div>`;
}

const keepFor = async (action) => {
  try {
    state.job = await api(jobUrl('/keep-for-client'), { method: 'POST', body: JSON.stringify({ action }) });
  } finally {
    await refreshClients(); // the client's notes and version changed, or changed under it (a refused save)
  }
};
export const offerLocal = {
  'offer-toggle': () => (opened.has(state.job.id) ? opened.delete(state.job.id) : opened.add(state.job.id)),
  'offer-hide': () => hidden.add(state.job.id),
};
export const offerServer = {
  'offer-save': () => keepFor('save'),
  'offer-undo': () => keepFor('undo'),
  'offer-dismiss': () => keepFor('dismiss'),
};
