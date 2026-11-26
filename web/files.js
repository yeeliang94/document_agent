// The Files panel: the run's outputs, checks and source files, and a viewer to look inside any of them.

import {
  api, attr, baseName, HIDDEN_OUTPUTS, isDeliverable, isNote, nameHtml, blobUrl, esc, extension, fileUrl, icon, inGroup, isActive, jobUrl, pill, plural, sources,
  state, storedPath,
} from './core.js'; // prettier-ignore
import {
  barHtml, blockCells, canComment, changedSpots, columnName, commenting, isBox, pendingHtml, pin, pinsOn, sentHtml, sentPin, sentPinsOn,
} from './comments.js'; // prettier-ignore
import {
  canFind, changedOnly, changeInFocus, changesBarHtml, changesOn, findBarHtml, isFound, loadChanges, marked, pageMarksHtml,
  placeHtml, rowProof, searchPdf, startMarks,
} from './review.js'; // prettier-ignore
import { versionMenuHtml } from './versions.js';
import { inline, lineLook, lineRoles, plainFor } from './markdown.js';
import { fullHtml, resetZoom, zoomHtml } from './zoom.js';

const DECISIONS = {
  used: ['ok', 'Used'],
  reference: ['', 'Reference'],
  not_relevant: ['', 'Not relevant'],
  unclear: ['warn', 'Unclear'],
};

// --- What the agent decided about each source file -------------------------

// The agent saves its file-by-file decisions with the outputs. Reload them when that file changes.
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

// Files the agent has opened so far, read from its activity: the files its scripts name, and for runs made by
// the earlier engine, the file each tool worked on.
function touched() {
  const read = new Set();
  for (const e of state.events) {
    const paths = e.type === 'engine' && Array.isArray(e.files) ? e.files : e.type === 'tool' ? [e.arguments?.path] : [];
    for (const path of paths) {
      const stored = storedPath(path);
      if (stored) read.add(stored);
    }
  }
  return read;
}

function sourcePill(path, seen) {
  const entry = state.map?.[path];
  if (entry) {
    const [tone, label] = DECISIONS[entry.decision] || ['warn', entry.decision || 'Undecided'];
    return pill(tone, label);
  }
  return seen.has(path) ? pill('', 'Read') : '';
}

// --- The list --------------------------------------------------------------

// One file in the list. `current` marks the file on show (in the list beside a file at full screen).
const row = (path, name, right = '', { dim = false, query = '' } = {}) =>
  `<button class="frow ${dim ? 'dim' : ''}" data-open="${esc(path)}" title="${esc(path)}" aria-current="${state.open === path}"><span class="tag">${esc(extension(name))}</span><span class="name">${nameHtml(name, query)}</span>${right}</button>`;

// The checks, folded into one line that says how they went; unfolded, one line each.
function checksHtml(job) {
  const check = job.engine_check;
  if (!check) return '';
  const s = check.summary || {};
  // Each check as [label, tone, text]: tone 'ok', 'warn' or '' (not checked).
  const ok = (good, text) => [good ? 'ok' : 'warn', text];
  const facts = [
    ['Template rules', ...(s.deliverable_problems === undefined ? ['', 'Not checked'] : ok(!s.deliverable_problems, s.deliverable_problems ? plural(s.deliverable_problems, 'problem') : 'Passed'))],
    ['Totals add up', ...(s.totals === undefined ? ['', 'Not checked'] : !s.totals && !s.totals_problems ? ['', 'No totals'] : ok(!s.totals_problems, s.totals_problems ? plural(s.totals_problems, 'problem') : 'Passed'))],
    ['Files decided', ...(s.files === undefined ? ['', 'Not checked'] : ok(s.files_decided === s.files, `${s.files_decided} of ${s.files}`))],
  ];
  if (s.rows_written != null) facts.push(['Rows with a source', ...ok(s.rows_sourced === s.rows_written && s.rows_written > 0, `${s.rows_sourced} of ${s.rows_written}`)]);
  facts.push(['Reviewer', ...(check.reviews ? ok(true, check.doubts ? `${plural(check.doubts, 'doubt')} checked` : 'No doubts') : ['', 'Not run'])]);
  const warned = facts.filter(([, tone]) => tone === 'warn').length;
  const verdict = check.passed ? pill('ok', 'All passed') : warned ? pill('warn', `${warned} to look at`) : pill('warn', 'Not passed');
  // A finished run lists what is still to fix in its report, so the list is shown once, there. Otherwise it stays
  // in view, folded or not: it is what the person most needs from the checks.
  const issues = !check.passed && check.issues?.length && !inGroup('finished', job.status) ? `<ul class="fix">${check.issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>` : '';
  const open = state.checksOpen;
  const rows = facts.map(([label, tone, text]) => `<div><span>${esc(label)}</span>${pill(tone, text)}</div>`).join('');
  return `<div class="list"><button class="fold checks" data-action="toggle-checks" aria-expanded="${open}">${icon('chev')}<span class="name">Checks</span>${verdict}</button>
    ${open ? `<div class="check-list">${rows}</div>` : ''}${issues}</div>`;
}

// Short names for folders, keeping the parts that tell them apart. Folders that start alike and are as deep are
// compared: a part they all share (payroll / 2026 / Q4) folds into "…", keeping the first and last part. A folder
// with nothing to compare against keeps its first two parts and its last; two that would still read the same show
// their whole paths. The full path is in the tooltip.
function folderLabels(dirs) {
  const labels = {};
  const kin = {};
  for (const dir of new Set(dirs)) {
    const parts = dir.split('/');
    (kin[`${parts[0]}|${parts.length}`] ||= []).push(parts);
  }
  for (const group of Object.values(kin)) {
    for (const parts of group) {
      let shown = parts;
      if (parts.length > 3) {
        const differs = parts.map((p, i) => i === 0 || i === parts.length - 1 || group.some((o) => o[i] !== p));
        if (group.length === 1) shown = [parts[0], parts[1], '…', parts.at(-1)];
        else shown = parts.reduce((out, p, i) => (differs[i] ? [...out, p] : out.at(-1) === '…' ? out : [...out, '…']), []);
      }
      labels[parts.join('/')] = shown.join(' / ');
    }
  }
  const count = {};
  for (const label of Object.values(labels)) count[label] = (count[label] || 0) + 1;
  for (const dir of Object.keys(labels)) if (count[labels[dir]] > 1) labels[dir] = dir.split('/').join(' / ');
  return labels;
}

const MANY = 40; // past this many sources, folders start folded

// The source files: a find box, a filter by the agent's decision, and folders, folded when there are many.
function sourcesHtml({ pills }) {
  const all = sources();
  const seen = pills ? touched() : null;
  const query = state.fileQuery.trim();
  const q = query.toLowerCase();
  const counts = {};
  for (const f of all) {
    const decision = state.map?.[f.path]?.decision;
    if (decision) counts[decision] = (counts[decision] || 0) + 1;
  }
  const choices = Object.keys(DECISIONS).filter((d) => counts[d]);
  if (state.mapFilter !== 'all' && !counts[state.mapFilter]) state.mapFilter = 'all';
  const chip = (id, label) => `<button data-map-filter="${id}" aria-pressed="${state.mapFilter === id}">${esc(label)}</button>`;
  const chips = choices.length > 1 ? `<div class="chips" role="group" aria-label="Show">${chip('all', `All ${all.length}`)}${choices.map((d) => chip(d, `${DECISIONS[d][1]} ${counts[d]}`)).join('')}</div>` : '';
  const find = all.length > 8
    ? `<label class="source-find">${icon('find')}<input id="files-q" type="search" value="${esc(state.fileQuery)}" placeholder="Find a source" aria-label="Find a source" autocomplete="off"></label>`
    : '';
  const shown = all.filter((f) => (state.mapFilter === 'all' || state.map?.[f.path]?.decision === state.mapFilter) && (!q || f.path.toLowerCase().includes(q)));

  // Folder headings drop the part every file shares (the upload folder and the zip's own name).
  const dirs = all.map((f) => f.path.split('/').slice(0, -1));
  let shared = dirs[0] || [];
  for (const d of dirs) {
    let same = 0;
    while (same < shared.length && d[same] === shared[same]) same += 1;
    shared = shared.slice(0, same);
  }
  const dirOf = (f) => f.path.split('/').slice(shared.length, -1).join('/');
  const many = all.length > MANY;
  const size = {};
  for (const f of all) size[dirOf(f)] = (size[dirOf(f)] || 0) + 1;
  // A few files: a heading only where a folder groups two or more, and a lone file keeps its folder in the tooltip.
  // Many files: every folder is a line that folds, and a find opens the folders it matches in.
  const groups = [];
  for (const f of shown) {
    const dir = many || size[dirOf(f)] > 1 ? dirOf(f) : '';
    if (groups.at(-1)?.dir !== dir) groups.push({ dir, files: [] });
    groups.at(-1).files.push(f);
  }
  const fileRows = (files) => files.map((f) => {
    const decision = state.map?.[f.path]?.decision;
    return row(f.path, baseName(f.path), pills ? sourcePill(f.path, seen) : '', { dim: decision === 'not_relevant', query });
  }).join('');
  const short = folderLabels(all.map(dirOf).filter(Boolean)); // from every folder, so a filter does not change them
  const body = groups.map(({ dir, files }) => {
    if (!dir) return fileRows(files);
    if (!many) return `<div class="folder" title="${esc(dir)}">${esc(short[dir])}</div>${fileRows(files)}`;
    const open = !!q || state.openFolders.has(dir);
    return `<button class="fold" data-fold="${esc(dir)}" aria-expanded="${open}" title="${esc(dir)}">${icon('chev')}<span class="name">${esc(short[dir])}</span><span class="count">${files.length}</span></button>
      ${open ? `<div class="inside">${fileRows(files)}</div>` : ''}`;
  }).join('');
  const count = shown.length === all.length ? `${all.length}` : `${shown.length} of ${all.length}`;
  return `<div class="list sources"><h3><span>Sources</span><span class="count">${count}</span></h3>
    ${find || chips ? `<div class="sift">${find}${chips}</div>` : ''}
    ${body || `<div class="empty">${all.length ? 'No source matches.' : 'None'}</div>`}</div>`;
}

// Every file of the run, by kind. `rail` is the list beside an open file at full screen: no checks, no pills.
function groupsHtml(job, { rail = false } = {}) {
  const outputs = (job.artifacts || []).filter((a) => !HIDDEN_OUTPUTS.includes(a.name));
  const made = outputs.filter((a) => isDeliverable(a.name));
  const notes = outputs.filter((a) => isNote(a.name));
  const status = (a) => (rail ? '' : a.status === 'validated' ? pill('ok', 'Checked') : a.status === 'draft' ? pill('warn', 'Draft') : '');
  const template = (job.files || []).find((f) => f.role === 'template');
  return `
    <div class="list"><h3>Outputs</h3>${made.length ? made.map((a) => row(a.path, a.name, status(a))).join('') : '<div class="empty">Nothing yet</div>'}</div>
    ${notes.length ? `<div class="list"><h3>Agent's notes</h3>${notes.map((a) => row(a.path, a.name)).join('')}</div>` : ''}
    ${rail ? '' : checksHtml(job)}
    ${template ? `<div class="list"><h3>${/\.json$/i.test(template.path) ? 'Format' : 'Template'}</h3>${row(template.path, baseName(template.path))}</div>` : ''}
    ${sourcesHtml({ pills: state.full && !rail })}`;
}

function listHtml(job) {
  return `
    <div class="files-head"><h2>Files</h2>${fullHtml()}<button class="icon-btn only-narrow" data-action="toggle-files" aria-label="Close">${icon('x')}</button></div>
    <div class="files-body">
      ${pendingHtml()}
      ${groupsHtml(job)}
    </div>${barHtml()}`;
}

// --- The viewer ------------------------------------------------------------

// A colour from the file, only if it is a plain #rrggbb, so nothing else can reach the style attribute.
const hex = (c) => (/^#[0-9a-f]{6}$/i.test(c || '') ? c : '');
const BORDER = /^[1-3]px (solid|double) #[0-9a-f]{6}$/i;

// One of the sheet's styles from the file (bold, colours, alignment, borders) as an inline style.
function lookStyle(look) {
  const css = [];
  if (look.bold) css.push('font-weight:500');
  if (look.italic) css.push('font-style:italic');
  const lines = [look.underline ? 'underline' : '', look.strike ? 'line-through' : ''].filter(Boolean).join(' ');
  if (lines) css.push(`text-decoration:${lines}`);
  if (hex(look.color)) css.push(`color:${look.color}`);
  if (hex(look.fill)) css.push(`background:${look.fill}`);
  if (['left', 'center', 'right'].includes(look.align)) css.push(`text-align:${look.align}`);
  for (const [side, line] of Object.entries(look.border || {})) {
    if (['top', 'right', 'bottom', 'left'].includes(side) && BORDER.test(line)) css.push(`border-${side}:${line}`);
  }
  return css.join(';');
}

// A cell's value as the preview shows it: the text Excel shows (like $1,200.00) when the file says, else the value.
// One formatter for every cell: making one per call is slow on a 500-row sheet.
const NUMBERS = new Intl.NumberFormat('en', { maximumFractionDigits: 6 });
const plainText = (value) => (typeof value === 'number' ? NUMBERS.format(value) : String(value ?? ''));
const shownText = (value, text) => (typeof text === 'string' ? text : plainText(value));

// One cell. `spot` is its address (B3) when it can take a comment; `mark` is the pin of a comment waiting on it.
// `look` is how the file formats it: the text Excel shows and any formula behind a worked-out value; `css` is its
// style, worked out once.
// `picked` is its place in a block being commented on: 'held' for a block with a comment waiting, or the edges
// ('sel t l') of the block being picked. `was` is what it showed before the agent's last rework, when that changed;
// `found` says it holds a value of the row this source was opened from. `shown` is the text it shows, worked out once.
function cell(value, { spot = '', changed = false, mark = '', look = {}, css = '', picked = '', was = null, found = false, ring = false, shown = shownText(value, look.text) } = {}) {
  const plain = typeof look.text === 'string' ? plainText(value) : shown;
  const cls = [typeof value === 'number' ? 'num' : '', changed || was ? 'changed' : '', found ? 'found' : '', ring ? 'ring' : '', picked].filter(Boolean).join(' ');
  // A value the preview worked out from a formula shows that formula on hover.
  const tip = typeof look.formula === 'string' ? look.formula : typeof value === 'string' || shown !== plain ? shown : '';
  const title = tip ? ` title="${esc(tip)}"` : '';
  // A colour the number format picks, like [Red] for negatives, wins over the style's text colour.
  const style = [css, hex(look.color) ? `color:${look.color}` : ''].filter(Boolean).join(';');
  const before = was && (was.value ?? was.text ?? '') !== '' ? `<s class="was">${esc(shownText(was.value, was.text))}</s>` : '';
  return `<td${cls ? ` class="${cls}"` : ''}${style ? ` style="${style}"` : ''}${spot ? ` data-cell="${spot}"` : ''}${title}>${mark}${before}${marked(shown)}</td>`;
}

function sheetHtml(view) {
  const { data, path } = view;
  const picks = data.row_sources && Object.keys(data.row_sources).length > 0;
  const latest = !view.version; // an older version takes no comments and shows none: they are on the latest
  const spots = canComment() && latest;
  const changed = latest ? changedSpots(path) : new Set();
  const waiting = latest ? pinsOn(path, { sheet: data.sheet }) : [];
  const pins = {}; // a block's pin sits on its first cell, beside any other pin there
  // Sent comments keep their pin, in the colour of the code's verdict, while open and with Changes off.
  if (latest && !changesOn(view)) {
    for (const t of sentPinsOn(path, { sheet: data.sheet })) {
      const first = String(t.root.c.cell || '').split(':')[0];
      if (first) pins[first] = (pins[first] || '') + sentPin(t);
    }
  }
  for (const c of waiting) pins[c.cell.split(':')[0]] = (pins[c.cell.split(':')[0]] || '') + pin(c);
  // The spot opened from a comment or a link, or the change in focus, is ringed.
  const ringed = new Set();
  const ring = changesOn(view) ? null : view.ring; // with Changes on, the change in focus is the one ringed
  if (ring?.cell && (ring.sheet || null) === (data.sheet || null)) for (const [r, col] of blockCells(ring.cell)) ringed.add(`${columnName(col)}${r}`);
  const focus = changesOn(view) ? changeInFocus(view) : null;
  if (focus) ringed.add(focus);
  const held = new Set(waiting.filter((c) => c.cell.includes(':')).flatMap((c) => blockCells(c.cell).map(([r, col]) => `${r},${col}`)));
  const draft = state.commentDraft;
  // The block of the comment being written. A block being dragged out is drawn by the drag itself (app.js).
  const live = draft?.cell && draft.path === path && (draft.sheet || null) === (data.sheet || null) ? draft.cell : '';
  const block = live ? blockCells(live) : [];
  const [top, left] = block[0] || [];
  const [bottom, right] = block[block.length - 1] || [];
  const picked = (row, col) => {
    if (live && row >= top && row <= bottom && col >= left && col <= right) {
      return ['sel', row === top && 't', row === bottom && 'b', col === left && 'l', col === right && 'r'].filter(Boolean).join(' ');
    }
    return held.has(`${row},${col}`) ? 'held' : '';
  };
  const css = (data.styles || []).map(lookStyle);
  const diff = changesOn(view)?.cells || null; // what each changed cell showed before, with Changes on
  const one = (r, i, c) => {
    const at = `${data.columns[c]}${i + 1}`;
    const look = data.formats?.[at] || {};
    const shown = shownText(r[c], look.text);
    return cell(r[c], {
      shown,
      spot: spots ? at : '',
      changed: changed.has(`${data.sheet || ''}!${at}`),
      mark: pins[at] || '',
      look,
      css: css[look.style] || '',
      picked: picked(i + 1, c + 1),
      was: diff?.[at] || null,
      found: !!view.found && isFound(view, shown),
      ring: ringed.has(at),
    });
  };
  // With "Changed rows only", the first row stays as the heading.
  const only = changedOnly(view);
  const kept = (i) => !only || i === 0 || data.columns.some((c) => diff[`${c}${i + 1}`]);
  const hidden = data.sheets.find((s) => s.name === data.sheet)?.hidden;
  const meta = [hidden ? 'Hidden sheet' : '', data.more ? 'Showing the first 500 rows' : '']
    .filter(Boolean)
    .map((t) => `<span>${t}</span>`)
    .join('');
  const grid = data.rows.length
    ? `<div class="sheet"><table class="grid ${picks ? 'pick' : ''} ${commenting() ? 'spots' : ''}"><thead><tr><th></th>${data.columns.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>
        ${data.rows.map((r, i) => (kept(i) ? `<tr ${picks ? `data-row="${i + 1}" tabindex="0"` : ''} class="${picks && state.row === i + 1 ? 'sel' : ''}"><td>${i + 1}</td>${data.columns.map((_, c) => one(r, i, c)).join('')}</tr>` : '')).join('')}
      </tbody></table></div>`
    : '<p class="empty">This sheet is empty.</p>';
  return `${meta ? `<div class="meta">${meta}</div>` : ''}${grid}<div class="proof list" id="proof">${picks ? proofHtml() : ''}</div>`;
}

// The uploaded files behind the selected row of an output sheet: which of its values came from which file, and
// where in it. A source none of the values was found in is still listed, so it can be opened.
export function proofHtml() {
  if (!state.row) return '';
  const data = state.preview?.data;
  const cited = data?.row_sources?.[state.row] || [];
  const head = `<h2>Row ${state.row} came from</h2>`;
  if (!cited.length) return `${head}<div class="empty">No source file is recorded for this row.</div>`;
  const stored = [...new Set(cited.map(storedPath).filter(Boolean))];
  const { groups, left } = rowProof(data, state.row, stored);
  // A source opens where the value was found (its sheet and cell, line or page), with the row's values outlined.
  const file = (path, spot = null) => {
    const at = spot ? ` data-spot="${esc(JSON.stringify(spot))}"` : '';
    return `<button class="file" data-open="${esc(path)}"${at} title="${esc(path)}">${esc(baseName(path))}</button>`;
  };
  const line = (label, value, from) => `<tr><td>${esc(label)}</td><td class="num">${esc(value)}</td><td>${from}</td></tr>`;
  const rows = groups.map((g) =>
    line(g.label, g.shown, g.from ? `${file(g.from.path, g.from.spot)} <span class="where">${esc(g.from.where)}</span>` : `<span class="where">${esc(g.status)}</span>`),
  );
  // Sources no value was found in (a picture, a scan), and cited files that are not among the uploads.
  for (const path of left) rows.push(line('Other source', '', `${file(path)} ${placeHtml(path)}`));
  for (const p of cited.filter((p) => !storedPath(p))) rows.push(line('Other source', '', `<span>${esc(p)}</span>`));
  return `${head}<table class="came-from">${rows.join('')}</table>`;
}

function bodyHtml(view) {
  if (view.error) return `<p class="empty problem">${esc(view.error)}</p>`;
  const data = view.data;
  if (!data) return '<p class="empty">Loading</p>';
  if (data.kind === 'sheet') return sheetHtml(view);
  if (data.kind === 'text') {
    // Each line can take a comment, so each is its own piece.
    const latest = !view.version;
    const diff = changesOn(view);
    const pins = {};
    if (latest && !diff) for (const t of sentPinsOn(view.path)) if (t.root.c.line) pins[t.root.c.line] = (pins[t.root.c.line] || '') + sentPin(t);
    if (latest) for (const c of pinsOn(view.path)) pins[c.line] = (pins[c.line] || '') + pin(c);
    const changed = latest ? changedSpots(view.path) : new Set();
    const ringLine = diff ? changeInFocus(view) : view.ring?.line ? String(view.ring.line) : null;
    const only = changedOnly(view);
    // Lines the agent took out show, struck through, where they were.
    const removed = {};
    for (const r of diff?.removed || []) (removed[r.after] ||= []).push(`<span class="ln gone"><s>${esc(r.text)}</s></span>`);
    let gap = false; // with "Changed lines only", a run of hidden lines shows as one gap
    // A Markdown file is shown formatted, line by line, so each line can still take a comment (markdown.js).
    const md = /\.(md|markdown)$/i.test(view.path);
    const all = data.text.split('\n');
    const roles = md ? lineRoles(all) : [];
    const plain = plainFor(state.find?.query);
    const lines = (removed[0] || []).concat(all.flatMap((line, i) => {
      const n = String(i + 1);
      const after = removed[i + 1] || [];
      if (only && !(n in diff.lines)) {
        const first = !gap;
        gap = !after.length;
        return [...(first ? ['<span class="gap">⋯</span>'] : []), ...after];
      }
      gap = false;
      const look = md ? lineLook(roles[i]) : { cls: '', attrs: '', before: '' };
      const cls = ['ln', look.cls, changed.has(`line ${n}`) || (diff && n in diff.lines) ? 'changed' : '', view.found && isFound(view, line) ? 'found' : '', n === ringLine ? 'ring' : ''].filter(Boolean).join(' ');
      const was = diff?.lines[n] ? ` title="Was: ${esc(diff.lines[n])}"` : '';
      const shown = md ? inline(marked(roles[i].text), plain || roles[i].role === 'code' || roles[i].role === 'table') : marked(line);
      return [`<span class="${cls}"${look.attrs}${was} ${canComment() && latest ? `data-line="${n}"` : ''}>${pins[n] || ''}${look.before}${shown}</span>`, ...after];
    }));
    const more = data.more ? '<p class="meta">The rest of the file is not shown. Download it to read everything.</p>' : '';
    if (md) return `<div class="doc md ${commenting() ? 'spots' : ''}">${lines.join('')}</div>${more}`;
    return `<pre class="doc ${commenting() ? 'spots' : ''}">${lines.join('\n')}</pre>${more}`;
  }
  if (data.kind === 'pages' || data.kind === 'image') {
    // Comments on a page or picture are pinned where the person clicked, as a share of its width and height.
    const where = data.kind === 'pages' ? { page: view.page } : {};
    // A drawn box shows its outline, with its pin on the top-left corner.
    const area = (a, cls) => `<span class="area ${cls}" style="left:${a.x * 100}%;top:${a.y * 100}%;width:${a.w * 100}%;height:${a.h * 100}%"></span>`;
    const at = (c) => `left:${c.x * 100}%;top:${c.y * 100}%`;
    const sent = sentPinsOn(view.path, where)
      .filter((t) => t.root.c.x != null)
      .map((t) => sentPin(t, at(t.root.c)))
      .join('');
    const ring = view.ring && view.ring.x != null && (view.ring.page || null) === (where.page || null) && isBox(view.ring) ? area(view.ring, 'ring') : '';
    const pins = sent + ring + pinsOn(view.path, where)
      .map((c) => (isBox(c) ? area(c, 'held') : '') + pin(c).replace('class="pin"', `class="pin" style="${at(c)}"`))
      .join('');
    const draft = state.commentDraft;
    const drawn = draft?.path === view.path && (draft.page || null) === (where.page || null) && isBox(draft) ? draft : null;
    const picture = `<div class="stage ${commenting() ? 'spots' : ''}" ${canComment() ? 'data-stage' : ''}><img src="${view.image}" alt="${esc(data.name)}${data.kind === 'pages' ? `, page ${view.page}` : ''}" draggable="false">${data.kind === 'pages' ? pageMarksHtml(view) : ''}${pins}${drawn ? area(drawn, 'live') : ''}</div>`;
    const shot = `<div class="shot">${view.image ? picture : '<span class="empty">Loading</span>'}</div>`;
    const pager = data.kind === 'pages' && data.pages > 1
      ? `<div class="pager"><button class="icon-btn" data-page="${view.page - 1}" aria-label="Previous page" ${view.page <= 1 ? 'disabled' : ''}>${icon('back')}</button>
          Page ${view.page} of ${data.pages}<button class="icon-btn" data-page="${view.page + 1}" aria-label="Next page" ${view.page >= data.pages ? 'disabled' : ''}>${icon('chev')}</button></div>`
      : '';
    return shot + pager;
  }
  return `<p class="empty">${data.error ? 'This file could not be opened for preview.' : 'There is no preview for this kind of file.'} Download it to look inside.</p>`;
}

// Whether the file on show has places a comment can go: a sheet with rows, text, or a page or picture once loaded.
function hasSpots(view) {
  const data = view.data;
  if (!data || view.error) return false;
  if (data.kind === 'sheet') return data.rows.length > 0;
  return data.kind === 'text' || (['pages', 'image'].includes(data.kind) && !!view.image);
}

// The sheets of a workbook, as tabs that scroll sideways when there are many.
function tabsHtml(view) {
  const data = view.data;
  if (data?.kind !== 'sheet' || data.sheets.length < 2) return '';
  return `<div class="tabs" role="group" aria-label="Sheets">${data.sheets.map((s) => `<button class="${s.name === data.sheet ? 'on' : ''}" data-sheet="${esc(s.name)}" aria-pressed="${s.name === data.sheet}">${esc(s.name)}</button>`).join('')}</div>`;
}

function viewerHtml(view) {
  const entry = state.map?.[view.path];
  const artifact = (state.job.artifacts || []).find((a) => a.path === view.path);
  // What the agent made of this file: a pill beside the sheet tabs, and for a source the reason in a line below.
  let status = '';
  let reason = '';
  if (entry) {
    const [tone, label] = DECISIONS[entry.decision] || ['warn', entry.decision || 'Undecided'];
    status = pill(tone, label);
    reason = entry.reason ? `<p class="reason">${esc(entry.reason)}</p>` : '';
  } else if (artifact?.status) status = artifact.status === 'validated' ? pill('ok', 'Checked') : pill('warn', 'Draft');
  // The body is drawn first: the find bar counts the matches it marked.
  startMarks();
  const body = bodyHtml(view);
  const full = state.full;
  const back = view.back ? (view.back.row ? `Back to row ${view.back.row}` : `Back to ${baseName(view.back.path)}`) : 'Back to files';
  // At full screen the list beside the file takes the place of Back, except Back to the row a source was opened from.
  const backBtn = !full || view.back ? `<button class="icon-btn" data-action="back-file" aria-label="${esc(back)}" title="${esc(back)}">${icon('back')}</button>` : '';
  const railBtn = full
    ? `<button class="icon-btn" data-action="toggle-rail" aria-pressed="${state.rail}" aria-label="${state.rail ? 'Hide' : 'Show'} the file list" title="${state.rail ? 'Hide' : 'Show'} the file list">${icon('side')}</button>`
    : '';
  const find = canFind(view) ? `<button class="icon-btn" data-find aria-label="Find in file" title="Find in file" aria-pressed="${!!state.find}">${icon('find')}</button>` : '';
  // An older version is for looking only: no comments, and a way to carry on from it or go back to the latest.
  const older = view.version
    ? `<div class="older"><span>An older version. Comments go on the latest.</span>
        <button class="btn quiet small" data-continue="${view.version}" ${attr('disabled', state.busy || isActive())}>Continue from here</button>
        <button class="btn quiet small" data-pick-version="latest">Back to latest</button></div>`
    : '';
  const loaded = view.data && !view.error;
  // Under the file's name: the sheet tabs, what the agent made of the file, and the Changes switch.
  const tabs = loaded ? tabsHtml(view) : '';
  const changes = loaded ? changesBarHtml(view) : '';
  const bar = tabs || status || changes ? `<div class="file-bar">${tabs}${status}${changes}</div>` : '';
  // The comments sent on this file, with their answers, on the latest and with Changes off.
  const sent = !view.version && !changesOn(view) && loaded ? sentHtml(view.path) : '';
  const close = full ? '' : `<button class="icon-btn" data-action="close-file" aria-label="Close" title="Close">${icon('x')}</button>`;
  const head = `
    <div class="files-head">${railBtn}${backBtn}
      <h2 title="${esc(view.path)}">${nameHtml(baseName(view.path))}</h2>
      ${versionMenuHtml(view.path, view.version)}
      ${canComment() && hasSpots(view) && !view.version ? commentHtml() : ''}${toolsHtml(view, find)}${fullHtml()}${close}</div>`;
  const content = `${findBarHtml(view)}<div class="files-body">${older}${bar}${reason}${body}${sent}</div>`;
  if (full && state.rail) {
    return `${head}<div class="split"><nav class="rail" aria-label="Files in this run">${groupsHtml(state.job, { rail: true })}</nav><div class="pane">${content}</div></div>${barHtml()}`;
  }
  return `${head}${content}${barHtml()}`;
}

// Zoom, find and download sit in the header while the panel is wide enough; in a narrow panel they move into a More
// menu (styles.css decides which, by the panel's width).
function toolsHtml(view, find) {
  const open = state.menu === 'more';
  const download = `<button class="icon-btn" data-download="${esc(view.path)}" ${view.version ? `data-file-version="${view.version}"` : ''} aria-label="Download" title="Download">${icon('down')}</button>`;
  return `<span class="menu-wrap more-wrap">
      <button type="button" class="icon-btn more" data-menu="more" aria-haspopup="true" aria-expanded="${open}" aria-label="More" title="More">${icon('more')}</button>
      <span class="tools ${open ? 'open' : ''}" role="group" aria-label="File tools">${zoomHtml()}${find}${download}</span></span>`;
}

// One button for comment mode, lit while it is on: what a click in the preview does. The C key flips it.
function commentHtml() {
  const on = state.previewMode === 'comment';
  return `<button class="icon-btn comment-btn" data-mode="${on ? 'view' : 'comment'}" aria-pressed="${on}" aria-label="Comment on this file" title="${on ? 'Stop commenting (C)' : 'Comment: click a cell, line or spot (C)'}">${icon('comment')}</button>`;
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
  state.open = state.preview = state.row = state.places = null;
  state.full = false;
}

// Loads in steps and calls `draw` after each, so the panel opens at once and fills in.
// `back` remembers the output and row a source was opened from, so Back returns there; `found` is what to outline
// in that source (see review.js), and `page` the page of a PDF to open at.
// `version` opens an output as that kept version holds it (null for the latest); `ring` is a spot to ring and bring
// into view: a cell, a line, or a box on a page.
export async function openFile(path, draw, { sheet = null, row = null, back = null, found = null, page = 1, version = null, ring = null } = {}) {
  dropImage();
  const view = { path, data: null, page: 1, image: null, error: '', back, found, hits: null, changes: null, version, ring: ring && { ...ring, jump: true } };
  state.menu = null;
  if (state.open !== path) resetZoom();
  state.open = path;
  state.preview = view;
  state.showFiles = true;
  state.row = row;
  draw();
  const current = () => state.preview === view;
  try {
    view.data = await api(fileUrl('preview', path, (sheet ? `&sheet=${encodeURIComponent(sheet)}` : '') + versionOf(view)));
    if (!current()) return;
    draw();
    loadChanges(view, draw);
    if (view.data.kind === 'image') view.image = await blobUrl(fileUrl('file', path, versionOf(view)));
    else if (view.data.kind === 'pages') {
      // A row's place in this source may have come in while the file loaded.
      view.page = Math.min(Math.max(1, view.found?.page || page), view.data.pages);
      view.image = await blobUrl(fileUrl('page', path, `&page=${view.page}${versionOf(view)}`));
      if (state.find) searchPdf(view, turnPage, draw);
    } else return;
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

const versionOf = (view) => (view.version ? `&version=${view.version}` : '');

// Open a file at a spot: a sent comment's, or a cell or file the agent named. The spot is ringed and scrolled into
// view; a file already on show (the latest, at that sheet and page) is not loaded again.
export function openSpot(spot, draw, back = null) {
  state.changes.on = false; // the plain file, with its pins, as the spot was commented on
  state.menu = null;
  const view = state.preview;
  const same = view?.path === spot.path && !view.version && view.data && !view.error;
  if (same && (!spot.sheet || spot.sheet === view.data.sheet) && (!spot.page || spot.page === view.page)) {
    view.ring = { ...spot, jump: true };
    return draw();
  }
  const from = view && view.path !== spot.path ? { path: view.path, sheet: view.data?.sheet } : back;
  return openFile(spot.path, draw, { sheet: spot.sheet || null, page: spot.page || 1, ring: spot, back: from });
}

export async function turnPage(page, draw) {
  const view = state.preview;
  if (!view?.data || page < 1 || page > view.data.pages) return;
  try {
    const image = await blobUrl(fileUrl('page', view.path, `&page=${page}${versionOf(view)}`));
    if (state.preview !== view) return URL.revokeObjectURL(image);
    dropImage();
    Object.assign(view, { page, image });
  } catch (error) {
    view.error = error.message;
  }
  draw();
}
