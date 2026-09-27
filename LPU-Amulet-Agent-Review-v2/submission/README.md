# BEEP workflow-review agent — frozen review snapshot

This is **one example submission of the existing application**, not a newly simplified exercise or a functioning hosted agent. It contains the current Python backend, React frontend, original tests and dependency locks. Relevant application business logic has not been repaired, replaced or seeded with exercise bugs.

**Unqualified local review code. Not deployable production software.** The real workflow-review service has not been qualified for unattended client use. Passing the checks below establishes only the stated local test/build boundary; it does not establish successful autonomous interviews, grounded screen evidence, recording delivery or report quality in real sessions.

## Start with the actual source

No account, API key, database, Docker, microphone or screen permission is needed to read the code or run the selected offline checks.

1. Read `src/beep_agent/domain.py`, `packs.py`, `consent.py` and `config.py`: contracts, interview configuration and admission boundaries.
2. Follow `api.py` / `auth.py` → `store.py` → `worker.py` / `discovery.py` → `reports.py`: access, canonical evidence, jobs, reasoning and report assembly.
3. Inspect `providers.py`, `codex_oauth.py`, `codex_rpc.py`, `realtime.py`, `media.py`, `media_authority.py`, `recording.py`, `media_validation.py` and `maintenance.py`: external integrations and lifecycle controls. These real code paths remain present for review; the offline checks do not qualify them.
4. Follow `web/src/main.tsx`, `entry.ts`, `App.tsx`, `Operator.tsx`, `Review.tsx`, `ReportView.tsx`, `WorkflowMap.tsx` and their tests for the real UI. The separately routed `JourneyPreview.tsx` is simulated design material, not evidence that those real-service paths work.

Use the surrounding challenge instructions for deliverables and the review window. You are not expected to purchase provider access, deploy this application or supply real client material.

## Install dependencies in a fresh extraction

The checked platform used Python 3.12 and Node.js 22.23.1. Run from this `submission/` directory on macOS or Linux, with `uv`, Python 3.12, Node.js 22.23.1 and npm installed:

```sh
uv sync --frozen --extra dev --python 3.12
npm --prefix web ci --ignore-scripts --no-audit --no-fund
```

These are **setup commands**, not offline commands: they may download locked dependencies. No dependencies, compiled output, credential files or runtime state are supplied. Do not copy an existing application runtime configuration into the extraction. The Python manifest supports a broader Python range, but the recorded check used Python 3.12. The locks are preserved from the source, not regenerated for the challenge.

## Verified offline Python path

After setup:

```sh
.venv/bin/python -I -B review/offline.py
```

The packaging-only runner clears inherited configuration, uses an empty temporary home, disables unrelated pytest plugin discovery and loads this snapshot's source explicitly. It rejects five Python audit events: `socket.connect`, `socket.getaddrinfo`, `socket.bind`, `subprocess.Popen` and `os.system`. It does not block every networking/process API, native-library I/O or arbitrary filesystem access. It runs the unchanged synthetic tests in:

- `tests/test_config.py`
- `tests/test_domain.py`
- `tests/test_domain_packs.py`
- `tests/test_discovery.py`
- `tests/test_reports.py`

The frozen check returned **67 passed**. This is real domain/discovery/report code with synthetic providers and in-memory checkpoints—not a live-agent simulation offered as production evidence. The guard is an accidental-I/O safeguard, not a security sandbox for hostile code.

**Do not run `pytest tests` as the offline entrypoint.** The retained wider suite includes opt-in database, RTC, recording, browser, deployment-contract and operational-script tests. Some depend on deliberately omitted private/deployment/verification material. Read them as source; their availability or skipped status is not a successful live check. See `REVIEW_SCOPE.md`.

## Frontend checks

In a clean shell without application/provider credentials:

```sh
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run build
```

The recorded checks returned **104 tests passed across 19 files**, successful TypeScript checking and a successful production-mode frontend build. The build emitted a large-chunk warning. These unit tests use synthetic fixtures; they do not connect to a running agent. The checks were run with network access denied and existing locked dependencies, in a separate extraction.

## Optional design preview — not a functioning agent

After the frontend build:

```sh
npm --prefix web run preview -- --config vite.review-preview.config.ts
```

Open **http://127.0.0.1:5194/preview** (including `/preview`). Stop the local static server with Ctrl-C when finished.

This existing design journey is labelled **Interactive preview · Sample workflow · No recording**. It uses a fictional workflow. Microphone, screen sharing, connection and agent responses are simulated; edits are local and reset on reload. It does not perform model inference, create sessions, persist evidence, record anything or send invitations. Sample report downloads are labelled sample material.

The added review-preview configuration serves the built frontend on loopback, has **no backend proxy**, blocks fetch/WebSocket connections with a content policy and disables microphone/camera/display capture permissions. It is for the preview route only. The ordinary application routes will not operate as an agent in this configuration. A Chromium check verified the design route and simulated controls with no device calls, no API or external requests, and no page errors; a deliberate API fetch was blocked.

Do not use the normal `dev` command, backend entrypoints, `scripts/dev.py`, live-provider probes, deployment tooling or retained integration tests to start real services for this challenge. The snapshot is a review artifact, not an operational setup kit.

## Scope and rights

Operational history, private qualification reports, internal research, deployment configuration, credentials, recordings and runtime data are intentionally absent. `REVIEW_SCOPE.md` distinguishes this packaging boundary from application behaviour.

Original source notices and component/brand attribution are retained in `PROVENANCE.md` and the source. **This snapshot adds no licence grant**, model-service entitlement or right to republish brand assets. Third-party packages retain their own licences; dependencies are not vendored here.
