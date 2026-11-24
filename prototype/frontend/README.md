# Document Intelligence frontend prototype

Throwaway UI prototype for evaluating module setup, live agent activity, and developer observability. No frontend dependencies or paid model calls.

## Design preference for future revisions

Recorded 18 October 2026: minimize helper text and microcopy to reduce frontend clutter. Use clear labels and concise statuses; remove redundant subtitles, hints, badges, and repeated explanations. Show secondary guidance on demand and keep implementation details in diagnostics. Retain accessible labels and brief, actionable errors or material limitations. Apply this across payroll and future modules. Applied to the current prototype: removed repeated subtitles, runtime explanations, onboarding blocks, and footer hints. Kept concise labels/statuses, moved demo limitations to on-demand help, and retained full diagnostic event details.

## Run locally

Run from the project root:

```sh
python3 prototype/frontend/server.py
```

Open <http://127.0.0.1:5173>. Three structurally different layouts share the same in-memory state:

- `?variant=A`: workspace with inputs, agent activity, and output/runtime panels.
- `?variant=B`: guided setup followed by the agent conversation.
- `?variant=C`: execution trace first, with activity alongside it and editable setup in a dialog.

Use the floating arrows or keyboard left/right outside editable fields to switch layouts. The layout selector belongs to this standalone prototype and must not be copied into the production application.

Try **Run payroll agent**. The approximately 20-second scripted run demonstrates file discovery, workbook inspection, LLM visual reading, an optional preview failure, a user question, context compaction, writing, and completion. Answer the period question to continue. Pause/resume, stop, send a clarification, inspect events, filter/search logs, export JSONL, and download illustrative CSV rows. Change to Accounts payable to try the same simulated runtime with another module's context.

Module instructions are reusable context; run instructions apply to one submission. Edits are in memory only. Run history survives navigation between views, but not reload. File pickers retain filenames/metadata only; contents are neither read nor uploaded. Even when real filenames are selected, processing events remain synthetic. No Excel workbook is generated. All durations and token counts are illustrative, and no inference cost is reported.

## Backend connection seam

Replace the scripted event emitter with an SSE subscription to a persisted job event stream. Keep a monotonically ordered sequence/cursor so reconnect can replay missed events, and deduplicate by event ID. Save events before attempting browser delivery. The UI is a consumer of those events, not their authoritative store.

The prototype JSONL shows the proposed shape: event/run/trace/span IDs, timestamp, module, event type, level, action summary, duration, tool arguments/results, usage when available, and a simulation marker. Production must additionally capture deployment/model-route metadata, parent relationships for real spans, error class/retry information, artifact references, and actual usage. Preserve distinction between real, estimated, and unavailable metrics.

Instrument the API, job worker, model factory, tool executor, and subprocess boundaries. Persist useful diagnostics even if a request or tool fails. User-facing updates are concise; developer details are expandable and searchable. Include relevant model instructions, tool schemas, sanitized model request/response content, provider-exposed reasoning summaries where available, and compaction events. Do not depend on private chain-of-thought: it is not available. Exclude secrets/base64 image payloads and use artifact references for bulky data. Sensitive document content in diagnostic storage follows job access and retention.

Logging is a first-implementation requirement. A temporary log export/telemetry outage should not crash an otherwise functioning payroll job; use bounded buffering/local spool, surface logging health, and retain recoverable events. Never silently claim a complete diagnostic record after dropping events.

This is a design prototype, not production-quality agent infrastructure. It deliberately has no authentication, backend processing, durable memory, real streaming transport, or cloud deployment.
