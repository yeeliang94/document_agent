// Cells and files the agent names in what it writes (its messages, its report, its answers to comments), shown as
// links that open the spot. The server finds them and checks each against the run's real files (POST /links), so
// only a cell of a real sheet or a file of this run becomes a link; anything else stays plain text.

import { api, esc, jobUrl, state } from './core.js';

const MAX_TEXTS = 200; // texts the server takes in one call
const wanted = new Set(); // texts drawn as plain text so far, to ask the server about
let asking = false;

// Links depend on the run's files, so they are asked again when the outputs change.
function current() {
  const job = state.job;
  const key = job ? `${job.id}|${(job.artifacts || []).map((a) => `${a.path}:${a.size}`).join(',')}` : '';
  if (state.links.key !== key) {
    state.links = { key, found: {} };
    wanted.clear();
  }
  return state.links;
}

// One text as HTML, with each link found in it as a button. Until the server has answered, the text is plain.
export function linked(text) {
  const shown = String(text ?? '');
  const found = current().found[shown];
  if (found === undefined) {
    if (shown.trim() && state.job) wanted.add(shown);
    return esc(shown);
  }
  // The server counts characters as Python does, one per code point.
  const chars = Array.from(shown);
  let out = '';
  let at = 0;
  for (const link of found) {
    if (link.start < at || link.end > chars.length) continue;
    const words = chars.slice(link.start, link.end).join('');
    const label = link.cell ? `${link.sheet} ${link.cell}` : words;
    const where = (link.sheet ? ` data-link-sheet="${esc(link.sheet)}"` : '') + (link.cell ? ` data-link-cell="${esc(link.cell)}"` : '');
    out += `${esc(chars.slice(at, link.start).join(''))}<button class="ref" data-link="${esc(link.path)}"${where} title="${esc(words)}">${esc(label)}</button>`;
    at = link.end;
  }
  return out + esc(chars.slice(at).join(''));
}

// Ask the server about the texts drawn since the last time, then draw again with their links.
export async function loadLinks(draw) {
  if (asking || !wanted.size || !state.job) return;
  asking = true;
  const links = state.links;
  const texts = [...wanted].slice(0, MAX_TEXTS);
  for (const t of texts) wanted.delete(t);
  try {
    const answer = await api(jobUrl('/links'), { method: 'POST', body: JSON.stringify({ texts }) });
    texts.forEach((t, i) => (links.found[t] = answer.links[i] || []));
  } catch {
    for (const t of texts) links.found[t] = []; // shown as plain text; asked again when the files change
  } finally {
    asking = false;
  }
  if (state.links === links) draw();
}
