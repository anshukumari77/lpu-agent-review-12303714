# Snapshot scope and execution boundary

## Included

- Every Python application module under `src/beep_agent/`, including real access, persistence, provider, OAuth, media, recording, diagnostics and lifecycle code.
- The React application's source, styles, synthetic preview, unit tests, existing browser test source, runtime brand assets and frontend configuration.
- The original Python test source and dependency manifests/locks. Integration tests are included for inspection even when their runtime prerequisites are outside this package.
- `scripts/dev.py` as a small original CLI entrypoint for understanding `localdev.py`. It is not an approved challenge launch command.
- Original component and brand attribution, with unavailable internal-research references removed.

## Packaging-only differences

- The original operational README is replaced with local-review instructions.
- `review/offline.py` is a new packaging-only test entrypoint, not application logic.
- `web/vite.review-preview.config.ts` is a new isolated static-preview configuration, not a change to the normal application.
- An owner-specific absolute directory in the opt-in database-test fixture was replaced with a generic placeholder. The original restriction is not relaxed; that fixture is not a portable database setup recipe and is outside the offline suite.
- No product defect has been intentionally seeded or repaired. Application source and existing frontend behaviour have otherwise been preserved.

## Deliberately absent

No runtime configuration, accounts, credentials, recordings, raw session outputs, client material, previous qualification results, internal research, repository history, deployment files, built assets, environment directories or installed dependencies are supplied. The original operational/diagnostic scripts other than `dev.py` are omitted. Brand artwork source files that are not runtime assets are omitted.

Some original tests name omitted scripts, deployment files or previous local-result documents. These are retained tests of the original application, **not new requirements to reconstruct those artifacts or a promise that the entire original suite runs here**. In particular, tests of container contracts, local deployment configuration, live diagnostic scripts and historical acceptance receipts are outside the supplied execution path. Do not treat intentionally absent packaging artifacts as an application bug.

## What the offline path establishes

The named Python subset uses synthetic inputs, in-memory state and a synthetic inference adapter to execute real domain validation, interview-pack configuration, discovery graph and report rendering. The frontend unit suite uses synthetic data and mocked services. The static preview is explicitly simulated design; it is not a fallback agent.

## What it does not establish

No PostgreSQL persistence/concurrency/lease integration, live OAuth authentication, paid-model inference, native voice, RTC, S3/Egress recording, recording decode, consent propagation across real services, screen-to-evidence-to-report end-to-end quality, long-session reliability, load, cost, regional policy or production security acceptance is claimed. Those source paths remain available for reasoned review without running them.

Dependency installation may need network access. The supplied test entrypoint does not bootstrap dependencies or silently substitute fake production services. If setup is blocked, source review remains possible; report the precise limitation rather than inventing a successful run.
