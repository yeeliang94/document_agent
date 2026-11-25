// Zoom in the file preview: the buttons, Fit (the whole width of a sheet, or a whole page), full screen, and
// Ctrl/Cmd with +, - and 0 or the scroll wheel. Zoom is applied to the drawn preview after each redraw, so changing it
// never rebuilds a 500-row sheet; the drawn HTML holds only which buttons there are.

import { $, icon, state } from './core.js';

const STEPS = [25, 33, 50, 67, 75, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400];
const [LOWEST, HIGHEST] = [STEPS[0], STEPS[STEPS.length - 1]];
// Kept unrounded, so the many small steps of a slow pinch add up; rounded only where it is shown.
const clamp = (level) => Math.min(HIGHEST, Math.max(LOWEST, level));
const shown = () => Math.round(state.zoom.level);

const kind = () => (state.preview?.data && !state.preview.error ? state.preview.data.kind : null);
// An empty sheet has nothing to zoom.
export const canZoom = () => ['pages', 'image', 'text'].includes(kind()) || (kind() === 'sheet' && state.preview.data.rows.length > 0);
const canFit = () => ['sheet', 'pages', 'image'].includes(kind()); // text wraps to the panel already

// A new file opens at 100%; another sheet or page of the same file keeps its zoom.
export function resetZoom() {
  state.zoom = { level: 100, fit: false };
}

export function zoomHtml() {
  if (!canZoom()) return '';
  return `<div class="zoom" role="group" aria-label="Zoom">
    <button data-zoom="out" aria-label="Zoom out" title="Zoom out">${icon('minus')}</button>
    <button class="pct" data-zoom="reset" title="Back to 100%"></button>
    <button data-zoom="in" aria-label="Zoom in" title="Zoom in">${icon('plus')}</button>
    ${canFit() ? `<button class="fit" data-zoom="fit" aria-pressed="${state.zoom.fit}">Fit</button>` : ''}</div>`;
}

export function fullHtml() {
  const label = state.full ? 'Exit full screen' : 'Full screen';
  return `<button class="icon-btn" data-zoom="full" aria-label="${label}" title="${label}" aria-pressed="${state.full}">${icon(state.full ? 'shrink' : 'expand')}</button>`;
}

// What a zoom button does. Returns true when the panel needs drawing again (Fit and full screen change the buttons).
export function zoomAction(name) {
  const zoom = state.zoom;
  if (name === 'full') {
    state.full = !state.full;
    return true;
  }
  if (name === 'fit') {
    zoom.fit = !zoom.fit;
    if (!zoom.fit) zoom.level = 100;
    return true;
  }
  // Stepped from the level as shown, so a click after a pinch to 99.6% ("100%") always moves.
  const at = shown();
  const level = name === 'reset' ? 100 : name === 'in' ? STEPS.find((s) => s > at) : STEPS.findLast((s) => s < at);
  const wasFit = zoom.fit;
  Object.assign(zoom, { level: level ?? zoom.level, fit: false });
  applyZoom();
  return wasFit;
}

// Ctrl/Cmd with the scroll wheel, or a pinch on a trackpad: a smooth change, not a jump to the next step.
export function wheelZoom(deltaY) {
  const wasFit = state.zoom.fit;
  // A wheel notch (about 100) is about a quarter larger or smaller; a pinch sends many small steps.
  const step = Math.max(-50, Math.min(50, deltaY));
  Object.assign(state.zoom, { level: clamp(state.zoom.level * Math.exp(-step * 0.005)), fit: false });
  applyZoom();
  return wasFit;
}

// The widest a page or picture shows at 100%: its own width, or the panel's when it is wider than the panel.
function baseWidth(shot, img) {
  return Math.min(img.naturalWidth, shot.clientWidth - 16);
}

function fitLevel(files) {
  if (kind() === 'sheet') {
    const sheet = files.querySelector('.sheet');
    const table = sheet?.querySelector('.grid');
    if (!table) return null;
    table.style.zoom = ''; // measured at its own size
    return Math.min(100, clamp(((sheet.clientWidth - 2) / table.offsetWidth) * 100));
  }
  const body = files.querySelector('.files-body');
  const shot = files.querySelector('.shot');
  const img = shot?.querySelector('img');
  if (!body || !img?.naturalWidth) return null;
  const pager = files.querySelector('.pager');
  const top = shot.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop;
  const room = {
    width: shot.clientWidth - 16,
    height: body.clientHeight - top - (pager ? pager.offsetHeight + 12 : 0) - 40,
  };
  const base = baseWidth(shot, img);
  const fits = Math.min(room.width / base, (room.height * img.naturalWidth) / (img.naturalHeight * base));
  return clamp(fits * 100);
}

// Show the preview at the zoom chosen. Called after every redraw. Fit measures the file again only when the panel was
// drawn again or the window changed size (`measure`): measuring a 500-row sheet costs two layouts.
export function applyZoom(measure = true) {
  const files = $('#files');
  const work = $('#work');
  if (work) work.classList.toggle('full', !!(state.full && state.open));
  if (!files || !canZoom()) return;
  const zoom = state.zoom;
  if (zoom.fit && measure) zoom.level = fitLevel(files) ?? zoom.level;
  const z = zoom.level / 100;
  const k = kind();
  if (k === 'sheet' || k === 'text') {
    const target = files.querySelector(k === 'sheet' ? '.grid' : '.doc');
    if (target) target.style.zoom = z === 1 ? '' : z;
  } else {
    const shot = files.querySelector('.shot');
    const stage = shot?.querySelector('.stage');
    const img = stage?.querySelector('img');
    if (img && !img.naturalWidth) img.addEventListener('load', () => applyZoom(), { once: true });
    else if (img) {
      const width = Math.round(baseWidth(shot, img) * z);
      Object.assign(stage.style, { width: `${width}px`, maxWidth: 'none' });
      shot.classList.toggle('big', width > shot.clientWidth - 16);
    }
  }
  const pct = files.querySelector('.zoom .pct');
  if (pct) {
    pct.textContent = `${shown()}%`;
    pct.setAttribute('aria-label', `Zoom ${shown()}%. Back to 100%`);
  }
  const out = files.querySelector('[data-zoom="out"]');
  const more = files.querySelector('[data-zoom="in"]');
  if (out) out.disabled = zoom.level <= LOWEST;
  if (more) more.disabled = zoom.level >= HIGHEST;
}
