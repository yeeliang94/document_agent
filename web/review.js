// Tools for checking a file in the preview: find in the file, what the agent changed since the last round of
// comments, and where a row of an output shows up in the files it came from.

import {
  api,
  esc,
  fileUrl,
  icon,
  jobUrl,
  plural,
  state,
  storedPath,
} from "./core.js";

// --- Find in the file --------------------------------------------------------
// state.find is null while the bar is closed, else { query, at, total, jump }. `at` is the match in focus; `total`
// is how many the last drawing found, so the arrows know where to wrap. A sheet or text file is searched here, as
// it is drawn. A PDF is searched on the server, and its matches are kept on the view (view.hits).

const NEEDLE = 200; // characters of a search, as the server allows

export const canFind = (view) =>
  ["sheet", "text", "pages"].includes(view?.data?.kind);

export function openFind() {
  state.find = state.find || { query: "", at: 0, total: 0, jump: false };
}

export function closeFind() {
  state.find = null;
}

const query = () => (state.find?.query || "").trim().slice(0, NEEDLE);

let drawn = 0; // matches drawn so far in this drawing of the viewer

export function startMarks() {
  drawn = 0;
}

// Text with each match of the search marked; the one in focus is marked "now".
export function marked(text) {
  const shown = String(text ?? "");
  const q = query().toLowerCase();
  if (!q) return esc(shown);
  const lower = shown.toLowerCase();
  let out = "";
  let from = 0;
  for (let i = lower.indexOf(q); i !== -1; i = lower.indexOf(q, from)) {
    const now = drawn === state.find.at ? " now" : "";
    out += `${esc(shown.slice(from, i))}<mark class="hit${now}">${esc(shown.slice(i, i + q.length))}</mark>`;
    drawn += 1;
    from = i + q.length;
  }
  return out + esc(shown.slice(from));
}

// How many matches the file has: those just drawn, or for a PDF those the server found.
export function matchCount(view) {
  if (view.data?.kind === "pages")
    return view.hits?.query === query() ? view.hits.list.length : 0;
  return drawn;
}

export function findBarHtml(view) {
  if (!state.find || !canFind(view)) return "";
  const total = matchCount(view);
  state.find.total = total; // the arrows wrap around what was last drawn
  if (state.find.at >= total) state.find.at = 0;
  const pdf = view.data.kind === "pages";
  const waiting = pdf && query() && view.hits?.query !== query();
  const count = !query()
    ? ""
    : waiting
      ? "Searching"
      : total
        ? `${state.find.at + 1} of ${total}${pdf && view.hits.more ? "+" : ""}`
        : view.hits?.error ||
          (pdf && !view.hits.text
            ? "No text to search in this PDF"
            : "No matches");
  return `<div class="find" role="search">
      <input id="find-q" type="search" placeholder="Find in file" aria-label="Find in file" value="${esc(state.find.query)}" maxlength="${NEEDLE}" autocomplete="off">
      <span class="count" aria-live="polite">${esc(count)}</span>
      <button class="icon-btn" data-find-step="-1" aria-label="Previous match" ${total ? "" : "disabled"}>${icon("up1")}</button>
      <button class="icon-btn" data-find-step="1" aria-label="Next match" ${total ? "" : "disabled"}>${icon("down1")}</button>
      <button class="icon-btn" data-find-close aria-label="Close find">${icon("x")}</button>
    </div>`;
}

// Move to the next or previous match. A PDF match on another page turns to that page.
export async function stepFind(delta, turnPage, draw) {
  const find = state.find;
  if (!find?.total) return;
  find.at = (find.at + delta + find.total) % find.total;
  find.jump = true;
  const view = state.preview;
  const hit = view?.data?.kind === "pages" ? view.hits?.list[find.at] : null;
  if (hit && hit.page !== view.page) return turnPage(hit.page, draw);
  draw();
}

// The search changed: start again from the first match, and for a PDF ask the server once typing pauses.
let findTimer;
export function typed(value, turnPage, draw) {
  state.find.query = value;
  state.find.at = 0;
  state.find.jump = true;
  clearTimeout(findTimer);
  const view = state.preview;
  if (view?.data?.kind === "pages")
    findTimer = setTimeout(() => searchPdf(view, turnPage, draw), 300);
  draw();
}

export async function searchPdf(view, turnPage, draw) {
  const q = query();
  if (!state.find || state.preview !== view || view.data?.kind !== "pages")
    return;
  if (!q || view.hits?.query === q) return draw();
  try {
    const { hits, more, text } = await api(
      fileUrl("find", view.path, `&q=${encodeURIComponent(q)}`),
    );
    if (state.preview !== view || query() !== q) return;
    view.hits = { query: q, list: hits, more, text };
  } catch (error) {
    if (state.preview !== view) return;
    view.hits = {
      query: q,
      list: [],
      more: false,
      text: true,
      error: error.message,
    };
  }
  if (!state.find) return draw(); // closed while the server searched
  // Start from the first match on the page on show or after it.
  const at = view.hits.list.findIndex((h) => h.page >= view.page);
  state.find.at = Math.max(at, 0);
  const first = view.hits.list[state.find.at];
  if (first && first.page !== view.page) return turnPage(first.page, draw);
  draw();
}

// After a drawing: bring the match in focus, or what a row was found to be, into view. Only when asked, so a
// redraw while the person scrolls leaves them where they are.
export function revealMarks(root) {
  const view = state.preview;
  if (state.find?.jump) {
    const el = root.querySelector("mark.now, .area.hit.now");
    if (el) {
      el.scrollIntoView({ block: "center", inline: "nearest" });
      state.find.jump = false;
    } else if (
      !query() ||
      view?.data?.kind !== "pages" ||
      view.hits?.query === query()
    )
      state.find.jump = false;
  }
  if (view?.found?.jump) {
    const el = root.querySelector(".found");
    if (el) {
      el.scrollIntoView({ block: "center", inline: "nearest" });
      view.found.jump = false;
    } else if (view.data && (view.data.kind !== "pages" || view.image))
      view.found.jump = false;
  }
}

// Boxes drawn over a PDF page: the search's matches on it and what a row was found to be.
export function pageMarksHtml(view) {
  const box = (b, cls) =>
    `<span class="area ${cls}" style="left:${b.x * 100}%;top:${b.y * 100}%;width:${b.w * 100}%;height:${b.h * 100}%"></span>`;
  let out = "";
  if (view.found?.page === view.page)
    out += view.found.boxes.map((b) => box(b, "found")).join("");
  if (state.find && view.hits?.query === query()) {
    view.hits.list.forEach((h, i) => {
      if (h.page === view.page)
        out += box(h, `hit ${i === state.find.at ? "now" : ""}`);
    });
  }
  return out;
}

// --- What changed since the last round ----------------------------------------
// state.changes holds the two switches, kept as the person moves between outputs. What changed in the file on show
// is loaded from the server when the switch is on, and kept on the view (view.changes).

export const canCompare = (view) =>
  String(view?.path).startsWith("outputs/") &&
  state.rounds.length > 0 &&
  ["sheet", "text"].includes(view?.data?.kind);

export async function loadChanges(view, draw) {
  if (!state.changes.on || !canCompare(view)) return;
  const sheet = view.data.sheet
    ? `&sheet=${encodeURIComponent(view.data.sheet)}`
    : "";
  const key = `${view.path}|${view.data.sheet || ""}`;
  if (view.changes?.key === key) return;
  view.changes = { key, loading: true };
  draw();
  try {
    const found = await api(fileUrl("changes", view.path, sheet));
    if (state.preview === view) view.changes = { key, ...found };
  } catch (error) {
    if (state.preview === view) view.changes = { key, error: error.message };
  }
  if (state.preview === view) draw();
}

// The changed cells ({B3: {value, text}}) or lines ({'4': old line}, with `removed` lines) of the file on show, or
// null with the switch off or nothing to show.
export function changesOn(view) {
  const c = state.changes.on && canCompare(view) ? view.changes : null;
  return c && (c.cells || c.lines) ? c : null;
}

const changeCount = (c) =>
  Object.keys(c.cells || c.lines || {}).length + (c.removed?.length || 0);

// "Changed rows only" applies while there is a change to show; otherwise the whole file stays in view.
export const changedOnly = (view) => {
  const c = changesOn(view);
  return !!c && state.changes.only && changeCount(c) > 0;
};

const toggle = (name, on, label) =>
  `<button type="button" class="switch-label" data-changes="${name}" role="switch" aria-checked="${on}"><span class="switch ${on ? "on" : ""}"></span>${esc(label)}</button>`;

export function changesBarHtml(view) {
  if (!canCompare(view)) return "";
  const c = view.changes;
  const lines = view.data.kind === "text";
  let note = "";
  if (state.changes.on && c) {
    if (c.loading) note = "<span>Comparing</span>";
    else if (c.error) note = `<span class="problem">${esc(c.error)}</span>`;
    else if (c.round == null)
      note = "<span>No round of comments has been sent yet</span>";
    else if (c.new)
      note = `<span class="pill ok">New since round ${c.round}</span>`;
    else if (!c.cells && !c.lines)
      note = "<span>This file cannot be compared</span>";
    else {
      const n = changeCount(c);
      note = n
        ? `<span class="pill ok">${plural(n, lines ? "line" : "cell")} changed since round ${c.round}</span>${toggle("only", state.changes.only, lines ? "Changed lines only" : "Changed rows only")}`
        : `<span>Nothing changed since round ${c.round}</span>`;
    }
  }
  return `<div class="changes">${note}${toggle("on", state.changes.on, "Changes")}</div>`;
}

// --- Where a row came from ------------------------------------------------------
// A row of an output lists the files it came from. The server looks for the row's values in each (state.places):
// in a PDF it finds the page holding most of them, so the list can say "page 2" and open the PDF there with those
// values outlined; for any file it says how each value may be written (`wanted`), so a sheet or text source can
// outline the cells and lines holding them.

const MAX_SOURCES = 8; // sources looked up for one row

// The values of a row as the preview shows them, and as stored, for looking them up in a source.
export function rowValues(data, row) {
  const cells = data?.rows?.[row - 1] || [];
  const values = [];
  cells.forEach((value, c) => {
    const text = data.formats?.[`${data.columns[c]}${row}`]?.text;
    for (const v of [text, value])
      if (v !== null && v !== undefined && v !== "" && typeof v !== "boolean")
        values.push(String(v).slice(0, 200));
  });
  return [...new Set(values)].slice(0, 60);
}

const placeKey = (data, row) =>
  `${state.preview?.path}|${data?.sheet || ""}|${row}`;

const placeOf = (path) =>
  state.places?.key === placeKey(state.preview?.data, state.row)
    ? state.places.by[path]
    : null;

// Look up the selected row in its sources. `update` redraws the list of sources as answers come, with `done` set
// after the last one; `arrived` is told of each answer, so a source opened before its answer came can catch up.
export async function locateRow(update, arrived) {
  const view = state.preview;
  const files = [
    ...new Set(
      (view?.data?.row_sources?.[state.row] || [])
        .map(storedPath)
        .filter(Boolean),
    ),
  ];
  const key = placeKey(view?.data, state.row);
  if (!files.length || state.places?.key === key) return;
  const values = rowValues(view.data, state.row);
  const places = { key, by: {} };
  state.places = places;
  await Promise.all(
    files.slice(0, MAX_SOURCES).map(async (path) => {
      places.by[path] = { loading: true };
      try {
        places.by[path] = await api(jobUrl("/locate"), {
          method: "POST",
          body: JSON.stringify({ path, values }),
        });
      } catch (error) {
        places.by[path] = { error: error.message };
      }
      if (state.places !== places) return;
      update(false);
      arrived(path, places.by[path]);
    }),
  );
  if (state.places === places) update(true);
}

// Where the selected row was found in one source file, for the list: "page 2" for a PDF.
export function placeHtml(path) {
  const place = placeOf(path);
  if (!place) return "";
  if (place.loading) return '<span class="where">Looking</span>';
  if (place.error)
    return `<span class="where problem" title="${esc(place.error)}">Could not search</span>`;
  if (!/\.pdf$/i.test(path)) return "";
  return `<span class="where">${place.page ? `page ${place.page}` : "Not found in the text"}</span>`;
}

// What to outline in a source opened from a row. While the source is still being searched, `waiting` holds it
// open for the answer (see applyPlace).
export function foundFor(path) {
  const place = placeOf(path);
  if (!place || place.loading)
    return { waiting: path, page: null, boxes: [], wanted: [], jump: true };
  return {
    page: place.page || null,
    boxes: place.boxes || [],
    wanted: place.wanted || [],
    jump: true,
  };
}

// The answer for a source that was opened while it was still being searched.
export function applyPlace(view, path, place) {
  if (view?.found?.waiting !== path) return false;
  view.found = {
    page: place.page || null,
    boxes: place.boxes || [],
    wanted: place.wanted || [],
    jump: true,
  };
  return true;
}

// Whether a cell or line of a source holds one of the row's values, as a whole word or number: 2,500.85 is found in
// "Total 2,500.85" but not in "12,500.85".
export function isFound(view, text) {
  const found = view.found;
  if (
    !found?.wanted?.length ||
    text === null ||
    text === undefined ||
    text === ""
  )
    return false;
  if (!found.pattern) {
    const parts = found.wanted.map((w) =>
      w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
    );
    found.pattern = new RegExp(
      `(?<![\\w.,])(?:${parts.join("|")})(?![\\w]|[.,]\\d)`,
      "i",
    );
  }
  return found.pattern.test(String(text));
}
