// The versions of a run's outputs, one kept at every save, newest first. One list, shown on the card of a run that
// stopped and (plan Step 33) in the file panel's Version menu, so both read and act the same way.

import { api, attr, esc, icon, jobUrl, pill, state } from './core.js';

// Reloaded when a run keeps a new version or changes state, so the list always matches the files.
export async function loadVersions() {
  const job = state.job;
  const kept = state.events.filter((e) => e.name === 'outputs.versioned').length;
  const key = job ? `${job.id}:${job.status}:${kept}` : '';
  if (key === state.versionsKey) return;
  state.versionsKey = key;
  if (!job) return void (state.versions = []);
  try {
    const { versions } = await api(jobUrl('/versions'));
    if (state.job?.id === job.id) state.versions = versions;
  } catch {
    state.versionsKey = ''; // try again on the next refresh
  }
}

// What a version holds, without the step its line starts with: "saved pay.xlsx; checks passed".
export const versionWhat = (v) => String(v.description || '').replace(/^Step \d+:?\s*/, '');

// "Step 9: saved pay.xlsx; checks passed" reads as "Step 9 · saved pay.xlsx; checks passed".
export const versionName = (v) => [`Step ${v.step}`, versionWhat(v)].filter(Boolean).join(' · ');

const at = (v) => (v.at ? new Date(v.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '');

// One row per version: the newest is marked Latest, any other can be continued from.
export function versionRows(list = state.versions || []) {
  return list
    .map(
      (v, i) => `<div class="row"><span class="name">${esc(versionName(v))}</span><span class="when">${esc(at(v))}</span>
        <span class="act">${i === 0 ? pill('ok', 'Latest') : `<button class="btn quiet small" data-continue="${v.number}" ${attr('disabled', state.busy)}>Continue from here</button>`}</span></div>`,
    )
    .join('');
}

// The versions that hold one output, newest first. Only outputs are kept.
export function versionsOf(path) {
  const name = String(path || '').replace(/^outputs\//, '');
  if (!String(path || '').startsWith('outputs/')) return [];
  return (state.versions || []).filter((v) => !Array.isArray(v.files) || v.files.includes(name));
}

// The Version menu in the file panel's header: the same versions as the list above, each opening the file as that
// version kept it. `shown` is the version on show, null for the latest.
export function versionMenuHtml(path, shown) {
  const list = versionsOf(path);
  if (list.length < 2) return '';
  const current = shown ?? list[0].number;
  const open = state.menu === 'version';
  const what = (v) => versionWhat(v).replace(/^./, (c) => c.toUpperCase());
  const items = list
    .map(
      (v, i) => `<button role="menuitemradio" aria-checked="${v.number === current}" data-pick-version="${i === 0 ? 'latest' : v.number}">
        <span class="v">Version ${v.number}${what(v) ? ` · ${esc(what(v))}` : ''}</span><span class="when">${esc(at(v))}</span></button>`,
    )
    .join('');
  return `<span class="menu-wrap"><button type="button" class="picker" data-menu="version" aria-haspopup="menu" aria-expanded="${open}">Version ${current}${icon('down1')}</button>
    ${open ? `<div class="drop" role="menu" aria-label="Version">${items}</div>` : ''}</span>`;
}

// Make a kept version the outputs again and start a run that carries on from it.
export const continueFrom = (number) => api(jobUrl(`/versions/${number}/continue`), { method: 'POST' });
