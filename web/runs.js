// The run list in the sidebar: each run's own name and status, and a menu to rename or delete it.

import { $, STATUS, api, esc, icon, inGroup, jobTitle, pill, state, toast } from './core.js';

const STILL_GOING = 'This run is still going. Stop it before you delete it.';

const statusOf = (job) => (job.id === state.job?.id ? state.job.status : job.status);
const listed = (id) => state.jobs.find((j) => j.id === id) || (state.job?.id === id ? state.job : null);

export function runItems() {
  return state.jobs.map((j) => {
    const status = statusOf(j);
    const [tone, label] = status === 'waiting' ? ['warn', 'Needs you'] : STATUS[status] || ['', status];
    const on = j.id === state.job?.id && ['run', 'log'].includes(state.view);
    const open = state.menu === `run:${j.id}`;
    const title = jobTitle(j);
    const menu = open
      ? `<div class="drop" role="menu" aria-label="${esc(title)}">
          <button type="button" role="menuitem" data-run-rename="${esc(j.id)}">Rename</button>
          <button type="button" role="menuitem" data-run-delete="${esc(j.id)}">Delete</button></div>`
      : '';
    return `<div class="item run ${on ? 'on' : ''}" data-job="${esc(j.id)}">
      <button type="button" class="run-open" ${on ? 'aria-current="page"' : ''}><span>${esc(title)}</span></button>${pill(tone, label)}
      <span class="menu-wrap"><button type="button" class="icon-btn run-more" data-menu="run:${esc(j.id)}" aria-haspopup="menu" aria-expanded="${open}" aria-label="More for ${esc(title)}" title="More">${icon('more')}</button>${menu}</span>
    </div>`;
  });
}

function dialog(title, body, does, id, label) {
  $('#details').innerHTML = `<div class="dialog-head"><h2>${esc(title)}</h2><button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
    <form class="dialog-body" id="run-form" data-does="${does}" data-id="${esc(id)}">${body}
      <div class="end"><button type="button" class="btn text" data-close>Cancel</button><button class="btn primary">${esc(label)}</button></div></form>`;
  $('#details').showModal();
}

export function renameRun(id) {
  const job = listed(id);
  if (!job) return;
  // An empty name goes back to the one made from the run's goal, which the box shows as its placeholder.
  const made = jobTitle({ ...job, name: null });
  dialog(
    'Rename run',
    `<label>Name<input class="field" name="name" value="${esc(job.name || '')}" placeholder="${esc(made)}" maxlength="80" autocomplete="off"></label>`,
    'rename',
    id,
    'Rename',
  );
  $('#run-form input').select();
}

export function deleteRun(id) {
  const job = listed(id);
  if (!job) return;
  if (inGroup('active', statusOf(job))) return toast(STILL_GOING);
  dialog(`Delete "${jobTitle(job)}"?`, '<p>Its files, results and history are removed for good.</p>', 'delete', id, 'Delete');
}

// Start over sets the current files aside (they are kept as a version first), so it asks before it does.
export function confirmStartOver(id) {
  dialog('Start over?', '<p>The agent does the job again from the beginning. The current files are kept as a version.</p>', 'start-over', id, 'Start over');
}

// The rename, delete or start over the dialog asked for. Returns the id of a run that was deleted, so the caller can leave it.
export async function submitRun(form) {
  const { does, id } = form.dataset;
  $('#details').close();
  const url = `/api/jobs/${encodeURIComponent(id)}`;
  if (does === 'rename') {
    const job = await api(url, { method: 'PATCH', body: JSON.stringify({ name: new FormData(form).get('name') }) });
    if (state.job?.id === id) state.job.name = job.name;
  } else if (does === 'start-over') {
    await api(`${url}/start-over`, { method: 'POST' });
    return null;
  } else await api(url, { method: 'DELETE' });
  state.jobs = await api('/api/jobs');
  return does === 'delete' ? id : null;
}
