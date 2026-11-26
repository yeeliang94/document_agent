// The two dividers: run list | conversation, and conversation | files. Each drags, or moves with the arrow keys when
// focused. A double-click (or Enter) on the first one hides the run list; the button left in its place shows it again.
// Widths and the hidden state are kept in this browser, and a kept width that no longer fits the window is narrowed.

import { $, icon, state } from './core.js';

const KEY = 'di-widths';
const GRIP = 8; // the divider's own width, as in styles.css
const SIDE = { min: 180, max: 400, start: 232 };
const FILES_MIN = { list: 340, viewer: 440 }; // an open file's header needs room for its buttons
const CHAT_MIN = 360; // the conversation never gets narrower than this
const STEP = 16;

let kept = {};
try {
  kept = JSON.parse(localStorage.getItem(KEY) || '{}') || {};
} catch {
  kept = {}; // storage blocked or damaged: start from the defaults
}
const keep = () => {
  try {
    localStorage.setItem(KEY, JSON.stringify(kept));
  } catch {
    // storage blocked: the widths last this visit only
  }
};

const wide = () => window.matchMedia('(min-width: 1001px)').matches; // below this the run list is a menu
const clamp = (value, low, high) => Math.round(Math.max(low, Math.min(high, value)));
const sideMax = () => clamp(window.innerWidth - GRIP * 2 - CHAT_MIN - FILES_MIN.viewer, SIDE.min, SIDE.max);
const filesKey = () => (state.open ? 'viewer' : 'list'); // an open file and the file list keep their own widths

function files() {
  const work = $('#work');
  if (!work) return null;
  const room = work.getBoundingClientRect().width;
  const min = FILES_MIN[filesKey()];
  const max = Math.max(min, room - GRIP - CHAT_MIN);
  const start = state.open ? Math.min(room * 0.6, 860) : 380;
  return { work, min, max, width: clamp(kept[filesKey()] ?? start, min, max) };
}

function setGrip(grip, now, low, high) {
  if (!grip) return;
  grip.setAttribute('aria-valuenow', now);
  grip.setAttribute('aria-valuemin', low);
  grip.setAttribute('aria-valuemax', high);
}

// Called after every redraw and when the window changes size.
export function applyWidths() {
  const app = $('.app');
  app.classList.toggle('slim', !!kept.hidden);
  if (!wide()) return;
  const side = clamp(kept.side ?? SIDE.start, SIDE.min, sideMax());
  app.style.setProperty('--side', side + 'px');
  const grip = $('#side-grip');
  setGrip(grip, kept.hidden ? 0 : side, 0, sideMax());
  grip.setAttribute('aria-valuetext', kept.hidden ? 'Run list hidden' : `${side} pixels`);
  const panel = files();
  if (!panel) return;
  panel.work.style.setProperty('--files', panel.width + 'px');
  setGrip($('#files-grip'), panel.width, panel.min, panel.max);
}

function setSide(width) {
  kept.side = clamp(width, SIDE.min, sideMax());
  kept.hidden = false;
}

function setFiles(width) {
  const panel = files();
  if (panel) kept[filesKey()] = clamp(width, panel.min, panel.max);
}

function toggleSide() {
  kept.hidden = !kept.hidden;
  keep();
  applyWidths();
}

document.addEventListener('pointerdown', (e) => {
  const grip = e.button === 0 && e.target.closest?.('.grip');
  if (!grip) return;
  e.preventDefault(); // no text selection while dragging
  const app = $('.app');
  const left = grip.id === 'side-grip';
  const x0 = e.clientX;
  let moved = false;
  grip.setPointerCapture(e.pointerId);
  const move = (m) => {
    if (!moved && Math.abs(m.clientX - x0) < 3) return; // a click (or the first of a double-click) is not a drag
    moved = true;
    app.classList.add('dragging');
    if (left) setSide(m.clientX - app.getBoundingClientRect().left);
    else setFiles($('#work').getBoundingClientRect().right - m.clientX - GRIP / 2);
    applyWidths();
  };
  const end = () => {
    grip.removeEventListener('pointermove', move);
    app.classList.remove('dragging');
    if (moved) keep();
  };
  grip.addEventListener('pointermove', move);
  grip.addEventListener('pointerup', end, { once: true });
  grip.addEventListener('pointercancel', end, { once: true });
});

document.addEventListener('dblclick', (e) => {
  if (e.target.closest?.('#side-grip')) toggleSide();
});

document.addEventListener('click', (e) => {
  if (e.target.closest?.('#side-show')) {
    toggleSide();
    $('#side-grip')?.focus();
  }
});

document.addEventListener('keydown', (e) => {
  const grip = e.target.closest?.('.grip');
  if (!grip || e.metaKey || e.ctrlKey || e.altKey) return;
  const left = grip.id === 'side-grip';
  if (left && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    return toggleSide();
  }
  // Arrows move the divider; Home and End give the panel its smallest and largest width.
  const now = kept.hidden && left ? SIDE.min : Number(grip.getAttribute('aria-valuenow'));
  const arrow = { ArrowLeft: -STEP, ArrowRight: STEP }[e.key];
  const to = arrow ? now + (left ? arrow : -arrow) : { Home: 0, End: 9999 }[e.key]; // left of the files: they grow
  if (to === undefined) return;
  e.preventDefault();
  if (left) setSide(to);
  else setFiles(to);
  keep();
  applyWidths();
});

window.addEventListener('resize', applyWidths);

// The way back when the run list is hidden.
$('#side-grip').insertAdjacentHTML(
  'afterend',
  `<button class="icon-btn side-show" id="side-show" aria-label="Show runs" title="Show runs">${icon('side')}</button>`,
);
applyWidths();
