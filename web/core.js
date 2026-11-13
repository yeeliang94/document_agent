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
  down: 'M12 4v12M7 11l5 5 5-5M5 20h14',
  clip: 'M20 11l-8.5 8.5a5 5 0 01-7-7L13 4a3.5 3.5 0 015 5l-8.5 8.5a2 2 0 01-3-3L14 7',
};
export const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${icons[name]}"/></svg>`;
export const btn = (action, label, kind = 'quiet', disabled = false, glyph = '') =>
  `<button class="btn ${kind}" data-action="${action}" ${attr('disabled', disabled)}>${glyph ? icon(glyph) : ''}${esc(label)}</button>`;
export const pill = (tone, label) => `<span class="pill ${tone}">${esc(label)}</span>`;

const monthParts = new Intl.DateTimeFormat('en', { year: 'numeric', month: '2-digit' }).formatToParts(new Date());
const currentPeriod =
  monthParts.find((p) => p.type === 'year').value + '-' + monthParts.find((p) => p.type === 'month').value;

export const state = {
  view: 'run', // run | instructions | settings | log
  module: 'payroll',
  config: null,
  job: null,
  jobs: [],
  events: [],
  seen: 0, // highest event number already drawn, so only newer items animate in
  toggled: new Set(), // work blocks the user opened or closed by hand
  files: [],
  template: null,
  period: currentPeriod,
  deliverable: 'template', // a new sandbox run fills a template or builds to a format
  instructions: {},
  contexts: {},
  drafts: {},
  open: null, // the file shown in the Files panel
  preview: null, // { path, data, sheet, page, image, error }
  row: null,
  map: null, // the sandbox engine's decision for each file, by packet path
  mapKey: '',
  mapFilter: 'all',
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
};

// --- Derived state ---------------------------------------------------------

export const modules = () => state.config?.modules || [];
export const currentModule = () => modules().find((m) => m.id === state.module) || modules()[0];
export const jobStatus = () => state.job?.status || 'ready';
// Status groups come from the server (/api/config) so the buttons always match what the API allows.
export const inGroup = (group, status = jobStatus()) => !!state.config?.statuses?.[group]?.includes(status);
export const isActive = () => !!state.job && inGroup('active');
export const isWarning = (event) => ['warn', 'warning', 'error'].includes(String(event.level).toLowerCase());
export const sandboxEngine = () =>
  state.job ? state.job.engine === 'sandbox' : state.settings?.values.engine === 'sandbox';

export const STATUS = {
  ready: ['', 'Ready'],
  queued: ['live', 'Queued'],
  running: ['live', 'Working'],
  pausing: ['live', 'Pausing'],
  paused: ['', 'Paused'],
  waiting: ['warn', 'Needs your answer'],
  completed: ['ok', 'Finished'],
  partial: ['warn', 'Partly done'],
  failed: ['bad', 'Stopped'],
  cancelled: ['', 'Cancelled'],
};

export function jobTitle(job) {
  const label = modules().find((m) => m.id === job.module)?.label || job.module || '';
  const [year, month] = String(job.period || '').split('-');
  const when = month ? new Date(year, month - 1).toLocaleString('en', { month: 'long', year: 'numeric' }) : '';
  return `${when} ${label.toLowerCase()}`.trim() || 'Run';
}

export const baseName = (path) => String(path || '').split('/').pop();
export const extension = (path) => (baseName(path).includes('.') ? baseName(path).split('.').pop() : '');
export const sources = () => (state.job?.files || []).filter((f) => f.role !== 'template');

// The stored file a path refers to. The classic engine names stored paths; the sandbox engine names paths inside
// the packet, which are the tail of a stored path.
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
export const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
export function duration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

// --- Network ---------------------------------------------------------------

export function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.add('visible');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 6000);
}

async function request(path, options = {}) {
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
