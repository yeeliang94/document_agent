// Screens for the sandbox engine: the Settings page, the document map, and the workspace's live stage and output.
// app.js owns the state and the shared helpers and hands them over once through configure().

let ui;

export function configure(helpers) {
  ui = helpers;
}

export const DECISIONS = {
  used: ['', 'Used'],
  reference: ['info', 'Reference'],
  not_relevant: ['stopped', 'Not relevant'],
  unclear: ['attention', 'Unclear'],
};

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// One two-way choice, one on/off switch and one number stepper, used everywhere they appear.
export const segment = (name, value, label, on) =>
  `<button type="button" class="${on ? 'active' : ''}" data-segment="${name}" data-value="${ui.esc(value)}" aria-pressed="${on}">${ui.esc(label)}</button>`;
const toggle = (name, label, on) =>
  `<button type="button" id="setting-${name}" class="switch ${on ? 'on' : ''}" data-toggle="${name}" role="switch" aria-checked="${on}" aria-label="${ui.esc(label)}"></button>`;
const stepper = (name, label, value, low, high) => `
  <span class="stepper" role="group" aria-label="${ui.esc(label)}">
    <button type="button" data-step="${name}" data-delta="-1" aria-label="Fewer" ${ui.attr('disabled', value <= low)}>−</button>
    <b>${ui.esc(value)}</b>
    <button type="button" data-step="${name}" data-delta="1" aria-label="More" ${ui.attr('disabled', value >= high)}>+</button>
  </span>`;

// Which stage the current run is in. Only this run's events count: a resumed job keeps its earlier runs' events.
export function sandboxStages() {
  const { state, esc } = ui;
  const engine = state.events.filter((e) => e.type === 'engine');
  const run = Math.max(0, ...engine.map((e) => e.run || 0));
  const current = engine.filter((e) => (e.run || 0) === run);
  const saved = current.some((e) => e.name === 'engine.outputs.saved');
  const reviewing = current.some((e) => e.name === 'engine.review.started');
  const stage = (label, now, done) =>
    `<span class="status ${now ? 'info' : done ? '' : 'stopped'}">${esc(label)}</span>`;
  const stages = [stage('Work', !saved, saved), stage('Check', saved && !reviewing, reviewing)];
  if (state.job.engine_team?.reviewer) stages.push(stage('Review', reviewing, false));
  return `<div class="work-focus sandbox-now" aria-label="Current stage"><div class="stages">${stages.join('')}</div></div>`;
}

function checkRows(check) {
  const { esc } = ui;
  const s = check.summary || {};
  const pill = (ok, text) => `<span class="status ${ok ? '' : 'attention'}">${esc(text)}</span>`;
  const notChecked = '<span class="status stopped">Not checked</span>';
  const rows = [
    ['Deliverable rules', s.deliverable_problems === undefined ? notChecked : pill(!s.deliverable_problems, s.deliverable_problems ? plural(s.deliverable_problems, 'problem') : 'Passed')],
    ['Every file decided', s.files === undefined ? notChecked : pill(s.files_decided === s.files, `${s.files_decided} of ${s.files}`)],
  ];
  if (s.rows_written !== undefined && s.rows_written !== null) {
    rows.push(['Row sources', pill(s.rows_sourced === s.rows_written && s.rows_written > 0, `${s.rows_sourced} of ${s.rows_written}`)]);
  }
  rows.push([
    'Reviewer',
    check.reviews
      ? pill(true, check.doubts ? `${plural(check.doubts, 'doubt')} checked` : 'No doubts')
      : '<span class="status stopped">Not run</span>',
  ]);
  return rows.map(([label, value]) => `<div class="check-row"><span>${label}</span>${value}</div>`).join('');
}

export function sandboxOutput(job) {
  const { esc, icon, btn, detailRow } = ui;
  // The document map has its own page, so it is not offered again as a download.
  const downloads = (job?.artifacts || [])
    .map((a, i) => ({ ...a, index: i }))
    .filter((a) => a.name !== 'document_map.json');
  const files = downloads.length
    ? downloads
        .map(
          (a) =>
            `<button class="btn artifact-download" data-artifact="${a.index}">${icon('down')}${esc(a.name)}</button>` +
            (a.status === 'draft'
              ? '<div class="artifact-badge draft">Draft — not validated.</div>'
              : a.status === 'validated'
                ? '<div class="artifact-badge validated">Checked against its template or format</div>'
                : ''),
        )
        .join('')
    : '<div class="artifact-meta">No output yet.</div>';
  const check = job?.engine_check;
  const issues =
    check && !check.passed && check.issues.length
      ? `<div class="still-to-fix"><h3>Still to fix</h3><ul>${check.issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul></div>`
      : '';
  const team = job?.engine_team;
  const details = [
    ['Engine', 'Sandbox'],
    ['Team', team ? `Lead${team.max_scouts ? ` + ${plural(team.max_scouts, 'scout')}` : ''}${team.reviewer ? ' + reviewer' : ''}` : '—'],
    ['Model calls', job?.request_count ?? '—'],
    ['Questions asked', (job?.engine_questions || []).length],
  ]
    .map(detailRow)
    .join('');
  const hasMap = job?.artifacts?.some((a) => a.name === 'document_map.json');
  return `
    <aside class="right-stack">
      <section class="panel">
        <div class="panel-head"><h2>${icon('file')} Output</h2></div>
        <div class="panel-body">
          ${job?.error ? `<p class="error-message">${esc(job.error)}</p>` : ''}
          ${job?.summary ? `<p class="output-summary">${esc(job.summary)}</p>` : ''}
          ${files}
          ${hasMap ? `<button class="btn map-link" data-view="map">${icon('map')} Document map</button>` : ''}
          ${check ? `<div class="checks">${checkRows(check)}</div>` : ''}
          ${issues}
        </div>
      </section>
      <section class="panel session">
        <div class="panel-head"><h2>${icon('terminal')} Run details</h2></div>
        <div class="panel-body">${details}${btn('diagnostics', 'Diagnostics')}</div>
      </section>
    </aside>`;
}

export function settingsPage() {
  const { state, esc, attr } = ui;
  if (state.settingsError) return `<section class="panel page-panel"><p class="error-message page-error">${esc(state.settingsError)}</p></section>`;
  if (!state.settingsDraft) return '<section class="panel page-panel"><div class="empty-state">Loading settings…</div></section>';
  const v = state.settingsDraft;
  const options = state.settings.options;
  const row = (label, control) => `<div class="settings-row"><span>${esc(label)}</span>${control}</div>`;
  const select = (name, labels) =>
    `<select id="setting-${name}" data-setting="${name}" aria-label="${esc(options[name].label)}">${options[name].choices
      .map((c) => `<option value="${esc(c)}" ${attr('selected', v[name] === c)}>${esc(labels?.[c] ?? c)}</option>`)
      .join('')}</select>`;
  const number = (name) =>
    `<input id="setting-${name}" type="number" data-setting="${name}" min="${options[name].low}" max="${options[name].high}" value="${esc(v[name])}" aria-label="${esc(options[name].label)}">`;
  const sandbox = v.engine === 'sandbox';
  const connection = state.settings.connection;
  const panel = (title, rows) =>
    `<section class="panel settings-panel"><div class="panel-head"><h2>${esc(title)}</h2></div><div class="panel-body">${rows}</div></section>`;
  const agent =
    row('Engine', `<div class="segmented">${segment('engine', 'classic', 'Classic', !sandbox)}${segment('engine', 'sandbox', 'Sandbox', sandbox)}</div>`) +
    (sandbox
      ? row('Parallel scouts', stepper('max_scouts', 'Parallel scouts', v.max_scouts, options.max_scouts.low, options.max_scouts.high)) +
        row('Independent reviewer', toggle('reviewer', 'Independent reviewer', v.reviewer)) +
        row('Proof level', select('proof', { row: 'Sources for every row', none: 'None' })) +
        row('Reasoning effort', select('effort', { low: 'Low', medium: 'Medium', high: 'High' }))
      : '');
  if (!sandbox) return `<div class="settings-grid">${panel('Agent', agent)}</div>`;
  const limits =
    row('Turn limit per run', number('max_turns')) +
    row('Time limit per run (minutes)', number('time_limit_minutes')) +
    row('Token limit per run (thousands)', number('token_limit_thousands')) +
    row('Hold sandbox for an answer (minutes)', number('question_wait_minutes')) +
    row('Sandbox memory', select('sandbox_memory', { '1g': '1 GB', '4g': '4 GB', '16g': '16 GB' }));
  const link =
    row('Endpoint', `<span class="settings-value">${esc(connection.base_url)}</span>`) +
    row('Model', `<span class="settings-value">${esc(connection.model)}</span>`) +
    row('LiteLLM provider header', `<input id="setting-litellm_provider" data-setting="litellm_provider" value="${esc(v.litellm_provider)}" placeholder="None" aria-label="LiteLLM provider header">`) +
    row('Name the model when creating a sandbox', toggle('model_on_create', 'Name the model when creating a sandbox', v.model_on_create)) +
    (connection.warning ? `<p class="error-message settings-warning">${esc(connection.warning)}</p>` : '');
  return `<div class="settings-grid">${panel('Agent', agent)}${panel('Limits', limits)}${panel('Connection', link)}</div>`;
}

export function documentMapPage() {
  const { state, esc, btn } = ui;
  const page = (body) => `<section class="panel page-panel">${body}</section>`;
  if (!state.job) return page('<div class="empty-state">Open a run to see its document map.</div>');
  if (state.mapError) return page(`<p class="error-message page-error">${esc(state.mapError)}</p>`);
  const files = state.documentMap?.files;
  if (!files) return page('<div class="empty-state">Loading the document map…</div>');
  if (state.documentMap.error) return page(`<p class="error-message page-error">${esc(state.documentMap.error)}</p>`);
  if (!files.length) return page('<div class="empty-state">No document map yet.</div>');
  const counts = {};
  for (const f of files) counts[f.decision] = (counts[f.decision] || 0) + 1;
  const filters = [
    ['all', `All · ${files.length}`],
    ...Object.entries(DECISIONS)
      .filter(([k]) => counts[k])
      .map(([k, [, label]]) => [k, `${label} · ${counts[k]}`]),
  ];
  const shown = files.filter((f) => state.mapFilter === 'all' || f.decision === state.mapFilter);
  const folders = {};
  for (const f of shown) {
    const cut = f.path.lastIndexOf('/');
    (folders[cut > 0 ? f.path.slice(0, cut) : '(top folder)'] ||= []).push({ ...f, name: f.path.slice(cut + 1) });
  }
  const rows = Object.entries(folders)
    .map(
      ([folder, items]) =>
        `<tr class="folder-row"><td colspan="3">${esc(folder)}</td></tr>` +
        items
          .map((f) => {
            const [tone, label] = DECISIONS[f.decision] || ['attention', f.decision || 'Missing'];
            return `<tr><td>${esc(f.name)}</td><td><span class="status ${tone}">${esc(label)}</span></td><td>${esc(f.reason)}</td></tr>`;
          })
          .join(''),
    )
    .join('');
  return page(`
      <div class="panel-head">
        <div class="filter-buttons">${filters.map(([k, label]) => `<button class="${state.mapFilter === k ? 'active' : ''}" data-map-filter="${k}">${esc(label)}</button>`).join('')}</div>
        ${btn('workspace', 'Workspace', 'small')}
      </div>
      <table class="trace-table document-map"><thead><tr><th>File</th><th>Decision</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table>`);
}
