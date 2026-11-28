// Shared state, small helpers and the network calls. The screens (thread.js, files.js, pages.js) read from here;
// app.js owns rendering and the event wiring.

export const $ = (selector) => document.querySelector(selector);
export const esc = (value) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
export const attr = (name, on) => (on ? name : '');

const icons = {
  plus: 'M12 5v14M5 12h14',
  send: 'M12 19V5M6 11l6-6 6 6',
  x: 'M6 6l12 12M18 6L6 18',
  back: 'M15 6l-6 6 6 6',
  chev: 'M9 6l6 6-6 6',
  tick: 'M5 12.5l4.5 4.5L19 7',
  down: 'M12 4v12M7 11l5 5 5-5M5 20h14',
  clip: 'M20 11l-8.5 8.5a5 5 0 01-7-7L13 4a3.5 3.5 0 015 5l-8.5 8.5a2 2 0 01-3-3L14 7',
  up: 'M12 20V8M7 13l5-5 5 5M5 4h14',
  find: 'M10.5 17a6.5 6.5 0 100-13 6.5 6.5 0 000 13zM15.5 15.5L20 20',
  up1: 'M6 15l6-6 6 6',
  minus: 'M5 12h14',
  expand: 'M15 4h5v5M9 20H4v-5M20 4l-6 6M4 20l6-6',
  shrink: 'M4 14h6v6M20 10h-6V4M14 10l6-6M10 14l-6 6',
  down1: 'M6 9l6 6 6-6',
  more: 'M7 12a1 1 0 11-2 0 1 1 0 012 0zM13 12a1 1 0 11-2 0 1 1 0 012 0zM19 12a1 1 0 11-2 0 1 1 0 012 0z',
  side: 'M4 5h16v14H4zM9 5v14',
  comment: 'M4 5h16v11h-9l-5 4v-4H4z',
  mic: 'M12 3a3 3 0 00-3 3v6a3 3 0 006 0V6a3 3 0 00-3-3zM6 11a6 6 0 0012 0M12 17v4',
  stop: 'M8 8h8v8H8z',
  spin: 'M12 4a8 8 0 108 8',
};
export const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icons[name]}"/></svg>`;
export const btn = (action, label, kind = 'quiet', disabled = false, glyph = '') =>
  `<button class="btn ${kind}" data-action="${action}" ${attr('disabled', disabled)}>${glyph ? icon(glyph) : ''}${esc(label)}</button>`;
// A long name is cut in the middle, so its end (the month, the version, the file type) stays readable; CSS shrinks
// the start only, and never below a few letters (.cut in styles.css). While a find is on, the match is marked.
const KEEP = 12;
export function nameHtml(name, query = '') {
  name = String(name ?? '');
  const at = query ? name.toLowerCase().indexOf(query.toLowerCase()) : -1;
  // Marks the match within one part of the name; a match across the cut is marked in each part it touches.
  const part = (text, from) => {
    if (at < 0) return esc(text);
    const a = Math.max(0, at - from);
    const b = Math.min(text.length, at + query.length - from);
    return a >= b ? esc(text) : `${esc(text.slice(0, a))}<mark>${esc(text.slice(a, b))}</mark>${esc(text.slice(b))}`;
  };
  if (name.length <= KEEP + 6) return `<span class="cut"><span class="start">${part(name, 0)}</span></span>`;
  const cut = name.length - KEEP;
  return `<span class="cut"><span class="start">${part(name.slice(0, cut), 0)}</span><span class="end">${part(name.slice(cut), cut)}</span></span>`;
}

// The agent's own records among a run's outputs: its notes and summary, shown apart from what it made for the person,
// and the two files the app reads for itself, not listed at all.
export const HIDDEN_OUTPUTS = ['document_map.json', 'provenance.json'];
export const isNote = (name) => name === 'notes.md' || /(^|_)processing_summary\.txt$/i.test(name);
export const isDeliverable = (name) => !HIDDEN_OUTPUTS.includes(name) && !isNote(name);

// Facts read as one line, joined by commas: "Reads monthly payslips, 2 programs". A full stop ending a fact goes.
export const facts = (...parts) =>
  parts
    .map((part) => String(part || '').trim())
    .filter(Boolean)
    .map((part, i, all) => (i < all.length - 1 ? part.replace(/\.$/, '') : part))
    .join(', ');

export const pill = (tone, label) => `<span class="pill ${tone}">${esc(label)}</span>`;

const monthParts = new Intl.DateTimeFormat('en', { year: 'numeric', month: '2-digit' }).formatToParts(new Date());
const currentPeriod =
  monthParts.find((p) => p.type === 'year').value + '-' + monthParts.find((p) => p.type === 'month').value;

// Whether the plan at the top of a run shows every step: closed unless the person opened it, kept in this browser.
const PLAN_KEY = 'di-plan-open';
function planKept() {
  try {
    return localStorage.getItem(PLAN_KEY) === '1';
  } catch {
    return false; // storage blocked: the plan starts folded
  }
}
export function keepPlanOpen(open) {
  state.planOpen = open;
  try {
    localStorage.setItem(PLAN_KEY, open ? '1' : '0');
  } catch {
    // storage blocked: the choice lasts this visit only
  }
}

export const state = {
  view: 'run', // run | playbooks | playbook | clients | client | settings | log
  playbook: '', // the playbook a new run will use
  book: null, // the playbook page that is open; see playbooks.js
  books: {}, // each playbook opened in this visit, with any edit not yet saved
  clients: [], // every client, with its latest version; see clients.js
  client: null, // the client page that is open
  clientPages: {}, // each client opened in this visit, with any edit not yet saved
  runClient: '', // the client a new run follows, only ever one the person chose
  clientSuggestion: null, // the client the chosen files point to, offered beside the client box
  clientDecided: false, // the person took or turned down the suggestion
  comments: {}, // comments pinned to files and not yet sent, by job id; see comments.js
  commentDraft: null, // the comment being written
  previewMode: 'view', // view: clicks in the preview check things; comment: clicks and drags make comments
  rounds: [], // the rounds of comments already sent on the open run
  roundsKey: '',
  versions: [], // the outputs kept at each save of the open run, newest first; see versions.js
  versionsKey: '',
  showVersions: false, // the stopped card shows the list of earlier versions
  sending: null, // the round of comments being sent, kept until the server has it
  reply: null, // the reply being written under a sent comment: { to: '1.2', text, round_id }
  activeComment: '', // the sent comment last opened from the chat or the list, as '1.2'
  showResolved: false, // the file panel lists the resolved comments too
  menu: null, // the dropdown open in the file panel: 'version', 'since' or 'more'
  links: { key: '', found: {} }, // cells and files named in the agent's words, by text; see links.js
  forgotten: new Set(), // remembered comments taken back out of the playbook in this visit
  config: null,
  job: null,
  jobs: [],
  events: [],
  seen: 0, // highest event number already drawn, so only newer items animate in
  toggled: new Map(), // work blocks, stages and thinking notes the user opened or closed by hand: id to open or not
  act: null, // the action opened in full, by its event number; see thread.js
  actCode: false, // the opened action shows its code
  scripts: {}, // each opened action's code and what it printed, as fetched: { code, output } or { error }
  planOpen: planKept(), // the plan at the top of the run opened in full; kept in this browser for every run
  wrapped: new Set(), // near-limit cards answered with Let it wrap up, as "job id:event number"
  files: [],
  template: null,
  period: '', // a reporting month, for work that has one
  moreOptions: false, // the New run page shows the template, format and month rows
  deliverable: 'template', // a new run fills a template or builds to a format
  instructions: {}, // what the agent should produce, as typed on the New run page, by playbook
  drafts: {},
  attachments: {}, // files waiting to go with the next message, by job id
  open: null, // the file shown in the Files panel
  zoom: { level: 100, fit: false }, // how large the file on show is drawn; see zoom.js
  full: false, // the file on show fills the window
  preview: null, // { path, data, sheet, page, image, error }
  row: null,
  find: null, // the find bar in the preview, while open; see review.js
  // The preview shows what changed since the last round (since: null) or since a version (its number); `step` is the
  // change in focus, for Previous and Next.
  changes: { on: false, only: false, since: null, step: 0, jump: false },
  places: null, // where the selected row was found in its PDF sources
  map: null, // the agent's decision for each file, by stored path
  mapKey: '',
  mapFilter: 'all',
  fileQuery: '', // the find box over the source files
  openFolders: new Set(), // folders unfolded in a long list of sources
  checksOpen: false, // the checks, folded into one line, unfolded
  rail: true, // at full screen with a file open, the list of files beside it
  showFiles: false,
  filter: 'all',
  search: '',
  busy: false,
  polling: false,
  loading: false,
  connectionError: '',
  notice: '',
  token: sessionStorage.getItem('di-token') || '',
  settings: null,
  settingsDraft: null,
  settingsError: '',
  settingsErrors: {}, // a refused value's sentence, by setting, shown beside its field
};

// --- Derived state ---------------------------------------------------------

export const playbooks = () => state.config?.playbooks || [];
export const currentPlaybook = () => playbooks().find((p) => p.id === state.playbook) || playbooks()[0];
export const jobStatus = () => state.job?.status || 'ready';
// Status groups come from the server (/api/config) so the buttons always match what the API allows.
export const inGroup = (group, status = jobStatus()) => !!state.config?.statuses?.[group]?.includes(status);
export const isActive = () => !!state.job && inGroup('active');
export const isWarning = (event) => ['warn', 'warning', 'error'].includes(String(event.level).toLowerCase());

export const STATUS = {
  ready: ['', 'Ready'],
  queued: ['live', 'Queued'],
  running: ['live', 'Working'],
  pausing: ['live', 'Pausing'],
  paused: ['', 'Paused'],
  waiting: ['warn', 'Needs you'],
  completed: ['ok', 'Finished'],
  partial: ['warn', 'Partly done'],
  failed: ['bad', 'Stopped'],
  cancelled: ['', 'Cancelled'],
};

export function jobTitle(job) {
  if (job.name) return job.name; // the name the person gave the run
  // The run list carries the playbook's name; a run made before playbooks carries only its module.
  const named = typeof job.playbook === 'string' ? job.playbook : job.playbook?.name;
  const label = named || playbooks().find((p) => p.id === job.module)?.name || job.module || '';
  const [year, month] = String(job.period || '').split('-');
  const when = month ? new Date(year, month - 1).toLocaleString('en', { month: 'long', year: 'numeric' }) : '';
  // A run without a month is told apart by how its goal begins.
  const goal = String(job.title || job.instructions || '').split('\n')[0].trim();
  if (!when && goal) return goal.length > 48 ? `${goal.slice(0, 47)}…` : goal;
  return `${when} ${label.toLowerCase()}`.trim() || 'Run';
}

// Runs that would read the same in a list get the time they were started beside the name, so each can be told
// apart without opening it. Maps a run's id to that time, or '' when its name is already unique.
const STARTED = new Intl.DateTimeFormat('en', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
const STARTED_EXACTLY = new Intl.DateTimeFormat('en', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }); // prettier-ignore

export function telling(jobs) {
  const titles = jobs.map((j) => jobTitle(j));
  const times = jobs.map((j) => STARTED.format(new Date(j.created_at)));
  const count = (list, i) => list.filter((x, k) => x === list[i] && titles[k] === titles[i]).length;
  return Object.fromEntries(
    jobs.map((j, i) => {
      if (titles.filter((t) => t === titles[i]).length < 2) return [j.id, ''];
      // Two started in the same minute are told apart by the second.
      return [j.id, count(times, i) > 1 ? STARTED_EXACTLY.format(new Date(j.created_at)) : times[i]];
    }),
  );
}

export const baseName = (path) => String(path || '').split('/').pop();
export const extension = (path) => (baseName(path).includes('.') ? baseName(path).split('.').pop() : '');
export const sources = () => (state.job?.files || []).filter((f) => f.role !== 'template');

// The stored file a path refers to. The agent names paths inside the packet, which are the tail of a stored path.
export function storedPath(path) {
  const wanted = String(path || '').replace(/\\/g, '/');
  if (!wanted || !state.job) return null;
  const files = state.job.files || [];
  const hit = files.find((f) => f.path === wanted) || files.find((f) => f.path.endsWith('/' + wanted));
  if (hit) return hit.path;
  const output = (state.job.artifacts || []).find((a) => a.path === wanted || a.name === baseName(wanted));
  return output ? output.path : null;
}

export const fmtTime = (t) =>
  t ? new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
export const bytes = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.ceil((n || 0) / 1024)} KB`);
// A money figure from the cost API: cents, or less than a cent.
export const money = (usd) => (usd >= 0.01 || !usd ? `$${usd.toFixed(2)}` : '<$0.01');
export const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
export function duration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

// --- Network ---------------------------------------------------------------

// A short message at the bottom of the screen. `action` is one button's HTML (an Undo), handled like any other click.
export function toast(message, action = '') {
  const box = $('#toast');
  box.innerHTML = `<span>${esc(message)}</span>${action}`;
  box.classList.toggle('acts', !!action);
  box.classList.add('visible');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(hideToast, action ? 10000 : 6000);
}
export const hideToast = () => $('#toast').classList.remove('visible');

// One Cancel stands for the requests refused with it, so the box does not come straight back for each of them.
let declinedAt = 0;
function askToken() {
  if (Date.now() - declinedAt < 2000) return null;
  const token = window.prompt('Application access token');
  if (!token) declinedAt = Date.now();
  return token;
}

export async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
  // JSON unless the body says otherwise: a form or a file (a recording) carries its own type.
  if (options.body && !(options.body instanceof FormData) && !(options.body instanceof Blob) && !headers.has('Content-Type'))
    headers.set('Content-Type', 'application/json');
  const sent = state.token;
  let res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    // Several requests refused at once ask once: those after the first use the token it was given.
    const token = state.token && state.token !== sent ? state.token : askToken();
    if (token) {
      state.token = token;
      sessionStorage.setItem('di-token', token);
      headers.set('Authorization', `Bearer ${token}`);
      res = await fetch(path, { ...options, headers });
    }
  }
  if (!res.ok) {
    let detail, field;
    try {
      ({ detail, field } = await res.json()); // field: the setting or form field the sentence is about
    } catch {}
    const error = new Error(typeof detail === 'string' ? detail : detail?.message || `Request failed (${res.status})`);
    error.status = res.status;
    error.field = field;
    throw error;
  }
  return res;
}

export async function api(path, options = {}) {
  const res = await request(path, options);
  return res.status === 204 ? null : res.json();
}

// A file fetched with the access token, as a link the page can show in an <img> or hand to the browser to save.
export async function blobUrl(path) {
  return URL.createObjectURL(await (await request(path)).blob());
}

export async function download(path, name) {
  const url = await blobUrl(path);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

export const jobUrl = (rest = '') => `/api/jobs/${encodeURIComponent(state.job.id)}${rest}`;
export const fileUrl = (route, path, more = '') => jobUrl(`/${route}?path=${encodeURIComponent(path)}${more}`);
