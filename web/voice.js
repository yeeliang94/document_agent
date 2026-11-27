// Speak instead of type: one microphone button for every box people write in.
//
// Press it (or Ctrl+M in the box) and speak. In live mode the words appear where the cursor was while the person
// talks, sent through this app to the transcription service (app/voice.py); press again to stop and the text
// settles. In "after you stop" mode the recording is sent when the person stops and the words stream back. Either
// way the words are written into the box and an input event is fired, so the box's own handler keeps them, exactly
// as if they had been typed.
//
// A box's template draws micButton(id) beside it and voiceNote(id) under it; render() calls syncVoice() after
// drawing. Nothing else is needed.
//
// Each recording belongs to the screen it started on (the run, playbook, client, comment or reply). The same box id
// is reused on every run and playbook, so a recording stops, keeping what it wrote, as soon as its screen is left or
// the box is changed by something other than the recording (typing, or Send clearing it).

import { $, api, esc, icon, request, state } from './core.js';

let redraw = () => {};
let active = null; // the recording being started or listened to; one at a time
const settling = new Set(); // recordings that were stopped and are waiting for their final text

export function initVoice(render) {
  redraw = render;
}

// --- Drawing ---------------------------------------------------------------------------------------------------

const phaseOf = (target) =>
  active?.target === target ? active.phase : [...settling].some((s) => s.target === target) ? 'working' : '';

export function micButton(target, disabled = false, name = '') {
  const phase = phaseOf(target);
  const label = phase === 'listening' ? 'Stop' : phase ? 'Writing out what you said' : name ? `Speak into ${name}` : 'Speak';
  const glyph = phase === 'listening' ? 'stop' : phase ? 'spin' : 'mic';
  // The clock's text is set by syncVoice, so the markup stays the same while it runs and nothing is redrawn.
  const time = phase === 'listening' ? '<span class="mic-time" aria-hidden="true"></span>' : '';
  return `${time}<button type="button" id="mic-${esc(target)}" class="mic ${phase}" data-mic="${esc(target)}" aria-label="${label}" title="${label}" ${disabled && !phase ? 'disabled' : ''}>${icon(glyph)}</button>`;
}

export function voiceNote(target) {
  const note = state.voiceNote;
  if (!note || note.target !== target) return '';
  const action =
    note.action === 'settings' ? '<button type="button" class="btn quiet small" data-view="settings">Settings</button>'
    : note.action === 'retry' ? `<button type="button" class="btn quiet small" data-mic="${esc(target)}">Try again</button>`
    : ''; // prettier-ignore
  return `<div class="voice-note ${note.tone}" role="${note.tone === 'bad' ? 'alert' : 'status'}"><p>${esc(note.text)}</p>${action}
    <button type="button" class="icon-btn" data-voice-dismiss aria-label="Dismiss">${icon('x')}</button></div>`;
}

export function dismissVoiceNote() {
  state.voiceNote = null;
  redraw();
}

function say(target, text, tone = 'bad', action = '') {
  state.voiceNote = { target, text, tone, action };
}

// After every redraw: the clock's time, and the box locked while it listens.
export function syncVoice() {
  if (!active || active.phase !== 'listening') return;
  if (!current(active)) return cancel(active);
  const time = $('.mic-time');
  if (time) time.textContent = clock(active.started);
  $('#' + active.target).readOnly = true;
}

function clock(started) {
  const s = Math.floor((Date.now() - started) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// --- Whose box ------------------------------------------------------------------------------------------------

// The screen a box belongs to. Comment and reply boxes belong to the one comment or reply being written.
const ownerOf = (target) => [
  state.view, state.job?.id, state.book?.id, state.client?.id,
  target === 'note' ? state.commentDraft : null, target === 'reply-text' ? state.reply : null,
]; // prettier-ignore

// The recording's box, if it is still on screen, still on the same screen and holds what the recording last wrote.
function current(s) {
  const box = $('#' + s.target);
  if (!box || ownerOf(s.target).some((part, i) => part !== s.owner[i])) return null;
  if (s.written !== undefined && box.value !== s.written) return null;
  return box;
}

// Where the cursor was when the button was pressed. The button takes the focus on a click, so the box's selection
// is noted as the pointer goes down (rememberCaret), before the focus moves.
let caret = null;
export function rememberCaret(target) {
  const box = $('#' + target);
  caret = box && document.activeElement === box ? { target, start: box.selectionStart, end: box.selectionEnd } : null;
}

function anchor(target) {
  const box = $('#' + target);
  const value = box?.value || '';
  const focused = box && document.activeElement === box;
  const kept = caret?.target === target ? caret : null;
  const start = focused ? box.selectionStart : kept ? kept.start : value.length;
  const end = focused ? box.selectionEnd : kept ? kept.end : value.length;
  caret = null;
  return { before: value.slice(0, start), after: value.slice(end) };
}

// --- Writing into the box -------------------------------------------------------------------------------------

function write(s) {
  const box = current(s);
  if (!box) return false;
  const words = s.text.trim();
  const gapBefore = words && s.before && !/\s$/.test(s.before) ? ' ' : '';
  const gapAfter = words && s.after && !/^\s/.test(s.after) ? ' ' : '';
  const value = words ? s.before + gapBefore + words + gapAfter + s.after : s.original; // nothing heard: as it was
  box.readOnly = s.phase === 'listening'; // typing while it listens would be overwritten by the next words
  s.written = value;
  if (box.value !== value) {
    box.value = value;
    box.dispatchEvent(new Event('input', { bubbles: true }));
  }
  if (document.activeElement === box) {
    const at = (s.before + gapBefore + words).length;
    box.setSelectionRange(at, at);
  }
  return true;
}

// --- Starting and stopping ------------------------------------------------------------------------------------

export async function toggleVoice(target) {
  if (active?.target === target) {
    if (active.phase === 'listening') stop(active);
    return; // still starting: a second press is ignored
  }
  if (phaseOf(target) === 'working') return;
  if (active) active.phase === 'listening' ? stop(active) : cancel(active); // one recording at a time
  await start(target);
}

async function start(target) {
  state.voiceNote = null;
  const box = $('#' + target);
  if (!box || box.disabled) return redraw(); // a box that cannot be typed in cannot be spoken into either
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    say(target, 'Browsers allow the microphone only on a secure (https) address or on this computer. Ask whoever runs the app to put it behind https.');
    return redraw();
  }
  // Claimed before anything is awaited, so a second press while the browser asks for the microphone does nothing.
  // `written` starts as the box's text, so an edit made while the microphone starts stops the recording rather
  // than being overwritten, and `original` is what goes back if no words are heard (a selection stays).
  const original = box.value;
  const s = { target, phase: 'starting', text: '', owner: ownerOf(target), ...anchor(target), original, written: original };
  active = s;
  redraw();
  try {
    Object.assign(s, await api('/api/voice'));
  } catch (e) {
    return fail(s, e.message);
  }
  try {
    s.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    return fail(s, e.name === 'NotFoundError' || e.name === 'OverconstrainedError'
      ? 'No microphone was found on this computer.'
      : 'The browser is blocking the microphone. Allow it from the icon at the left of the address bar, then press the microphone again.'); // prettier-ignore
  }
  if (active !== s) return release(s); // cancelled while the browser asked
  s.phase = 'listening';
  s.started = Date.now();
  s.ticker = setInterval(() => (write(s) ? syncVoice() : cancel(s)), 1000);
  try {
    await (s.mode === 'live' ? startLive(s) : startRecording(s));
  } catch (e) {
    return fail(s, `The microphone could not be started: ${e.message}`);
  }
  if (s.stopSoon) return stop(s);
  redraw();
}

function stop(s) {
  if (s.phase !== 'listening') return;
  // Stop pressed while the recording is still being set up: it stops as soon as it can be.
  if (!s.stop) {
    s.stopSoon = true;
    return;
  }
  s.phase = 'working';
  clearInterval(s.ticker);
  clearTimeout(s.limitTimer);
  if (active === s) active = null;
  settling.add(s);
  write(s); // unlocks the box: it can be typed in while the last words arrive
  redraw();
  s.stop();
}

function finish(s) {
  release(s);
  s.phase = 'done';
  write(s);
  redraw();
}

// What was heard so far stays in the box.
function fail(s, message, action = '') {
  if (active === s || settling.has(s)) say(s.target, message, 'bad', action);
  finish(s);
}

// The screen was left or the box changed: stop listening, keep what was written, say nothing.
function cancel(s) {
  release(s);
  s.phase = 'done';
  const box = $('#' + s.target);
  if (box) box.readOnly = false;
  redraw();
}

function release(s) {
  if (active === s) active = null;
  settling.delete(s);
  clearInterval(s.ticker);
  clearTimeout(s.limitTimer);
  s.stream?.getTracks().forEach((t) => t.stop());
  s.context?.close().catch(() => {});
  if (s.socket && s.socket.readyState <= 1) s.socket.close();
  if (s.recorder?.state === 'recording') s.recorder.stop();
}

// --- Live: audio streams to the app, words stream back --------------------------------------------------------

async function startLive(s) {
  s.context = new AudioContext();
  await s.context.audioWorklet.addModule('/voice-worklet.js');
  const source = s.context.createMediaStreamSource(s.stream);
  const node = new AudioWorkletNode(s.context, 'pcm16', { numberOfOutputs: 0 });
  const held = []; // audio from before the service is ready
  let ready = false;
  let ended = false;
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  s.socket = new WebSocket(`${scheme}://${location.host}/api/voice/live`);
  s.socket.binaryType = 'arraybuffer';
  s.socket.onopen = () => s.socket.send(JSON.stringify({ type: 'start', token: state.token || '' }));
  node.port.onmessage = (e) => {
    if (e.data?.end) {
      ended = true; // sent once the service is ready, after the audio held until then
      if (ready) s.socket.send(JSON.stringify({ type: 'stop' }));
    } else if (ready && s.socket.readyState === 1) s.socket.send(e.data);
    else held.push(e.data);
  };
  s.socket.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === 'ready') {
      ready = true;
      held.splice(0).forEach((piece) => s.socket.send(piece));
      if (ended) s.socket.send(JSON.stringify({ type: 'stop' }));
    } else if (m.type === 'delta') {
      s.text += m.text;
      if (!write(s)) cancel(s);
    } else if (m.type === 'done') {
      s.text = m.text || s.text;
      finish(s);
    } else if (m.type === 'limit') {
      say(s.target, m.message, 'hint');
      stop(s); // the service has the audio up to the limit; its final text follows
    } else if (m.type === 'error') fail(s, m.message, m.action);
  };
  s.socket.onclose = () => {
    if (s.phase !== 'done') fail(s, 'The connection to the transcription service dropped. What was heard so far is kept.', 'retry');
  };
  source.connect(node);
  s.stop = () => {
    s.stream.getTracks().forEach((t) => t.stop());
    node.port.postMessage('flush'); // the worklet sends what it holds, then the stop
  };
}

// --- After you stop: the recording is sent once, the words stream back ----------------------------------------

const TYPES = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'];

async function startRecording(s) {
  const type = TYPES.find((t) => window.MediaRecorder?.isTypeSupported?.(t)) || '';
  const pieces = [];
  s.recorder = new MediaRecorder(s.stream, type ? { mimeType: type } : {});
  s.recorder.ondataavailable = (e) => e.data.size && pieces.push(e.data);
  const stopped = new Promise((resolve) => (s.recorder.onstop = resolve));
  s.recorder.start(1000);
  s.limitTimer = setTimeout(() => {
    say(s.target, s.limit_message, 'hint');
    stop(s);
  }, s.max_minutes * 60000);
  s.stop = async () => {
    if (s.recorder.state === 'recording') s.recorder.stop();
    s.stream.getTracks().forEach((t) => t.stop());
    await stopped;
    // Sent as the request body, not a form, so the server never spools it to a temporary file.
    const audio = new Blob(pieces, { type: (s.recorder.mimeType || type || 'audio/webm').split(';')[0] });
    try {
      const response = await request('/api/voice/transcribe', { method: 'POST', body: audio, headers: { 'Content-Type': audio.type } });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let rest = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        rest += decoder.decode(value, { stream: true });
        const lines = rest.split('\n');
        rest = lines.pop();
        for (const line of lines.filter(Boolean)) {
          const m = JSON.parse(line);
          if (m.type === 'delta') {
            s.text += m.text;
            write(s);
          } else if (m.type === 'done') s.text = m.text || s.text;
          else if (m.type === 'error') return fail(s, m.message, m.action);
        }
      }
      finish(s);
    } catch (e) {
      fail(s, e.message);
    }
  };
}
