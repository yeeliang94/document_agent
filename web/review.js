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
import { versionsOf } from "./versions.js";

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
  // A spot opened from a comment or a link, or the change Previous and Next moved to.
  const ringed = view?.ring?.jump || state.changes.jump;
  if (ringed) {
    const el = root.querySelector(".ring");
    if (el) el.scrollIntoView({ block: "center", inline: "nearest" });
    if (el || (view?.data && (view.data.kind !== "pages" || view.image) && !view.changes?.loading)) {
      if (view?.ring) view.ring.jump = false;
      state.changes.jump = false;
    }
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

// --- What changed since the last round, or since a version ---------------------
// state.changes holds the switches, kept as the person moves between outputs, and what to compare with: the files as
// they were at the last round of comments (since: null, on the latest only) or any version older than the one on
// show. What changed in the file on show is loaded from the server when the switch is on, and kept on the view
// (view.changes), with the change in focus for Previous and Next.

// The version on show: the one picked in the Version menu, else the newest.
const shownNumber = (view) => view?.version ?? state.versions?.[0]?.number ?? null;

// What the file on show can be compared with.
export function sinceChoices(view) {
  const choices = [];
  if (!view?.version && state.rounds.length) choices.push({ id: null, name: "Your last comments" });
  const shown = shownNumber(view);
  for (const v of versionsOf(view?.path)) {
    if (shown != null && v.number < shown)
      choices.push({ id: v.number, name: `Version ${v.number}`, at: v.at });
  }
  return choices;
}

// The choice in use: the one picked, or the first there is when that one does not apply here.
export const sinceOf = (view) => {
  const choices = sinceChoices(view);
  return choices.find((c) => c.id === state.changes.since) || choices[0];
};

export const canCompare = (view) =>
  String(view?.path).startsWith("outputs/") &&
  ["sheet", "text"].includes(view?.data?.kind) &&
  sinceChoices(view).length > 0;

const changesKey = (view) =>
  `${view.path}|${view.data?.sheet || ""}|${view.version ?? ""}|${sinceOf(view)?.id ?? ""}`;

export async function loadChanges(view, draw) {
  if (!state.changes.on || !canCompare(view)) return;
  const key = changesKey(view);
  if (view.changes?.key === key) return;
  const since = sinceOf(view).id;
  const more = [
    view.data.sheet ? `&sheet=${encodeURIComponent(view.data.sheet)}` : "",
    view.version ? `&version=${view.version}` : "",
    since != null ? `&since=${since}` : "",
  ].join("");
  view.changes = { key, loading: true };
  draw();
  try {
    const found = await api(fileUrl("changes", view.path, more));
    if (state.preview === view && view.changes.key === key) view.changes = { key, ...found };
  } catch (error) {
    if (state.preview === view && view.changes.key === key) view.changes = { key, error: error.message };
  }
  if (state.preview === view) draw();
}

// The changed cells ({B3: {value, text}}) or lines ({'4': old line}, with `removed` lines) of the file on show, or
// null with the switch off or nothing to show.
export function changesOn(view) {
  const c = state.changes.on && canCompare(view) ? view.changes : null;
  return c && c.key === changesKey(view) && (c.cells || c.lines) ? c : null;
}

const changeCount = (c) =>
  Object.keys(c.cells || c.lines || {}).length + (c.removed?.length || 0);

// The changed cells or lines in the order Previous and Next step through them: down the file.
export function changedList(view) {
  const c = changesOn(view);
  if (!c) return [];
  if (c.cells) return Object.keys(c.cells);
  return Object.keys(c.lines).sort((a, b) => Number(a) - Number(b));
}

// The cell ("B3") or line ("4") in focus, ringed in the file.
export function changeInFocus(view) {
  const list = changedList(view);
  if (!list.length) return null;
  state.changes.step = Math.min(Math.max(state.changes.step, 0), list.length - 1);
  return list[state.changes.step];
}

export function stepChange(delta) {
  state.changes.step += delta;
  state.changes.jump = true;
}

// "Changed rows only" applies while there is a change to show; otherwise the whole file stays in view.
export const changedOnly = (view) => {
  const c = changesOn(view);
  return !!c && state.changes.only && changeCount(c) > 0;
};

const toggle = (name, on, label) =>
  `<button type="button" class="switch-label" data-changes="${name}" role="switch" aria-checked="${on}"><span class="switch ${on ? "on" : ""}"></span>${esc(label)}</button>`;

const at = (iso) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";

// The Since dropdown: what the Changes switch compares with.
function sinceHtml(view) {
  const since = sinceOf(view);
  const open = state.menu === "since";
  const label = since.id == null ? "Since your last comments" : `Since version ${since.id}`;
  const items = sinceChoices(view)
    .map(
      (c) => `<button role="menuitemradio" aria-checked="${c.id === since.id}" data-compare="${c.id ?? ""}"><span class="v">${esc(c.name)}</span>${c.at ? `<span class="when">${esc(at(c.at))}</span>` : ""}</button>`,
    )
    .join("");
  return `<span class="menu-wrap"><button type="button" class="picker" data-menu="since" aria-haspopup="menu" aria-expanded="${open}">${esc(label)}${icon("down1")}</button>
    ${open ? `<div class="drop" role="menu" aria-label="Compare with">${items}</div>` : ""}</span>`;
}

export function changesBarHtml(view) {
  if (!canCompare(view)) return "";
  const on = state.changes.on;
  const c = view.changes?.key === changesKey(view) ? view.changes : null;
  const lines = view.data.kind === "text";
  let detail = "";
  if (on) {
    const since = sinceOf(view);
    detail = sinceHtml(view);
    if (!c || c.loading) detail += '<span class="count">Comparing</span>';
    else if (c.error) detail += `<span class="count problem">${esc(c.error)}</span>`;
    else if (since.id == null && c.round == null)
      detail += '<span class="count">No round of comments has been sent yet</span>';
    else if (c.new)
      detail += `<span class="count">New since ${since.id == null ? "your last comments" : `version ${since.id}`}</span>`;
    else if (!c.cells && !c.lines)
      detail += '<span class="count">This file cannot be compared</span>';
    else {
      const n = changeCount(c);
      const list = changedList(view);
      const step = changeInFocus(view) == null ? 0 : state.changes.step;
      detail += n
        ? `<span class="count">${plural(n, "change")}
            <button class="icon-btn" data-change-step="-1" aria-label="Previous change" ${step <= 0 ? "disabled" : ""}>${icon("up1")}</button>
            <button class="icon-btn" data-change-step="1" aria-label="Next change" ${step >= list.length - 1 ? "disabled" : ""}>${icon("down1")}</button></span>
          ${toggle("only", state.changes.only, lines ? "Changed lines only" : "Changed rows only")}`
        : '<span class="count">Nothing changed</span>';
    }
  }
  return `<div class="changes">${toggle("on", on, "Changes")}${detail}</div>`;
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

// --- Which value came from which file ---------------------------------------------
// The server says where in each source it found each of the row's values (place.values). A row's cells are grouped
// by where they came from, the way a person would read them: "Employee ID, name · employee_report.xlsx, Employees,
// row 2". A worked-out cell says what it was worked out from.

// A heading as it reads inside a list: "name" after "Employee ID", but "ID" stays as it is.
const lower = (label) =>
  /^[A-Z][a-z]/.test(label) ? label[0].toLowerCase() + label.slice(1) : label;

// The row's filled cells: the heading over each (row 1 of the sheet), what it shows, and its formula if any.
export function rowFields(data, row) {
  const cells = data?.rows?.[row - 1] || [];
  const heads = row > 1 ? data.rows[0] || [] : [];
  const fields = [];
  cells.forEach((value, c) => {
    if (value === null || value === undefined || value === "") return;
    const at = `${data.columns[c]}${row}`;
    const look = data.formats?.[at] || {};
    const head = heads[c];
    fields.push({
      label:
        typeof head === "string" && head.trim()
          ? head.trim()
          : `Column ${data.columns[c]}`,
      shown:
        typeof look.text === "string"
          ? look.text
          : typeof value === "number"
            ? value.toLocaleString("en", { maximumFractionDigits: 6 })
            : String(value),
      number: typeof value === "number",
      formula: typeof look.formula === "string" ? look.formula : "",
      keys: [look.text, value]
        .filter((v) => v !== null && v !== undefined && v !== "")
        .map((v) => String(v).slice(0, 200)),
    });
  });
  return fields;
}

// A formula in the row's own words: =C2*12 reads "salary × 12".
function workedOut(formula, data, row) {
  const heads = data.rows[0] || [];
  return formula
    .replace(/^=/, "")
    .replace(/\$?([A-Z]{1,3})\$?(\d+)/g, (ref, col, r) => {
      const head = heads[data.columns.indexOf(col)];
      return Number(r) === row && typeof head === "string" && head.trim()
        ? lower(head.trim())
        : ref;
    })
    .replace(/\*/g, " × ")
    .replace(/\//g, " ÷ ")
    .replace(/\s+/g, " ")
    .trim();
}

// The rows of the "came from" table: each group of cells with the source it was found in, or why there is none.
// `files` are the row's stored source paths, in the order the agent gave them. `left` are the sources no value was
// found in, so they can still be opened (a picture, a scan).
export function rowProof(data, row, files) {
  const places = files.slice(0, MAX_SOURCES).map((path) => [path, placeOf(path)]);
  const looking = places.some(([, p]) => !p || p.loading);
  const used = new Set();
  const groups = [];
  for (const field of rowFields(data, row)) {
    let from = null;
    if (!field.formula) {
      for (const [path, place] of places) {
        const key = field.keys.find((k) => place?.values?.[k]);
        if (key) {
          from = { path, where: place.values[key] };
          break;
        }
      }
    }
    if (from) used.add(from.path);
    const status = field.formula
      ? `Worked out: ${workedOut(field.formula, data, row)}`
      : from
        ? ""
        : looking
          ? "Looking"
          : "Not found in its sources";
    const last = groups[groups.length - 1];
    // Cells next to each other from the same place read as one line; a number keeps a line of its own.
    const same =
      last &&
      !field.number &&
      !last.number &&
      (from
        ? last.from?.path === from.path && last.from?.where === from.where
        : !last.from && last.status === status);
    if (same) {
      last.labels.push(field.label);
      continue;
    }
    groups.push({
      labels: [field.label],
      number: field.number,
      shown: field.number ? field.shown : "",
      from,
      status,
    });
  }
  for (const g of groups)
    g.label = g.labels.map((l, i) => (i ? lower(l) : l)).join(", ");
  return { groups, left: files.filter((p) => !used.has(p)) };
}
