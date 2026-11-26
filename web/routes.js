// Each page has its own address (#/runs/<id>, #/playbooks/<id>, …), so Refresh keeps the place and the browser's
// Back and Forward move between the pages the person saw, in order. The address follows what is on screen; going to
// an address opens that page.

import { state } from './core.js';

const LISTS = ['playbooks', 'clients', 'settings'];

// The address of the page on screen, or null while it has nothing to name (a playbook or client still loading).
export function pageAddress() {
  const id = (x) => encodeURIComponent(x);
  switch (state.view) {
    case 'run':
      return state.job ? `#/runs/${id(state.job.id)}` : '#/new';
    case 'log':
      return state.job ? `#/runs/${id(state.job.id)}/log` : '#/new';
    case 'playbook':
      return state.book ? `#/playbooks/${id(state.book.id)}` : null;
    case 'client':
      return state.client ? `#/clients/${id(state.client.id)}` : null;
    default:
      return LISTS.includes(state.view) ? `#/${state.view}` : null;
  }
}

// The page an address names: { view, id }. Anything it does not know is New run.
export function parseAddress(hash) {
  const [kind, raw, extra] = String(hash || '').replace(/^#\/?/, '').split('/');
  let id = '';
  try {
    id = raw ? decodeURIComponent(raw) : '';
  } catch {
    return { view: 'new' };
  }
  if (kind === 'runs' && id) return { view: extra === 'log' ? 'log' : 'run', id };
  if (kind === 'playbooks') return id ? { view: 'playbook', id } : { view: 'playbooks' };
  if (kind === 'clients') return id ? { view: 'client', id } : { view: 'clients' };
  if (LISTS.includes(kind)) return { view: kind };
  return { view: 'new' };
}
