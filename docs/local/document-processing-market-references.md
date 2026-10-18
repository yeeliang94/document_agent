# References for a folder-to-template document processing agent

Researched 17 October 2026 from first-party product documentation and official repositories. Capabilities below are documented vendor/project claims, not results of hands-on trials. Rankings and architecture recommendations are our assessment. No enterprise files were uploaded and no services were purchased or installed.

## Commercial references

| Product | Documented capabilities | Relevance to this project |
| --- | --- | --- |
| V7 Go | Workflows combining LLMs, Python, branches and human review; file bundles grouping several documents into one entity; visual citations; a commercial Excel export page explicitly describes custom templates. | Closest product reference for mixed documents, structured review and team-specific output. Excel fidelity needs a demonstration using our actual templates. |
| Extend | Extraction pipelines with human review that pauses downstream processing; source citations, corrections, approval/rejection, and machine-versus-reviewed outputs. A separate Review Agent flags likely extraction issues. | Strong reference for review workbench, exception routing and preserving correction history. This does not establish arbitrary Excel-template preservation. |
| Instabase | Classification, grounded extraction, validation checkpoints and human review; packet processing consolidates fields across document classes; batch custom functions support cross-document comparisons. | Strong reference for one business case spanning multiple files and staged validation. Some referenced flow documentation is specifically Enterprise Single-tenant. |
| Reducto | Schema extraction and separate document editing. Edit is advertised to fill PDFs and modify DOCX using instructions or reusable form schemas. | Particularly relevant when the output is a completed Word/PDF document. Do not infer arbitrary XLSX editing from PDF/DOCX support. |
| LandingAI ADE | Parse documents into grounded Markdown; extract fields using JSON Schema or Pydantic; extracted values have source locations. A schema-building product supports natural-language schema creation/refinement. | Strong reference for schema onboarding and field-level evidence. Its public Python client calls an API; open client code is not an open inference backend. |

Sources:

- V7: [workflow model](https://docs.go.v7labs.com/docs/section-1-what-is-a-v7-go-agent), [file bundles](https://docs.go.v7labs.com/docs/file-bundles), [citations](https://docs.go.v7labs.com/docs/ai-citations), [Excel export](https://www.v7labs.com/automations/excel-export). Citation documentation specifically lists PDF input; do not assume identical grounding for all input formats. Excel functionality is described on a commercial product page rather than verified by a test here.
- Extend: [human review](https://docs.extend.ai/workflows/human-review), [Review Agent](https://docs.extend.ai/extraction/review-agent). A model-generated review score is a routing signal, not independent proof of correctness or completeness.
- Instabase: [flow steps](https://docs.instabase.com/flow/step-config-reference), [packet-processing flows](https://docs.instabase.com/flow/guides/packet-processing-flows).
- Reducto: [Edit product](https://reducto.ai/edit), [Edit API reference](https://docs.reducto.ai/api-reference/edit). Check endpoint/version support for the intended format before adopting; a marketing page is not an acceptance test.
- LandingAI: [official Python SDK](https://github.com/landing-ai/ade-python), [schema builder](https://marketing.dev.landing.ai/ade/build-schema).

## Open projects and output tools

See the companion [open-source research](open-source-document-processing-research.md) for extraction and processing projects, licenses and deployment considerations.

Additional components directly relevant to the orchestration/output layers:

- [LangGraph](https://github.com/langchain-ai/langgraph): MIT-licensed orchestration framework documenting durable execution and human-in-the-loop support. Useful for persisted processing/review state, but does not supply document parsing, business rules or a finished operations UI. [Interrupt documentation](https://docs.langchain.com/oss/python/langgraph/interrupts) describes pausing and resuming execution through persisted state.
- [python-docx-template / docxtpl](https://github.com/elapouya/python-docx-template): generates Word documents from existing DOCX templates containing Jinja-style placeholders. Useful for repeatable output after a template has been configured; it does not infer the business meaning of arbitrary untagged templates. Repository identifies LGPL-2.1 licensing.
- [openpyxl documentation](https://openpyxl.readthedocs.io/en/3.1/tutorial.html): useful for ordinary workbook population, but the documentation warns that unsupported workbook content such as shapes can be lost on load/save. Test actual templates rather than assuming perfect preservation.

## What this means for our design

The research supports treating this as several cooperating layers: intake and parsing, schema extraction with evidence, cross-document reconciliation, exception review, and controlled output generation. The separation between source schema and output template is our design recommendation, not a claim that every vendor implements the same architecture.

Use agentic reasoning where the next step depends on ambiguous evidence: classifying unfamiliar inputs, selecting relevant documents, proposing mappings and investigating conflicts. Keep file discovery, counts, arithmetic, identifiers, schema validation and export bookkeeping deterministic where possible.

The most relevant product benchmark is: give the system a representative mixed folder, the team's existing template and instructions, then assess record completeness, conflicts, traceability, output fidelity and time to resolve exceptions. A successful document-to-JSON demo does not establish folder-to-template readiness.

For Enterprise Windows, prefer a small local application with locally controlled document storage and the approved gateway where feasible. Evaluate parser dependencies, model downloads, outbound services and actual gateway protocols individually against the existing environment handoff. A Python library being open source does not prove offline operation, Windows compatibility or permission to use its default hosted services.

Initial reading shortlist: V7 for workflow/product design; Extend for human review; Instabase for packets; Reducto for document output. Select open components after reviewing the companion research and testing synthetic fixtures.
