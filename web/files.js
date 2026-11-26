// The Files panel: the run's outputs, checks and source files, and a viewer to look inside any of them.

import {
  api, attr, baseName, blobUrl, esc, extension, fileUrl, icon, inGroup, isActive, jobUrl, pill, plural, sources,
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
const HIDDEN_OUTPUTS = ['document_map.json', 'provenance.json'];

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
      fact('Totals add up', s.totals === undefined ? pill('', 'Not checked') : !s.totals && !s.totals_problems ? pill('', 'No totals') : ok(!s.totals_problems, s.totals_problems ? plural(s.totals_problems, 'problem') : 'Passed')),
      fact('Files decided', s.files === undefined ? pill('', 'Not checked') : ok(s.files_decided === s.files, `${s.files_decided} of ${s.files}`)),
    ];
    if (s.rows_written != null) rows.push(fact('Rows with a source', ok(s.rows_sourced === s.rows_written && s.rows_written > 0, `${s.rows_sourced} of ${s.rows_written}`)));
    rows.push(fact('Reviewer', check.reviews ? ok(true, check.doubts ? `${plural(check.doubts, 'doubt')} checked` : 'No doubts') : pill('', 'Not run')));
    // A finished run lists what is still to fix in its report, so the list is shown once, there.
    const issues = !check.passed && check.issues?.length && !inGroup('finished', job.status) ? `<ul class="fix">${check.issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>` : '';
    return `<div class="list"><h3>Checks</h3>${rows.join('')}${issues}</div>`;
  }
  return '';
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
  return `
    <div class="files-head"><h2>Files</h2><button class="icon-btn only-narrow" data-action="toggle-files" aria-label="Close">${icon('x')}</button></div>
    <div class="files-body">
      ${pendingHtml()}
      <div class="list"><h3>Output</h3>${outputRows}</div>
      ${checksHtml(job)}
      ${template ? `<div class="list"><h3>${/\.json$/i.test(template.path) ? 'Format' : 'Template'}</h3>${row(template.path, baseName(template.path))}</div>` : ''}
      <div class="list"><h3><span>Sources</span><span>${all.length}</span></h3>${filter}${sourceRows}</div>
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
  const tabs = data.sheets.length > 1
    ? `<div class="filter">${data.sheets.map((s) => `<button class="${s.name === data.sheet ? 'on' : ''}" data-sheet="${esc(s.name)}">${esc(s.name)}</button>`).join('')}</div>`
    : '';
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
  return `${tabs || meta ? `<div class="meta">${meta}${tabs}</div>` : ''}${grid}<div class="proof list" id="proof">${picks ? proofHtml() : ''}</div>`;
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

function viewerHtml(view) {
  const entry = state.map?.[view.path];
  const artifact = (state.job.artifacts || []).find((a) => a.path === view.path);
  let about = '';
  if (entry) {
    const [tone, label] = DECISIONS[entry.decision] || ['warn', entry.decision || 'Undecided'];
    about = `<div class="meta">${pill(tone, label)}<span>${esc(entry.reason)}</span></div>`;
  } else if (artifact?.status) about = `<div class="meta">${artifact.status === 'validated' ? pill('ok', 'Checked') : pill('warn', 'Draft')}</div>`;
  // The body is drawn first: the find bar counts the matches it marked.
  startMarks();
  const body = bodyHtml(view);
  const back = view.back ? (view.back.row ? `Back to row ${view.back.row}` : `Back to ${baseName(view.back.path)}`) : 'Back to files';
  const find = canFind(view) ? `<button class="icon-btn" data-find aria-label="Find in file" title="Find in file" aria-pressed="${!!state.find}">${icon('find')}</button>` : '';
  // An older version is for looking only: no Comment mode, and a way to carry on from it or go back to the latest.
  const older = view.version
    ? `<div class="older"><span>An older version. Comments go on the latest.</span>
        <button class="btn quiet small" data-continue="${view.version}" ${attr('disabled', state.busy || isActive())}>Continue from here</button>
        <button class="btn quiet small" data-pick-version="latest">Back to latest</button></div>`
    : '';
  // The comments sent on this file, with their answers, on the latest and with Changes off.
  const sent = !view.version && !changesOn(view) && view.data && !view.error ? sentHtml(view.path) : '';
  return `
    <div class="files-head"><button class="icon-btn" data-action="back-file" aria-label="${esc(back)}" title="${esc(back)}">${icon('back')}</button>
      <h2 title="${esc(view.path)}">${esc(baseName(view.path))}</h2>
      ${versionMenuHtml(view.path, view.version)}
      ${canComment() && hasSpots(view) && !view.version ? modesHtml() : ''}${toolsHtml(view, find)}
      <button class="icon-btn" data-action="close-file" aria-label="Close">${icon('x')}</button></div>
    ${findBarHtml(view)}
    <div class="files-body">${older}${about}${view.data && !view.error ? changesBarHtml(view) : ''}${body}${sent}</div>${barHtml()}`;
}

// Zoom, find, full screen and download sit in the header while the panel is wide enough; in a narrow panel they
// move into a More menu (styles.css decides which, by the panel's width).
function toolsHtml(view, find) {
  const open = state.menu === 'more';
  const download = `<button class="icon-btn" data-download="${esc(view.path)}" ${view.version ? `data-file-version="${view.version}"` : ''} aria-label="Download" title="Download">${icon('down')}</button>`;
  return `<span class="menu-wrap more-wrap">
      <button type="button" class="icon-btn more" data-menu="more" aria-haspopup="true" aria-expanded="${open}" aria-label="More" title="More">${icon('more')}</button>
      <span class="tools ${open ? 'open' : ''}" role="group" aria-label="File tools">${zoomHtml()}${find}${fullHtml()}${download}</span></span>`;
}

// View or Comment: what a click in the preview does. The C key flips it.
function modesHtml() {
  const mode = (name, label) => `<button data-mode="${name}" aria-pressed="${state.previewMode === name}">${label}</button>`;
  return `<div class="modes" role="group" aria-label="Clicks in the preview">${mode('view', 'View')}${mode('comment', 'Comment')}</div>`;
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
