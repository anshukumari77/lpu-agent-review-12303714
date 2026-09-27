# Implementation provenance and component boundaries

## Owned code

The application under `src/beep_agent/` and the React UI were implemented for this project. The research screened existing Amulet/BEEP materials and platform sources for reusable concepts; this project does not silently import an unrelated Hermes runtime or claim another application's authentication/operational acceptance.

Methodology sources were internal BEEP workflow, evidence, interview and confidence materials and a research-stage evidence contract (not included). This implementation keeps observations, reported claims, inferences, unknowns and corrections distinct. No unsupported business result is seeded into production.

## Adopted components

Exact Python and JavaScript versions are locked in `uv.lock` and `web/package-lock.json`. The selected architecture follows the reusable research pack, rather than installing its entire screened register.

| Component | Actual role | Research licence boundary |
|---|---|---|
| [LiveKit Server](https://github.com/livekit/livekit) | RTC media transport | Apache-2.0 |
| [LiveKit Agents](https://github.com/livekit/agents) / official OpenAI plugin | Client-bound native audio runtime and SDK lifecycle | Apache-2.0 framework; bundled model licences are separate |
| [LiveKit Components](https://github.com/livekit/components-js) | Actual React room/audio/screen components | Apache-2.0 |
| [LiveKit Egress](https://github.com/livekit/egress) | Independent recorder | Apache-2.0 service; packaged codecs/dependencies need separate review |
| [LangGraph](https://github.com/langchain-ai/langgraph) / PostgreSQL checkpointer | Discovery graph and durable checkpoint state | MIT; no paid LangSmith server is required |
| [FastAPI](https://github.com/fastapi/fastapi) / [Pydantic](https://github.com/pydantic/pydantic) | HTTP API and strict portable contracts | MIT |
| [PostgreSQL](https://github.com/postgres/postgres) / psycopg | Canonical state, evidence, jobs and concurrency fences | PostgreSQL's permissive server licence; driver licence separate |
| [React Flow](https://github.com/xyflow/xyflow) / Dagre | Evidence-linked workflow map/layout | React Flow MIT; each dependency retains its own licence |
| Official OpenAI Python SDK | Native realtime and structured Responses/vision integration | SDK licence does not grant model-service rights or usage credits |
| SeaweedFS local S3-compatible service | Local private object-store development fixture | Review the digest-pinned image and its dependencies before distribution |

Internal dated licence evidence and lane manifests are not included. They are research evidence, not perpetual legal assurance or a complete redistribution audit.

Native mode explicitly disables framework-added VAD/STT/TTS pipelines. Optional input transcription is configured in the OpenAI realtime session and must be included in actual bill reconciliation; it is not proof of a cost-free transcript. No turn-detector model weights were qualified or adopted merely because the package exposes them.

OpenTelemetry libraries are installed, while the implemented operational boundary is allowlisted persisted provider usage and payload-withheld logging. A hosted trace exporter and Promptfoo-based scored live suite have not been provisioned. Do not present dependency installation as an operational integration.

## Assets and third-party material

The genuine supplied Amulet wordmark is reused in `web/public/amulet-wordmark.svg`; it is not reconstructed from typography. Internal brand-source handling records are not included. No generated paid artwork or external client screenshots were used.

Tests contain explicitly synthetic workflow content and network-response fixtures. Actual browser/RTC/PostgreSQL results are documented separately from synthetic inference. No client recordings, model keys, private configuration or credentials may enter a source bundle.

## Non-adopted alternatives

No Temporal/DBOS/Prefect/Celery stack is layered under LangGraph. No LangSmith paid deployment, alternate meeting bot, self-hosted speech model, general-purpose agent supervisor or model-controlled business-system executor was added. Cascaded speech and other researched components remain conditional alternatives to compare against the same qualified evaluation cases.
