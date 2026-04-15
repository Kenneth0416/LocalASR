# Single-Track Realtime ASR Rollback Design

## Goal

Roll the app back from preview/final dual-track realtime ASR to one single realtime transcription lane.

The target behavior is:

- realtime mode keeps WebRTC VAD utterance segmentation
- each utterance is transcribed exactly once through the final ASR model
- the browser receives only final transcript events
- upload mode and history mode remain final-only, unchanged in user-facing behavior

This is a simplification change, not a new feature.

## Scope

- Remove preview ASR as a runtime concept from realtime transcription.
- Remove preview-specific state, status fields, and compatibility branches from frontend and backend.
- Keep WebRTC VAD based utterance segmentation for realtime mode.
- Keep upload transcription, meeting history, export, transcript editing, summary, and QA flows working on final transcript segments only.
- Update tests and docs to describe and verify the single-track architecture.

## Non-Goals

- No change to the upload transcription algorithm.
- No change to SQLite schema or persisted meeting data.
- No redesign of VAD endpointing thresholds as part of this rollback.
- No introduction of streaming token-by-token final transcript rendering.
- No hidden “disabled preview” fallback path left behind in production code.

## Current Constraints

- Browser capture already produces 16 kHz mono PCM frames through the audio worklet. See [static/audio-worklet.js](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/static/audio-worklet.js:3).
- The frontend currently batches four 20 ms frames before sending them to the WebSocket server. See [static/app.js](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/static/app.js:15) and [static/app.js](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/static/app.js:1189).
- Realtime audio processing currently runs through `audio_worker()` and `WebRTCVADMeetingTranscriber`. See [ws_handler.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/ws_handler.py:376) and [vad.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/vad.py:61).
- Dual-track behavior currently leaks into config, service initialization, runtime health/readiness, session preview cache, frontend live transcript state, and tests. See [config.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/config.py:243), [server.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/server.py:52), [session.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/session.py:105), and [static/live-transcript-state.mjs](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/static/live-transcript-state.mjs:1).

## High-Level Architecture

After the rollback, the live path is:

1. Browser captures audio exactly as today.
2. Browser sends framed PCM packets to `/ws/meeting`.
3. `AudioPacketDecoder` continues unpacking frames.
4. `audio_worker()` feeds decoded PCM into `WebRTCVADMeetingTranscriber`.
5. The transcriber uses WebRTC VAD to decide utterance boundaries.
6. When one utterance is sealed, the server runs one final ASR transcription for that utterance.
7. The server persists the final segment and broadcasts one final `transcript` event to the browser.
8. Summary and QA continue to use persisted final transcript only.

Upload mode remains a one-shot final ASR path and does not change architecturally.

## Backend Design

### 1. Runtime configuration and service initialization

The backend must stop modeling preview ASR as a first-class runtime dependency.

Required changes:

- Remove `preview_asr_config` from the loaded config tuple.
- Remove `PREVIEW_ASR_*` and realtime preview timing variables from code paths that affect runtime behavior.
- Remove `DISABLE_PREVIEW_ASR` and any branch that conditionally builds preview support.
- Initialize exactly one ASR service for both realtime final transcription and upload transcription.

`WebRTCVADConfig` stays, because realtime segmentation still exists. Only preview-trigger parameters are removed from the config object.

### 2. Realtime router and transcriber

Realtime mode still needs utterance segmentation, but no longer needs two transcription lanes.

Required changes:

- Keep `WebRTCVADMeetingTranscriber` as the realtime segmentation engine.
- Remove preview-specific fields and flows from the transcriber state machine:
  - no preview task
  - no preview revision tracking
  - no `last_non_empty_preview_text`
  - no preview event emission
- Seal utterances on endpoint silence, max utterance duration, or forced flush.
- Run exactly one final transcription per utterance.
- Emit only final `RealtimeTranscriptEvent` values.

The transcriber may keep its current class name to minimize file churn, but its behavior must become final-only.

### 3. Session and websocket behavior

`MeetingSession` must become final-transcript-only again.

Required changes:

- Remove `LivePreviewSegment`.
- Remove `live_preview_segments`.
- Remove preview upsert and clear helpers.
- Remove websocket preview emit helpers and preview-specific `transcribe_done` phases.
- Keep final transcript ordering guarantees before persistence.

The websocket protocol keeps `type: "transcript"`, but the payload is single-track:

- every emitted segment is final
- every emitted segment has a persisted `id`
- preview-only fields and semantics are removed
- compatibility fields such as `segment_id`, `revision`, and `is_final` may remain temporarily if that reduces rollout churn, but frontend code must not depend on them for final-only rendering

### 4. Runtime status endpoints

Health, readiness, and websocket handshake responses must reflect a single ASR lane.

Required changes:

- `/api/health` reports one ASR state
- `/api/readiness` reports one realtime transcription dependency state
- websocket `ready` payload no longer includes `preview_asr_model`, `preview_asr_state`, or `preview_enabled`

The UI and logs should describe this as one local ASR model path, not a preview/final split.

## Frontend Design

### 1. Realtime transcript rendering

Frontend live rendering must become final-only and append-oriented.

Required changes:

- Remove preview/final differentiation in realtime transcript rendering.
- Remove revision-based upsert behavior from the live transcript UI.
- Remove preview-specific CSS classes and visual treatment.
- Render realtime transcript rows from final segments only.
- Guard against duplicate final inserts by segment `id`, not by preview `segment_id`.

### 2. Frontend state simplification

The frontend no longer needs a preview-aware live transcript state machine.

Required changes:

- Remove `static/live-transcript-state.mjs`.
- Replace its usage in `static/app.js` with final-only transcript rendering state.
- Remove `data-live-segment-id`, `persisted`, and preview-only rendering branches.

History rendering and upload rendering stay final-only and continue to work with persisted transcript segments.

### 3. Status and UX text

Any UI language that implies preview/final split must be removed.

Required changes:

- simplify status logs and runtime labels to one ASR lane
- remove preview-related readiness display assumptions
- keep upload/history/edit/export UX unchanged

## Testing Strategy

This rollback changes behavior, so tests must be rewritten to prove the old preview path is gone.

Required coverage:

- backend config and startup tests verify only one ASR service is initialized and shut down
- health/readiness tests verify single-track payloads and no preview metadata
- websocket tests verify realtime mode emits only final transcript events
- realtime transcriber tests verify utterance segmentation still works without preview emission
- frontend tests verify final-only transcript rendering and duplicate protection by persisted `id`
- upload/history tests continue passing without preview compatibility logic

Preview-specific tests must be removed or replaced, not merely skipped.

## Migration and Compatibility

- No database migration is required.
- Existing meetings remain valid because persisted history already stores only final transcript segments.
- Old preview-related keys may remain in a developer’s local `.env`, but the application will ignore them because runtime code no longer consumes them.
- `.env.example`, README, and internal diagrams must be updated to describe the new single-track architecture.

## Risks and Mitigations

### Risk 1: tail speech loss during forced stop

Mitigation:

- keep explicit flush-on-stop and flush-on-disconnect behavior
- keep websocket tests that verify the final buffered utterance is committed on shutdown paths

### Risk 2: accidental preview compatibility leftovers

Mitigation:

- remove preview state types and helpers entirely instead of leaving dead compatibility branches
- update tests to fail if preview-specific payload fields remain

### Risk 3: frontend duplicate rows after simplification

Mitigation:

- key final-only live rendering by persisted segment `id`
- add a focused frontend test for duplicate-id suppression

## Acceptance Criteria

The rollback is complete only when all of the following are true:

1. Realtime mode initializes one ASR service and emits only final transcript events.
2. Realtime mode still performs VAD-based utterance segmentation.
3. Upload transcription, meeting history, export, and transcript editing remain functional.
4. Health/readiness/websocket status payloads no longer expose preview-lane fields.
5. Preview-specific state, code paths, styles, and tests have been removed or replaced with single-track equivalents.
6. Updated tests pass on the resulting single-track implementation.
