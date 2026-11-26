// Tells the person when a run needs them while they look elsewhere: when a run asks a question, finishes or stops,
// the computer shows a notification and the browser tab gets a mark (a word before its title and a dot on its icon).
// Both clear when the person is back on the tab. One Settings switch covers both; a run the person is looking at,
// or one they paused or stopped themselves, raises nothing.

import { api, esc, inGroup, jobTitle, state } from './core.js';

const seen = new Map(); // each run's status when last looked at
let mark = null; // { word, colour } while the person is away
const link = document.querySelector('link[rel="icon"]');
const PLAIN = link?.getAttribute('href') || '';
const KINDS = {
  ask: { word: 'Question', colour: '%23d99a1e', title: (name) => `${name} has a question` },
  done: { word: 'Finished', colour: '%232e9e44', title: (name) => `${name} is finished` },
  stop: { word: 'Stopped', colour: '%23d14343', title: (name) => `${name} stopped` },
};

export const permission = () => (typeof Notification === 'undefined' ? 'unsupported' : Notification.permission);
const switchedOn = () => state.settings?.values?.notify !== false;
const away = () => document.visibilityState !== 'visible' || !document.hasFocus();

// What happened to a run that was going and is no longer, or null for nothing to tell (paused or stopped by the
// person, or still going).
function kindOf(job) {
  if (job.status === 'waiting') return 'ask';
  if (job.status === 'paused' && job.cut_short === 'day') return 'stop'; // the day's AI usage ran out, not the person
  if (job.status === 'completed') return 'done';
  if (job.status === 'partial') return job.stopped_because ? 'stop' : 'done';
  if (job.status === 'failed') return 'stop';
  return null;
}

function says(job, kind) {
  if (kind === 'ask') return job.question?.text || 'It is waiting for your answer.';
  if (kind === 'stop') return job.stopped_because || job.error || 'Open the run to see why.';
  return String(job.summary || 'The result is ready to check.').split('\n')[0];
}

// Looks at the runs' statuses after each refresh. `open(id)` opens a run, for a click on a notification.
export async function noticeRuns(jobs, open, draw) {
  for (const row of jobs || []) {
    const before = seen.get(row.id);
    seen.set(row.id, row.status);
    if (!before || !inGroup('active', before) || inGroup('active', row.status) || !switchedOn()) continue;
    if (!['waiting', 'completed', 'partial', 'failed', 'paused'].includes(row.status)) continue;
    if (!away()) continue; // the person is on this tab and sees the run list and the run itself
    let job = row;
    try {
      job = await api(`/api/jobs/${encodeURIComponent(row.id)}`);
    } catch {
      // the list's row still names the run
    }
    const kind = kindOf(job);
    if (!kind) continue;
    setMark(KINDS[kind], draw);
    if (permission() === 'granted') {
      const note = new Notification(KINDS[kind].title(jobTitle(job)), { body: says(job, kind), tag: job.id });
      note.onclick = () => {
        window.focus();
        note.close();
        open(job.id);
      };
    }
  }
}

function setMark(kind, draw) {
  mark = kind;
  if (link) {
    // The app's icon with a dot in the corner, in the colour of what happened.
    const dot = `%3Ccircle cx='25' cy='7' r='6' fill='${kind.colour}' stroke='%23fff' stroke-width='2'/%3E%3C/svg%3E`;
    link.setAttribute('href', PLAIN.replace(/%3C\/svg%3E$/, dot));
  }
  draw();
}

// The tab's title, with the mark in front while the person is away.
export const tabTitle = (title) => (mark ? `${mark.word} · ${title}` : title);

export function clearMark(draw) {
  if (!mark || away()) return;
  mark = null;
  if (link) link.setAttribute('href', PLAIN);
  draw();
}

// Turning the switch on asks the browser for permission, the first time only (the browser remembers the answer).
export async function askPermission(draw) {
  if (permission() !== 'default') return;
  await Notification.requestPermission();
  draw();
}

// Under the switch: a button that lets the browser ask, while it has not yet (the switch starts on), or a sentence
// when the browser will not show notifications.
export function notifyNote(on) {
  if (!on) return '';
  if (permission() === 'default') {
    return '<p class="row-note" id="setting-notify-note"><button type="button" class="btn quiet small" data-action="allow-notify">Allow notifications</button></p>';
  }
  const said = {
    denied: 'Your browser blocks notifications from this app. To allow them, click the icon at the left of the web address, set Notifications to Allow, then reload the page.',
    unsupported: "This browser can't show notifications. The tab still shows a mark.",
  }[permission()];
  return said ? `<p class="${permission() === 'denied' ? 'problem' : 'muted'} row-note" id="setting-notify-note">${esc(said)}</p>` : '';
}
