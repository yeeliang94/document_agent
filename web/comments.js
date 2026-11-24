// Comments pinned to a run's files. The person clicks a spot in the viewer, types what should change, and sends
// the comments together as one round. Only where each comment points is sent: the server reads the file itself
// to tell the agent what is there, so nothing here has to be trusted.

import { $, api, attr, baseName, esc, icon, inGroup, jobUrl, playbooks, plural, state } from './core.js';

export const canComment = () => !!state.job && !state.job.legacy && inGroup('accepts_comments');
// Clicks in the preview make comments only in Comment mode; in View mode they check things, like a row's sources.
export const commenting = () => canComment() && state.previewMode === 'comment';
export const MAX_BLOCK = 1000; // cells one comment can cover; the server holds to the same limit
export const pending = () => state.comments[state.job?.id] || [];

// --- Where a comment points -------------------------------------------------

const sameSpot = (a, b) =>
  a.path === b.path && (a.sheet || '') === (b.sheet || '') && a.cell === b.cell && a.line === b.line && a.page === b.page &&
  a.x === b.x && a.y === b.y && (a.w || 0) === (b.w || 0) && (a.h || 0) === (b.h || 0);

// A drawn box on a page or picture, as against a point.
export const isBox = (spot) => !!(spot.w || spot.h);

// Every cell of a block such as C3:E5, as [row, column] counted from 1.
export function blockCells(cell) {
  const at = (ref) => {
    const [, letters, row] = /^([A-Z]+)(\d+)$/.exec(ref);
    return [Number(row), [...letters].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0)];
  };
  const [first, last = first] = cell.split(':');
  const [r1, c1] = at(first);
  const [r2, c2] = at(last);
  const cells = [];
  for (let r = Math.min(r1, r2); r <= Math.max(r1, r2); r++) for (let c = Math.min(c1, c2); c <= Math.max(c1, c2); c++) cells.push([r, c]);
  return cells;
}

function label(spot) {
  if (spot.cell) {
    const cells = spot.cell.includes(':') ? `cells ${spot.cell}` : `cell ${spot.cell}`;
    return spot.sheet ? `${spot.sheet}, ${cells}` : cells[0].toUpperCase() + cells.slice(1);
  }
  if (spot.line) return `Line ${spot.line}`;
  const place = spot.page ? `Page ${spot.page}` : 'Picture';
  return isBox(spot) ? `${place}, selected area` : place;
}

// The number each unsent comment will carry, for the pins: its place in the list.
const numbered = () => pending().map((c, i) => ({ ...c, n: i + 1 }));
export const pinsOn = (path, where = {}) =>
  numbered().filter((c) => c.path === path && Object.entries(where).every(([k, v]) => (c[k] ?? null) === (v ?? null)));
export const pin = (c) => `<button class="pin" data-pin="${c.id}" aria-label="Comment ${c.n}: ${esc(c.text)}">${c.n}</button>`;

// The spots of one file that a finished round reports as changed, to show in green: "sheet!cell" for a cell,
// "line N" for a line. The server gives a verdict per comment; nothing is read out of its wording.
export function changedSpots(path) {
  const changed = new Set();
  for (const round of state.rounds) {
    const verdicts = round.report?.spots || [];
    for (const c of round.comments) {
      if (c.path !== path || !verdicts.some((v) => v.round === round.number && v.n === c.n && v.changed)) continue;
      if (c.cell) for (const [r, col] of blockCells(c.cell)) changed.add(`${c.sheet || ''}!${columnName(col)}${r}`);
      else if (c.line) changed.add(`line ${c.line}`);
    }
  }
  return changed;
}

export function columnName(n) {
  let name = '';
  for (; n > 0; n = Math.floor((n - 1) / 26)) name = String.fromCharCode(65 + ((n - 1) % 26)) + name;
  return name;
}

// --- Writing one ------------------------------------------------------------

// Open the comment box at a spot: a new comment, or the one already pinned there.
export function writeAt(spot, x, y) {
  const held = pending().find((c) => sameSpot(c, spot));
  state.commentDraft = { ...(held || { ...spot, text: '', remember: false }), at: [x, y] };
}

export function editPin(id, x, y) {
  const held = pending().find((c) => String(c.id) === String(id));
  if (held) state.commentDraft = { ...held, at: [x, y] };
}

export function popHtml() {
  const d = state.commentDraft;
  if (!d || !state.job) return '';
  // Beside the click, kept inside the window; a narrow screen gets it along the bottom.
  const narrow = window.innerWidth < 560;
  const left = Math.max(8, Math.min(d.at[0] + 12, window.innerWidth - 328));
  const top = Math.max(8, Math.min(d.at[1] + 12, window.innerHeight - (isBox(d) ? 360 : 260))); // room for the cut-out
  const place = narrow ? 'left:8px;right:8px;bottom:8px' : `left:${left}px;top:${top}px`;
  return `<div class="catch" data-comment="away"></div>
    <form class="pop" id="comment-form" style="${place}" role="dialog" aria-label="Comment">
      <h2>${esc(label(d))}</h2>${isBox(d) ? snipHtml(d) : ''}
      <textarea id="note" aria-label="Comment" placeholder="What should change?" maxlength="2000"></textarea>
      <div class="end">${d.id ? '<button type="button" class="btn text" data-comment="delete">Delete</button>' : ''}
        <button type="button" class="btn text" data-comment="cancel">Cancel</button><button class="btn quiet">${d.id ? 'Save' : 'Add comment'}</button></div></form>`;
}

// The drawn area, cut out of the page on show, so the person sees what the agent will be pointed at.
function snipHtml(d) {
  const img = document.querySelector('[data-stage] img');
  if (!img?.naturalWidth || state.preview?.path !== d.path) return '';
  const wide = Math.max(d.w, 0.001) * img.naturalWidth;
  const tall = Math.max(d.h, 0.001) * img.naturalHeight;
  const fit = Math.min(284 / wide, 96 / tall); // inside the comment box, at most 96px tall as in the prototype
  const [width, height] = [Math.round(wide * fit), Math.round(tall * fit)];
  const share = (at, size) => (size < 1 ? (at / (1 - size)) * 100 : 0);
  return `<div class="snip" role="img" aria-label="The selected area" style="width:${width}px;height:${height}px;background-image:url(${img.src});background-size:${100 / Math.max(d.w, 0.001)}% ${100 / Math.max(d.h, 0.001)}%;background-position:${share(d.x, d.w)}% ${share(d.y, d.h)}%"></div>`;
}

// Put the typed comment back after a redraw, so typing never redraws the box.
export function syncPop() {
  const box = $('#note');
  if (!box) return;
  if (box.value !== state.commentDraft.text) box.value = state.commentDraft.text;
  if (document.activeElement !== box && !$('#comment-form').contains(document.activeElement)) box.focus();
}

export function commentAction(name) {
  const d = state.commentDraft;
  if (!d) return;
  // A click elsewhere keeps what was typed as a comment; only Cancel throws it away.
  else if (name === 'away' && d.text.trim()) keepComment();
  else if (name === 'cancel' || name === 'away') state.commentDraft = null;
  else if (name === 'delete') {
    state.comments[state.job.id] = pending().filter((c) => c.id !== d.id);
    state.commentDraft = null;
  }
}

export function keepComment() {
  const { at, ...comment } = state.commentDraft;
  comment.text = comment.text.trim();
  if (!comment.text) return $('#note').focus();
  const list = pending();
  if (comment.id) state.comments[state.job.id] = list.map((c) => (c.id === comment.id ? comment : c));
  else state.comments[state.job.id] = [...list, { ...comment, id: Date.now() + Math.random() }];
  state.commentDraft = null;
}

// The comments waiting to be sent, in the Files list: where each points, and a way to take one out without
// opening its file (which may no longer be there).
export function pendingHtml() {
  const rows = numbered()
    .map(
      (c) => `<div class="frow"><span class="pin still">${c.n}</span>
        <button class="name" data-open="${esc(c.path)}" title="${esc(c.text)}">${esc(baseName(c.path))} · ${esc(label(c))}</button>
        <button class="icon-btn" aria-label="Remove comment ${c.n}" data-drop-comment="${c.id}">${icon('x')}</button></div>`,
    )
    .join('');
  return rows ? `<div class="list"><h3>Comments to send</h3>${rows}</div>` : '';
}

export function dropComment(id) {
  state.comments[state.job.id] = pending().filter((c) => String(c.id) !== String(id));
}

// --- Sending ----------------------------------------------------------------

// The strip under the Files panel: how many comments wait, and the button that sends them. `spots` says the
// file on show has places to click; without any, the strip appears only when comments are waiting.
export function barHtml(spots) {
  const n = pending().length;
  if (!canComment() || (!n && !spots)) return '';
  const files = new Set(pending().map((c) => c.path)).size;
  const said = n ? `${plural(n, 'comment')}${files > 1 ? ` on ${files} files` : ''}` : '';
  return `<div class="files-foot"><span>${said}</span>
    <button class="btn primary" data-action="send-comments" ${attr('disabled', !n || state.busy)}>Send to agent</button></div>`;
}

export async function sendComments() {
  const job = state.job;
  // One round is one fixed set of comments under one id, so a retry after a lost reply resends exactly that set
  // and the server takes it as the same round. Anything written or changed meanwhile waits for the next round.
  if (state.sending?.job !== job.id) {
    state.sending = { job: job.id, round_id: crypto.randomUUID(), comments: pending().map(({ at, ...c }) => ({ ...c })) };
  }
  const round = state.sending;
  try {
    await api(jobUrl('/comments'), {
      method: 'POST',
      body: JSON.stringify({ round_id: round.round_id, comments: round.comments.map(({ id, ...c }) => c) }),
    });
  } catch (error) {
    // A refusal means the round was not taken, so the next send is a new round with the comments as they are
    // then. A lost reply or a server fault leaves it unknown, and the same round is sent again.
    if (error.status >= 400 && error.status < 500) state.sending = null;
    throw error;
  }
  state.sending = null;
  const sent = new Map(round.comments.map((c) => [c.id, c]));
  const same = (c) => sent.has(c.id) && sent.get(c.id).text === c.text && sent.get(c.id).remember === c.remember;
  state.comments[job.id] = (state.comments[job.id] || []).filter((c) => !same(c));
  state.commentDraft = null;
}

// --- What was sent ----------------------------------------------------------

// The rounds already sent on this run, reloaded when one is added or changes state.
export async function loadRounds() {
  const job = state.job;
  const key = job ? job.id + JSON.stringify((job.feedback || []).map((r) => [r.number, r.status])) : '';
  if (key === state.roundsKey) return;
  state.roundsKey = key;
  if (!job?.feedback?.length) return void (state.rounds = []);
  try {
    const rounds = await api(jobUrl('/comments'));
    if (state.job?.id === job.id) state.rounds = rounds;
  } catch {
    state.roundsKey = ''; // try again on the next refresh
  }
}

// One round in the conversation: the comments as the person wrote them, and where each one pointed.
export function roundHtml(event, cls = '') {
  const lines = (event.comments || [])
    .map(
      (c) => `<li><span class="pin still">${c.n}</span><span><button class="file" data-open="${esc(c.path)}">${esc(baseName(c.path))}</button>
        <span class="where">${esc(c.where)}</span>${c.remember ? '<span class="pill">Remember</span>' : ''}<br>${esc(c.text)}</span></li>`,
    )
    .join('');
  const book = playbooks().find((p) => p.id === (state.job.playbook?.id || state.job.module));
  const version = event.remembered_version;
  // Undo is offered while that version is still the latest: after that, undoing it would undo later work too.
  const name = esc(book?.name || 'the playbook');
  const undone = state.forgotten.has(`${state.job.id}:${version}`);
  const kept = version
    ? `<div class="saved ${cls}"><span>${undone ? `Removed from ${name}` : `Added to ${name} · version ${version}`}</span>
        ${!undone && book?.version === version ? `<button class="btn text small" data-forget="${version}">Undo</button>` : ''}</div>`
    : '';
  return `<div class="me ${cls}"><ol class="notes">${lines}</ol></div>${kept}`;
}

// A lesson the agent took from a comment and added to the playbook by itself, with the way to take it back.
export function learnedHtml(item, last, cls = '') {
  const book = playbooks().find((p) => p.id === (state.job.playbook?.id || state.job.module));
  const name = esc(book?.name || 'the playbook');
  const undone = state.forgotten.has(`${state.job.id}:${item.version}`);
  const undo = last && !undone && book?.version === item.version ? `<button class="btn text small" data-forget="${item.version}">Undo</button>` : '';
  return `<div class="saved learned ${cls}"><span>${undone ? `Removed from ${name}` : `Learned for ${name}`}: ${esc(item.lesson)}</span>${undo}</div>`;
}

// Take a remembered comment back out of the playbook. Based on the version that added it: if the playbook has
// moved on since, the server refuses, because going back now would undo that later work too.
export async function forget(version) {
  const id = state.job.playbook?.id || state.job.module;
  const body = JSON.stringify({ version: version - 1, based_on: version });
  await api(`/api/playbooks/${encodeURIComponent(id)}/restore`, { method: 'POST', body });
  state.forgotten.add(`${state.job.id}:${version}`);
}
