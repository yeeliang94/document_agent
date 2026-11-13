// The Files panel: the run's outputs, checks and source files, and a viewer to look inside any of them.

import {
  api, baseName, blobUrl, esc, extension, fileUrl, icon, isActive, jobUrl, pill, plural, sources,
  state, storedPath,
} from './core.js'; // prettier-ignore

const DECISIONS = {
  used: ['ok', 'Used'],
  reference: ['', 'Reference'],
  not_relevant: ['', 'Not relevant'],
  unclear: ['warn', 'Unclear'],
};
const HIDDEN_OUTPUTS = ['document_map.json', 'provenance.json'];

// --- What the agent decided about each source file -------------------------

// The sandbox engine saves its file-by-file decisions with the outputs. Reload them when that file changes.
export async function loadMap() {
  const job = state.job;
  const saved = job?.artifacts?.find((a) => a.name === 'document_map.json');
  const key = saved ? `${job.id}:${saved.size}` : '';
  if (key === state.mapKey) return false;
  state.mapKey = key;
  state.map = null;
  if (!saved) return true;
  try {
    const { files } = await api(jobUrl('/document-map'));
    if (state.job?.id !== job.id) return false;
    state.map = {};
    for (const f of files) {
      const stored = storedPath(f.path);
      if (stored) state.map[stored] = f;
    }
  } catch {
    state.mapKey = ''; // try again on the next refresh
  }
  return true;
}

// Files the agent has opened so far, and the ones it is on right now, read from its activity.
function touched() {
  const read = new Set();
  const reading = new Set();
  const done = new Set(state.events.filter((e) => e.type === 'tool' && e.status !== 'running').map((e) => e.span_id));
  for (const e of state.events) {
    if (e.type === 'tool' && e.arguments?.path) {
      const stored = storedPath(e.arguments.path);
      if (stored) (e.status === 'running' && !done.has(e.span_id) && isActive() ? reading : read).add(stored);
    }
    for (const path of Array.isArray(e.files) && e.type === 'engine' ? e.files : []) {
      const stored = storedPath(path);
      if (stored) read.add(stored);
    }
  }
  return { read, reading };
}

function sourcePill(path, seen) {
  const entry = state.map?.[path];
  if (entry) {
    const [tone, label] = DECISIONS[entry.decision] || ['warn', entry.decision || 'Undecided'];
    return pill(tone, label);
  }
  if (seen.reading.has(path)) return pill('live', 'Reading');
  return seen.read.has(path) ? pill('', 'Read') : '';
}

// --- The list --------------------------------------------------------------

const row = (path, name, right = '') =>
  `<button class="frow" data-open="${esc(path)}" title="${esc(path)}"><span class="tag">${esc(extension(name))}</span><span class="name">${esc(name)}</span>${right}</button>`;
const fact = (label, value) => `<div class="frow"><span class="name">${esc(label)}</span>${value}</div>`;

function checksHtml(job) {
  const check = job.engine_check;
  if (check) {
    const s = check.summary || {};
    const ok = (good, text) => pill(good ? 'ok' : 'warn', text);
    const rows = [
      fact('Template rules', s.deliverable_problems === undefined ? pill('', 'Not checked') : ok(!s.deliverable_problems, s.deliverable_problems ? plural(s.deliverable_problems, 'problem') : 'Passed')),
      fact('Files decided', s.files === undefined ? pill('', 'Not checked') : ok(s.files_decided === s.files, `${s.files_decided} of ${s.files}`)),
    ];
    if (s.rows_written != null) rows.push(fact('Rows with a source', ok(s.rows_sourced === s.rows_written && s.rows_written > 0, `${s.rows_sourced} of ${s.rows_written}`)));
    rows.push(fact('Reviewer', check.reviews ? ok(true, check.doubts ? `${plural(check.doubts, 'doubt')} checked` : 'No doubts') : pill('', 'Not run')));
    const issues = !check.passed && check.issues?.length ? `<ul class="fix">${check.issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>` : '';
    return `<div class="list"><h3>Checks</h3>${rows.join('')}${issues}</div>`;
  }
  const records = job.coverage?.records;
  if (!records || !Object.values(records).some(Boolean)) return '';
  const open = records.pending + records.unresolved;
  return `<div class="list"><h3>Checks</h3>
    ${fact('Rows to resolve', pill(open ? 'warn' : 'ok', open ? String(open) : 'None'))}
    ${fact('Open items', pill(job.coverage.open_items ? 'warn' : 'ok', job.coverage.open_items ? String(job.coverage.open_items) : 'None'))}</div>`;
}

function listHtml(job) {
  const outputs = (job.artifacts || []).filter((a) => !HIDDEN_OUTPUTS.includes(a.name));
  const outputRows = outputs.length
    ? outputs.map((a) => row(a.path, a.name, a.status === 'validated' ? pill('ok', 'Checked') : a.status === 'draft' ? pill('warn', 'Draft') : '')).join('')
    : '<div class="empty">Nothing yet</div>';
  const template = (job.files || []).find((f) => f.role === 'template');
  const seen = touched();
  const all = sources();

  // With decisions in hand, offer them as a filter: this replaces the separate document map page.
  const counts = {};
  for (const f of all) {
    const decision = state.map?.[f.path]?.decision;
    if (decision) counts[decision] = (counts[decision] || 0) + 1;
  }
  const choices = Object.keys(DECISIONS).filter((d) => counts[d]);
  if (state.mapFilter !== 'all' && !counts[state.mapFilter]) state.mapFilter = 'all';
  const filter = choices.length > 1
    ? `<div class="filter">${[['all', 'All'], ...choices.map((d) => [d, `${DECISIONS[d][1]} ${counts[d]}`])]
        .map(([id, label]) => `<button class="${state.mapFilter === id ? 'on' : ''}" data-map-filter="${id}">${esc(label)}</button>`)
        .join('')}</div>`
    : '';
  const shown = all.filter((f) => state.mapFilter === 'all' || state.map?.[f.path]?.decision === state.mapFilter);
  // Folder headings drop the part every file shares (the upload folder and the zip's own name).
  const dirs = all.map((f) => f.path.split('/').slice(0, -1));
  let shared = dirs[0] || [];
  for (const d of dirs) {
    let same = 0;
    while (same < shared.length && d[same] === shared[same]) same += 1;
    shared = shared.slice(0, same);
  }
  // A folder heading only where it groups two or more files; a lone file keeps its folder in the tooltip.
  const dirOf = (f) => f.path.split('/').slice(shared.length, -1).join('/');
  const size = {};
  for (const f of shown) size[dirOf(f)] = (size[dirOf(f)] || 0) + 1;
  let folder = null;
  const sourceRows = shown
    .map((f) => {
      const dir = size[dirOf(f)] > 1 ? dirOf(f) : '';
      const head = dir !== folder && dir ? `<div class="folder">${esc(dir)}</div>` : '';
      folder = dir;
      return head + row(f.path, baseName(f.path), sourcePill(f.path, seen));
    })
    .join('');
  // The sandbox engine's "read" marks come from the text of its scripts, so they are a hint and not a count.
  const counted = !state.map && job.engine !== 'sandbox';
  const progress = counted ? `${all.filter((f) => seen.read.has(f.path)).length} of ${all.length} read` : `${all.length}`;
  return `
    <div class="files-head"><h2>Files</h2><button class="icon-btn only-narrow" data-action="toggle-files" aria-label="Close">${icon('x')}</button></div>
    <div class="files-body">
      <div class="list"><h3>Output</h3>${outputRows}</div>
      ${checksHtml(job)}
      ${template ? `<div class="list"><h3>${/\.json$/i.test(template.path) ? 'Format' : 'Template'}</h3>${row(template.path, baseName(template.path))}</div>` : ''}
      <div class="list"><h3><span>Sources</span><span>${progress}</span></h3>${filter}${sourceRows}</div>
    </div>`;
}

// --- The viewer ------------------------------------------------------------

function cell(value) {
  if (value == null) return '<td></td>';
  if (typeof value === 'number') return `<td class="num">${esc(value.toLocaleString('en', { maximumFractionDigits: 6 }))}</td>`;
  return `<td title="${esc(value)}">${esc(value)}</td>`;
}

function sheetHtml(data) {
  const picks = data.row_sources && Object.keys(data.row_sources).length > 0;
  const tabs = data.sheets.length > 1
    ? `<div class="filter">${data.sheets.map((s) => `<button class="${s.name === data.sheet ? 'on' : ''}" data-sheet="${esc(s.name)}">${esc(s.name)}</button>`).join('')}</div>`
    : '';
  const hidden = data.sheets.find((s) => s.name === data.sheet)?.hidden;
  const meta = [hidden ? 'Hidden sheet' : '', data.more ? 'Showing the first 500 rows' : '', picks ? 'Select a row to see its source files' : '']
    .filter(Boolean)
    .map((t) => `<span>${t}</span>`)
    .join('');
  const grid = data.rows.length
    ? `<div class="sheet"><table class="grid ${picks ? 'pick' : ''}"><thead><tr><th></th>${data.columns.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>
        ${data.rows.map((r, i) => `<tr ${picks ? `data-row="${i + 1}" tabindex="0"` : ''} class="${picks && state.row === i + 1 ? 'sel' : ''}"><td>${i + 1}</td>${data.columns.map((_, c) => cell(r[c])).join('')}</tr>`).join('')}
      </tbody></table></div>`
    : '<p class="empty">This sheet is empty.</p>';
  return `${tabs || meta ? `<div class="meta">${meta}${tabs}</div>` : ''}${grid}<div class="proof list" id="proof">${picks ? proofHtml() : ''}</div>`;
}

// The uploaded files behind the selected row of an output sheet.
export function proofHtml() {
  const files = state.preview?.data?.row_sources?.[state.row];
  if (!state.row) return '';
  return `<h2>Row ${state.row} came from</h2>${
    files?.length
      ? files.map((p) => (storedPath(p) ? row(storedPath(p), baseName(p)) : `<div class="frow"><span class="name">${esc(p)}</span></div>`)).join('')
      : '<div class="empty">No source file is recorded for this row.</div>'
  }`;
}

function bodyHtml(view) {
  if (view.error) return `<p class="empty problem">${esc(view.error)}</p>`;
  const data = view.data;
  if (!data) return '<p class="empty">Loading</p>';
  if (data.kind === 'sheet') return sheetHtml(data);
  if (data.kind === 'text') return `<pre class="doc">${esc(data.text)}</pre>${data.more ? '<p class="meta">The rest of the file is not shown. Download it to read everything.</p>' : ''}`;
  if (data.kind === 'pages' || data.kind === 'image') {
    const shot = `<div class="shot">${view.image ? `<img src="${view.image}" alt="${esc(data.name)}${data.kind === 'pages' ? `, page ${view.page}` : ''}">` : '<span class="empty">Loading</span>'}</div>`;
    const pager = data.kind === 'pages' && data.pages > 1
      ? `<div class="pager"><button class="icon-btn" data-page="${view.page - 1}" aria-label="Previous page" ${view.page <= 1 ? 'disabled' : ''}>${icon('back')}</button>
          Page ${view.page} of ${data.pages}<button class="icon-btn" data-page="${view.page + 1}" aria-label="Next page" ${view.page >= data.pages ? 'disabled' : ''}>${icon('chev')}</button></div>`
      : '';
    return shot + pager;
  }
  return `<p class="empty">${data.error ? 'This file could not be opened for preview.' : 'There is no preview for this kind of file.'} Download it to look inside.</p>`;
}

function viewerHtml(view) {
  const entry = state.map?.[view.path];
  const artifact = (state.job.artifacts || []).find((a) => a.path === view.path);
  let about = '';
  if (entry) {
    const [tone, label] = DECISIONS[entry.decision] || ['warn', entry.decision || 'Undecided'];
    about = `<div class="meta">${pill(tone, label)}<span>${esc(entry.reason)}</span></div>`;
  } else if (artifact?.status) about = `<div class="meta">${artifact.status === 'validated' ? pill('ok', 'Checked') : pill('warn', 'Draft')}</div>`;
  return `
    <div class="files-head"><button class="icon-btn" data-action="back-file" aria-label="${view.back ? `Back to ${esc(baseName(view.back.path))}` : 'Back to files'}">${icon('back')}</button>
      <h2 title="${esc(view.path)}">${esc(baseName(view.path))}</h2>
      <button class="icon-btn" data-download="${esc(view.path)}" aria-label="Download">${icon('down')}</button>
      <button class="icon-btn" data-action="close-file" aria-label="Close">${icon('x')}</button></div>
    <div class="files-body">${about}${bodyHtml(view)}</div>`;
}

export function filesHtml() {
  return state.open && state.preview ? viewerHtml(state.preview) : listHtml(state.job);
}

// --- Opening a file --------------------------------------------------------

function dropImage() {
  if (state.preview?.image) URL.revokeObjectURL(state.preview.image);
}

export function closeFile() {
  dropImage();
  state.open = state.preview = state.row = null;
}

// Loads in steps and calls `draw` after each, so the panel opens at once and fills in.
// `back` remembers the output and row a source was opened from, so Back returns there.
export async function openFile(path, draw, { sheet = null, row = null, back = null } = {}) {
  dropImage();
  const view = { path, data: null, page: 1, image: null, error: '', back };
  state.open = path;
  state.preview = view;
  state.showFiles = true;
  state.row = row;
  draw();
  const current = () => state.preview === view;
  try {
    view.data = await api(fileUrl('preview', path, sheet ? `&sheet=${encodeURIComponent(sheet)}` : ''));
    if (!current()) return;
    draw();
    if (view.data.kind === 'image') view.image = await blobUrl(fileUrl('file', path));
    else if (view.data.kind === 'pages') view.image = await blobUrl(fileUrl('page', path, '&page=1'));
    else return;
    if (!current()) return URL.revokeObjectURL(view.image);
  } catch (error) {
    if (!current()) return;
    view.error = error.message;
  }
  draw();
}

export function backFile(draw) {
  const back = state.preview?.back;
  if (!back) {
    closeFile();
    return draw();
  }
  return openFile(back.path, draw, { sheet: back.sheet, row: back.row });
}

export async function turnPage(page, draw) {
  const view = state.preview;
  if (!view?.data || page < 1 || page > view.data.pages) return;
  try {
    const image = await blobUrl(fileUrl('page', view.path, `&page=${page}`));
    if (state.preview !== view) return URL.revokeObjectURL(image);
    dropImage();
    Object.assign(view, { page, image });
  } catch (error) {
    view.error = error.message;
  }
  draw();
}
