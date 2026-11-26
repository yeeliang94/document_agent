// Comments pinned to a run's files. The person clicks a spot in the viewer, types what should change, and sends
// the comments together as one round. Only where each comment points is sent: the server reads the file itself
// to tell the agent what is there, so nothing here has to be trusted.

import { runClient } from './clients.js';
import { $, api, attr, baseName, esc, icon, inGroup, jobUrl, playbooks, plural, state } from './core.js';
import { linked } from './links.js';

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
  state.commentDraft = { ...(held || { ...spot, text: '', remember: false, remember_to: null }), at: [x, y] };
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
      ${rememberHtml(d)}
      <div class="end">${d.id ? '<button type="button" class="btn text" data-comment="delete">Delete</button>' : ''}
        <button type="button" class="btn text" data-comment="cancel">Cancel</button><button class="btn quiet">${d.id ? 'Save' : 'Add comment'}</button></div></form>`;
}

// Whether the agent keeps this comment for later runs, and where. A run with a client chooses: this run only, the
// client's notes, or the playbook for everyone; without a client, it is the playbook or nothing.
export const rememberedTo = (c) => (c.remember ? c.remember_to || 'playbook' : '');

// A name short enough for its button: "Acme" for Acme Manufacturing.
function shortName(name) {
  const first = name.split(/\s+/)[0];
  return name.length <= 12 || first.length < 3 ? name : first;
}

function rememberHtml(d) {
  const client = state.job.client?.name;
  const scope = rememberedTo(d);
  if (!client) {
    const on = scope === 'playbook';
    return `<div class="line"><span>Remember for next time</span><button type="button" class="switch ${on ? 'on' : ''}" role="switch" aria-checked="${on}" aria-label="Remember for next time" data-remember="${on ? '' : 'playbook'}"></button></div>`;
  }
  const book = state.job.playbook?.name || playbooks().find((p) => p.id === state.job.module)?.name || 'the playbook';
  const all = /^[A-Z][a-z]/.test(book) ? book[0].toLowerCase() + book.slice(1) : book;
  const choice = (value, label, title) =>
    `<button type="button" class="${scope === value ? 'on' : ''}" data-remember="${value}" aria-pressed="${scope === value}" title="${esc(title)}">${esc(label)}</button>`;
  return `<div class="line"><span>Remember</span><span class="seg" role="group" aria-label="Remember">${choice('', 'This run', 'Only this run')}${choice('client', shortName(client), `For ${client}`)}${choice('playbook', `All ${all}`, `For every ${book} run`)}</span></div>`;
}

export function chooseRemember(value) {
  const d = state.commentDraft;
  if (!d) return;
  d.remember = !!value;
  d.remember_to = value || null;
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

// The strip under the Files panel, while comments wait: how many, and the button that sends them.
export function barHtml() {
  const n = pending().length;
  if (!canComment() || !n) return '';
  const files = new Set(pending().map((c) => c.path)).size;
  return `<div class="files-foot"><span>${plural(n, 'comment')}${files > 1 ? ` on ${files} files` : ''}</span>
    <button class="btn primary" data-action="send-comments" ${attr('disabled', state.busy)}>Send to agent</button></div>`;
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
  const same = (c) => sent.has(c.id) && sent.get(c.id).text === c.text && rememberedTo(sent.get(c.id)) === rememberedTo(c);
  state.comments[job.id] = (state.comments[job.id] || []).filter((c) => !same(c));
  state.commentDraft = null;
}

// --- What was sent ----------------------------------------------------------

// The rounds already sent on this run, reloaded when one is added, answered or changes state.
export async function loadRounds() {
  const job = state.job;
  // Answers come in while a round is open, and resolving changes a round too, so both are part of the key.
  const key = job ? job.id + JSON.stringify((job.feedback || []).map((r) => [r.number, r.status, r.answers || {}, r.resolved || []])) : '';
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

// One round in the conversation: the comments as the person wrote them, where each one pointed, and the colour of
// the code's verdict on its pin. A row opens the file at the comment's spot.
export function roundHtml(event, cls = '') {
  const round = state.rounds.find((r) => r.number === event.round);
  const lines = (event.comments || [])
    .map((c) => {
      const kept = round?.comments.find((x) => x.n === c.n) || c;
      // An event from before comments carried their spot says where in its own words.
      const where = hasSpot(kept) ? label(kept) : c.where;
      return `<li><button class="spot" data-goto="${event.round}.${c.n}"><span class="pin ${verdictOf(round, c.n) || 'still'}">${c.n}</span>
        <span><span class="where">${esc(baseName(c.path))} · ${esc(where)}</span>${c.remember ? '<span class="pill">Remember</span>' : ''}${esc(c.text)}</span></button></li>`;
    })
    .join('');
  // Remembered comments go to the playbook, the client's notes, or both, each as one version.
  const kept = (scope, version) => {
    if (!version) return '';
    const { name, latest } = keptIn(scope);
    // Undo is offered while that version is still the latest: after that, undoing it would undo later work too.
    const undone = state.forgotten.has(forgetKey(scope, version));
    return `<div class="saved ${cls}"><span>${undone ? `Removed from ${name}` : `Added to ${name} · version ${version}`}</span>
        ${!undone && latest === version ? undoButton(scope, version) : ''}</div>`;
  };
  return `<div class="me round ${cls}"><ol class="notes">${lines}</ol></div>${kept('playbook', event.remembered_version)}${kept('client', event.remembered_client_version)}`;
}

// Where a run keeps what it remembers: the name to show, already escaped, and the latest version there now.
function keptIn(scope) {
  if (scope === 'client') {
    const client = runClient();
    const who = esc(client?.name || state.job.client?.name || 'the client');
    return { name: `${who}’s notes`, who, latest: client?.version };
  }
  const book = playbooks().find((p) => p.id === (state.job.playbook?.id || state.job.module));
  const name = esc(book?.name || 'the playbook');
  return { name, who: name, latest: book?.version };
}
const forgetKey = (scope, version) => `${state.job.id}:${scope === 'client' ? 'client:' : ''}${version}`;
const undoButton = (scope, version) =>
  `<button class="btn text small" data-forget="${version}" ${scope === 'client' ? 'data-scope="client"' : ''}>Undo</button>`;

// --- Answers, replies and resolving -------------------------------------------
// Each sent comment is named by its round and number, "1.2". The agent answers it with a save; the code's verdict
// (did the spot change?) comes in the round's report when the run finishes, and wins over what the answer says.

const SPOT = ['path', 'sheet', 'cell', 'page', 'x', 'y', 'w', 'h', 'line'];
const hasSpot = (c) => !!(c.cell || c.line || c.page || c.x != null);
const OPEN_ROUND = ['saving', 'sent', 'delivered'];

// 'done' when the spot changed, 'left' when it did not, 'wait' while the agent works on it.
export function verdictOf(round, n) {
  if (!round) return '';
  if (round.report) return round.report.spots?.some((v) => v.round === round.number && v.n === n && v.changed) ? 'done' : 'left';
  return OPEN_ROUND.includes(round.status) ? 'wait' : '';
}

// Every sent comment, by its name.
function sentComments() {
  const all = new Map();
  for (const round of state.rounds) {
    for (const c of round.comments) {
      all.set(`${round.number}.${c.n}`, {
        key: `${round.number}.${c.n}`, round, c, answer: round.answers?.[String(c.n)] || '',
        verdict: verdictOf(round, c.n), resolved: (round.resolved || []).includes(c.n),
        open: OPEN_ROUND.includes(round.status),
      }); // prettier-ignore
    }
  }
  return all;
}

export const sentComment = (key) => sentComments().get(key) || null;

// The comments on one file as threads: a comment, then each reply to it in order. A thread shows the verdict of its
// last comment, and resolving it resolves that one; a reply opens its comment again on the server.
export function threadsOn(path) {
  const all = sentComments();
  const rootOf = (item) => {
    for (let seen = 0, at = item; seen < 50; seen++) {
      const up = at.c.reply_to && all.get(at.c.reply_to);
      if (!up) return at.key;
      at = up;
    }
    return item.key;
  };
  const threads = new Map();
  for (const item of all.values()) {
    const root = rootOf(item);
    if (!threads.has(root)) threads.set(root, []);
    threads.get(root).push(item);
  }
  return [...threads.values()]
    .filter((items) => items[0].c.path === path)
    .map((items) => ({ root: items[0], items, last: items[items.length - 1] }));
}

// A sent comment's number as its pin shows it: "2" while there is one round, "1.2" once there are more.
export const sentLabel = (item) => (state.rounds.length > 1 ? item.key : String(item.c.n));

// The open threads pinned on one place of a file, for the viewer: { sheet } for a sheet, { page } for a page.
export function sentPinsOn(path, where = {}) {
  return threadsOn(path).filter(
    (t) => !t.last.resolved && Object.entries(where).every(([k, v]) => (t.root.c[k] ?? null) === (v ?? null)),
  );
}

export const sentPin = (t, style = '') =>
  `<span class="pin still ${t.last.verdict}" ${style ? `style="${style}"` : ''} aria-label="Sent comment ${sentLabel(t.root)}">${sentLabel(t.root)}</span>`;

const VERDICT = { done: 'Changed', left: 'Not changed' };

function answerHtml(item) {
  if (item.answer) return `<div class="answer"><span class="who">Agent</span><p>${linked(item.answer)}</p></div>`;
  return item.open ? '<div class="answer pending"><p><span class="live-dot"></span>Working on it</p></div>' : '';
}

// The comments sent on the file on show, each with the agent's answer, Reply and Resolve; the resolved ones fold away.
export function sentHtml(path) {
  const threads = threadsOn(path);
  if (!threads.length) return '';
  const open = threads.filter((t) => !t.last.resolved);
  const done = threads.filter((t) => t.last.resolved);
  const canReply = canComment();
  const item = (t) => {
    const { root, last } = t;
    const replying = state.reply?.to === last.key;
    const waiting = last.open && !last.answer;
    const verdict = VERDICT[last.verdict] || '';
    const replies = t.items
      .slice(1)
      .map((r) => `<div class="answer you"><span class="who">You</span><p>${esc(r.c.text)}</p></div>${answerHtml(r)}`)
      .join('');
    const box = replying
      ? `<form class="reply" id="reply-form"><textarea id="reply-text" placeholder="Reply" aria-label="Reply to the agent" maxlength="2000"></textarea>
          <div class="row"><button type="button" class="btn text" data-reply-cancel>Cancel</button><button class="btn primary small" ${attr('disabled', state.busy)}>Send</button></div></form>`
      : waiting || !canReply ? '' : `<div class="c-act"><button class="btn text small" data-reply="${last.key}">Reply</button></div>`;
    return `<div class="c ${t.items.some((x) => x.key === state.activeComment) ? 'on' : ''}">
      <div class="c-top"><span class="pin still ${last.verdict}">${sentLabel(root)}</span>
        <button class="where" data-goto="${root.key}">${esc(label(root.c))}</button>
        <span class="verdict ${last.verdict === 'done' ? 'ok' : ''}">${verdict}</span>
        ${waiting || replying ? '' : `<button class="btn text small" data-resolve="${last.key}">Resolve</button>`}</div>
      <p>${esc(root.c.text)}</p>${answerHtml(root)}${replies}${box}</div>`;
  };
  const doneRow = (t) => `<div class="resolved"><span class="pin still ${t.last.verdict}">${sentLabel(t.root)}</span>
      <span>${esc(label(t.root.c))} · ${esc(t.root.c.text)}</span><button class="btn text small" data-reopen="${t.last.key}">Reopen</button></div>`;
  const toggle = done.length
    ? `<button type="button" class="switch-label" role="switch" aria-checked="${state.showResolved}" data-show-resolved><span class="switch ${state.showResolved ? 'on' : ''}"></span>Resolved (${done.length})</button>`
    : '';
  return `<section class="comments" aria-label="Comments"><div class="comments-head"><h2>Comments</h2>${toggle}</div>
    ${open.map(item).join('') || '<p class="empty">No open comments.</p>'}
    ${state.showResolved ? done.map(doneRow).join('') : ''}</section>`;
}

// Put the typed reply back after a redraw, so typing never redraws the box.
export function syncReply() {
  const box = $('#reply-text');
  if (!box || !state.reply) return;
  if (box.value !== state.reply.text) box.value = state.reply.text;
  const send = $('#reply-form .btn.primary');
  if (send) send.disabled = state.busy || !state.reply.text.trim();
}

// A reply is a comment of its own, sent as a new round on the same spot; the server takes the spot from the
// comment it replies to. Like a round, it keeps one id until the server has it, so a retry is the same reply.
export async function sendReply() {
  const reply = state.reply;
  const item = reply && sentComment(reply.to);
  const text = reply?.text.trim();
  if (!item || !text) return;
  reply.round_id ||= crypto.randomUUID();
  const spot = Object.fromEntries(SPOT.filter((k) => item.c[k] != null).map((k) => [k, item.c[k]]));
  try {
    await api(jobUrl('/comments'), {
      method: 'POST',
      body: JSON.stringify({ round_id: reply.round_id, comments: [{ ...spot, text, reply_to: reply.to }] }),
    });
  } catch (error) {
    if (error.status >= 400 && error.status < 500) reply.round_id = null;
    throw error;
  }
  if (state.reply === reply) state.reply = null;
}

// Resolve a comment or open it again. For the screen only: the agent is never told.
export async function resolveComment(key, resolved) {
  state.rounds = await api(jobUrl('/comments/resolve'), { method: 'POST', body: JSON.stringify({ comment: key, resolved }) });
  if (resolved && state.reply?.to === key) state.reply = null;
}

// A lesson the agent took from a comment and added to the playbook by itself, with the way to take it back.
export function learnedHtml(item, last, cls = '') {
  const scope = item.scope === 'client' ? 'client' : 'playbook';
  const { name, who, latest } = keptIn(scope);
  const undone = state.forgotten.has(forgetKey(scope, item.version));
  const undo = last && !undone && latest === item.version ? undoButton(scope, item.version) : '';
  return `<div class="saved learned ${cls}"><span>${undone ? `Removed from ${name}` : `Learned for ${who}`}: ${esc(item.lesson)}</span>${undo}</div>`;
}

// Take a remembered comment or a lesson back out of the playbook or the client's notes. Based on the version that
// added it: if that place has moved on since, the server refuses, because going back now would undo later work too.
export async function forget(version, scope = 'playbook') {
  const body = JSON.stringify({ version: version - 1, based_on: version });
  const where =
    scope === 'client'
      ? `/api/clients/${encodeURIComponent(state.job.client.id)}`
      : `/api/playbooks/${encodeURIComponent(state.job.playbook?.id || state.job.module)}`;
  await api(`${where}/restore`, { method: 'POST', body });
  state.forgotten.add(forgetKey(scope, version));
}
