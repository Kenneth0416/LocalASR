# Phase 0 Local Offline Foundation Design

## Summary

Phase 0 defines the release baseline for a single-machine, offline-first meeting assistant. The goal is not to expand feature scope. The goal is to turn the current local beta into a stable, secure-by-default, local-only web application that can be installed, started, and used reliably on one machine without depending on network connectivity.

The product shape for this phase is:

- single user
- browser UI on `localhost`
- ASR runs locally
- summary and Q&A prefer local models
- meeting data stays on the local machine

This phase is the prerequisite for all later work, including better product UX, desktop packaging, and any future internal-network mode.

## Product Goal

Make the current application good enough to serve as a trustworthy local foundation:

- stable startup and shutdown
- deterministic local runtime
- explicit local-only behavior
- actionable dependency diagnostics
- no known correctness bugs in the core meeting flows
- green verification baseline on the supported runtime

## Non-Goals

Phase 0 does not include:

- desktop shell packaging
- multi-user support
- internal-network shared deployment
- public deployment
- speaker diarization
- meeting search and advanced archive features
- billing, quotas, or cloud account management
- broad UI redesign beyond changes required for safety and clarity

## Constraints

- Release runtime is Python `3.12`. Python `3.11` may remain supported, but Phase 0 validation targets `3.12` first.
- The app remains a FastAPI server plus browser frontend.
- The default deployment mode is `127.0.0.1` only.
- The default operating mode is local-only. Remote LLM use is not part of the release path for this phase.
- Existing realtime transcription, upload transcription, summary, Q&A, history, export, and edit flows must continue to work.

## Phase 0 Outcomes

At the end of Phase 0, a user should be able to:

1. install the app on a clean machine by following one documented path
2. understand which local dependencies are missing before starting a meeting
3. run realtime and upload transcription without hidden network requirements
4. ask questions and generate summaries using local models when available
5. view, edit, export, and delete local meeting history with clear local-data expectations
6. recover from common local failures through actionable UI and API feedback

## Design Principles

### Local-Only by Default

The application must fail closed, not fail open. If a local model or local dependency is missing, the app should say so explicitly instead of silently falling back to a remote provider.

### Explicit Degradation

When a capability is unavailable, the app should degrade in a predictable way:

- ASR unavailable: meetings cannot start
- local LLM unavailable: transcription still works, summary and Q&A are disabled with explanation
- `ffmpeg` unavailable: WAV still works, non-WAV upload is disabled with explanation

### Stable Foundations Before More Features

Phase 0 prioritizes correctness, lifecycle management, and runtime predictability over new product features.

### Single-Machine Safety

Security in this phase means:

- no accidental exposure to the network
- no accidental remote model usage
- clear handling of destructive local actions
- local data paths are explicit and controlled

## Current Gaps This Phase Must Close

Phase 0 directly addresses the following gaps in the current codebase:

- ASR startup prewarm logic exists but is not wired into FastAPI startup.
- Upload transcription can trigger background summary task exceptions for short transcripts.
- Session reconstruction for persisted meetings is unstable and can create registry drift.
- Docker and runtime dependency expectations do not match transcoding requirements.
- Local-only behavior is implied but not enforced as a product contract.
- The supported runtime is documented as Python `3.11` and `3.12`, but current local verification is already drifting onto Python `3.14`.
- The verification baseline is not fully green on the supported release path.

## Architecture

### 1. Runtime Profile

Phase 0 introduces a formal local release profile.

Expected characteristics:

- server binds to `127.0.0.1` by default
- frontend is same-origin with the local server
- ASR uses a local model path only
- summary and chat use a local provider only
- all meeting artifacts are written to local filesystem paths under configured application data directories

The app may keep internal support for non-local provider code paths, but those paths are not part of the supported release profile for this phase. They must not be the silent default.

### 2. Startup and Readiness Pipeline

Startup becomes an explicit lifecycle instead of ad hoc lazy failure.

The app should perform a bounded startup probe for:

- Python version compatibility
- ASR model path presence
- optional aligner path presence
- local LLM availability
- `ffmpeg` availability
- recording and database directory writability

The results feed three surfaces:

- server logs
- `/api/health` and `/api/readiness`
- frontend status and onboarding copy

The user should be able to distinguish between:

- app process is alive
- local dependencies are ready
- a subset of features is disabled

### 3. Capability Model

Phase 0 should formalize feature capability states instead of relying on scattered conditionals.

Minimum capability categories:

- `transcription_realtime`
- `transcription_upload_wav`
- `transcription_upload_transcoded`
- `summary_local`
- `chat_local`
- `history_read_write`

Each capability should resolve to:

- ready
- degraded
- unavailable

This is not a separate microservice or plugin system. It is a small internal runtime contract used by the backend and surfaced to the UI so the app can explain what is actually available on this machine.

### 4. Meeting Session Lifecycle

Realtime and upload flows should converge on one meeting state model:

- create session
- persist transcript and chat
- optionally persist summary
- complete session
- reopen persisted meeting safely

Persisted meeting chat should not recreate unstable in-memory session identities. If a meeting is rehydrated for local Q&A, the registry must stay internally consistent and the object should behave like a recovered session, not a half-new session.

### 5. Local Data Contract

Phase 0 should define and document the local data contract:

- where the SQLite file lives
- where recordings live
- which generated files are retained
- how exports are produced
- what delete means

Delete behavior should remain local and immediate. The user should never have ambiguity about whether data still exists elsewhere.

### 6. Safety Guardrails

Phase 0 does not add full login, but it should add single-machine safety rails:

- keep default bind address on loopback only
- warn loudly when `HOST` is changed away from loopback
- keep dangerous actions explicit in the UI
- mark local-only mode visibly in the interface
- avoid implicit remote provider fallback

For this phase, the safety objective is preventing accidental misuse and accidental exposure, not solving enterprise-grade access control.

## Workstreams

### Workstream A: Core Stability and Correctness

Scope:

- wire startup lifecycle correctly
- eliminate known background task exceptions
- fix recovered session identity handling
- resolve failing ASR timestamp-related verification
- align supported runtime, docs, and tests

Success criteria:

- no uncaught background task errors in normal upload flow
- startup prewarm actually runs
- Python test suite is green on the supported release runtime

### Workstream B: Local-Only Runtime Enforcement

Scope:

- define the supported local-only runtime profile
- make non-local provider usage explicit and non-default
- expose current runtime mode in the UI and health endpoints

Success criteria:

- a user can tell whether the app is operating in local-only mode
- missing local LLM does not silently cause remote calls
- health/readiness responses explain local capability state

### Workstream C: Dependency and Install Experience

Scope:

- add dependency detection for `ffmpeg`, local model paths, and local LLM reachability
- make startup and readiness messages actionable
- update install and troubleshooting docs to one supported path

Success criteria:

- clean-machine install path is documented and reproducible
- startup failures identify the missing dependency directly
- Docker and local runtime docs no longer disagree with actual transcoding requirements

### Workstream D: Local Data Safety

Scope:

- document and verify writable paths
- ensure recording persistence behavior is deterministic
- keep delete, export, and edit behavior explicit and predictable

Success criteria:

- local data locations are documented and configurable
- export and delete continue to work under the supported release profile
- local recording failures are surfaced clearly

### Workstream E: Verification Gate

Scope:

- stabilize existing Python, UI, and end-to-end tests
- add a release smoke path that matches supported local usage
- verify offline and degraded-mode scenarios

Success criteria:

- automated verification is green on release runtime
- there is at least one reliable smoke path for:
  - realtime transcription
  - upload transcription
  - summary
  - chat
  - history export/edit/delete

## User Experience Requirements

### First Start

On first run, the app should communicate:

- this is a local-only application
- data stays on this machine
- which local dependencies are required
- what is currently ready vs unavailable

### Meeting Start

When a meeting cannot start, the user should get one direct reason, for example:

- ASR model missing
- model still loading
- local LLM missing for summary/chat
- microphone permission denied

The user should not need to inspect server logs to understand the immediate blocker.

### Upload Flow

Upload mode should clearly distinguish:

- file accepted
- local transcoding unavailable
- transcription running
- transcription complete
- summary unavailable because local LLM is not ready

### History and Destructive Actions

History remains local-only. Editing transcript, regenerating summary, and deleting meetings should stay available, but the UI must keep the user aware that these actions affect only local stored data.

## Error Handling

Phase 0 error handling should standardize around actionable failure classes:

- configuration error
- missing dependency
- unsupported local environment
- local model unavailable
- runtime processing failure
- local storage failure

Each class should map to:

- structured log message
- API response shape
- user-facing explanation

Phase 0 does not require a full observability platform. It does require that ordinary failures become diagnosable without reading stack traces by default.

## Testing Strategy

Phase 0 verification must cover:

- startup lifecycle and readiness states
- upload mode with and without summary availability
- realtime meeting stop/flush behavior
- local session recovery behavior
- transcoding dependency behavior
- local-only mode behavior
- destructive history operations

Release validation should run on the supported Python runtime, not an arbitrary newer interpreter.

## Acceptance Criteria

Phase 0 is complete when all of the following are true:

- the supported local release runtime is defined and documented
- the app starts with correct startup lifecycle behavior
- local dependency problems are surfaced through health, readiness, and UI messaging
- realtime and upload flows complete without known uncaught background exceptions
- summary and Q&A behavior in local-only mode is explicit and predictable
- local history flows remain functional
- the automated verification baseline is green on the release runtime

## Risks and Deferrals

### Risks

- Local model performance may vary heavily by hardware.
- Full offline summary and chat quality depend on the chosen local LLM and may require later tuning.
- Local transcoding support depends on the installation path for `ffmpeg`.

### Deferred to Later Phases

- desktop shell
- richer archive and search
- speaker diarization
- internal-network multi-user mode
- stronger auth and access control for shared environments
- background job orchestration beyond what is needed for single-machine stability

## Recommended Next Step

After this spec is approved, the next document should be a concrete Phase 0 implementation plan broken into workstreams and ordered tasks. The first implementation target should be Workstream A, because it removes correctness debt that would otherwise contaminate every later phase.
