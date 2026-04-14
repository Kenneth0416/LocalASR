# WebRTC VAD Preview/Final Realtime Transcription Design

## Goal

Replace fixed-second realtime chunking with WebRTC VAD endpointing, and split realtime transcription into two layers:

- low-latency preview revisions using Qwen3-ASR 0.6B
- finalized transcript segments using Qwen3-ASR 1.7B

The same utterance must keep one stable `segment_id` from speech start through all preview revisions and the final transcript commit.

## Scope

- Replace the realtime WebSocket transcription path with a new WebRTC VAD driven transcriber.
- Keep the browser capture format as 16 kHz mono PCM with 20 ms frames.
- Emit preview and final updates for the same utterance through the existing WebSocket `transcript` message type.
- Persist only final transcript segments.
- Keep summary generation and meeting QA grounded only on final transcript segments.
- Add preview and final ASR service separation so preview uses 0.6B and final uses 1.7B.
- Add runtime configuration and readiness visibility for both preview and final ASR services.

## Non-Goals

- No Silero VAD in this change.
- No redesign of the upload transcription path.
- No transcript schema migration to store preview revisions.
- No forced aligner or timestamp-based semantic commit logic for realtime preview decisions.
- No preview persistence across page reloads, meeting history views, or database-backed history APIs.

## Current Constraints

- Browser audio already arrives as 16 kHz mono PCM frames, matching WebRTC VAD input requirements. See [static/audio-worklet.js](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/static/audio-worklet.js:3).
- Server-side realtime audio currently flows through `AudioPacketDecoder` and `audio_worker()`. See [server.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/server.py:91) and [server.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/server.py:713).
- Current realtime transcript handling assumes every `transcript` event is already final and append-only. See [server.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/server.py:797) and [static/app.js](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/static/app.js:1401).
- Current semantic chunking is still driven by chunk duration thresholds and overlap logic, not by utterance lifecycle. See [asr.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/asr.py:394).

## High-Level Architecture

The realtime path changes only in the live WebSocket transcription branch:

1. Browser continues streaming 20 ms PCM frames.
2. `AudioPacketDecoder` continues unpacking frames.
3. `audio_worker()` instantiates a new `WebRTCVADMeetingTranscriber` instead of `SemanticMeetingTranscriber`.
4. The new transcriber runs WebRTC VAD frame-by-frame, manages utterance lifecycle, and emits:
   - preview transcript revisions during active speech
   - one final transcript event when the utterance is sealed
5. Preview events are sent to the browser only.
6. Final events are sent to the browser and then persisted into `MeetingSession` and SQLite.

Upload transcription remains unchanged and continues to use the existing final ASR path.

## State Model

The realtime transcriber uses three states:

- `idle`
- `in_speech`
- `pending_final`

Meaning:

- `idle`: there is no active utterance.
- `in_speech`: the current utterance is collecting audio and may emit preview revisions.
- `pending_final`: the current utterance has been sealed and its final ASR task is running asynchronously.

`pending_final` is a short-lived transition state, not a long-lived holding mode. If new speech starts while an earlier utterance is still `pending_final`, the new speech must open the next utterance immediately.

## Utterance Lifecycle

Each utterance is created at speech start and owns a stable identity for its entire lifecycle.

Rules:

- `segment_id` is allocated exactly once, at speech start.
- `revision` is monotonically increasing for the lifetime of that utterance.
- Preview and final events reuse the same `segment_id`.
- Final does not create a new `segment_id`.
- Once an utterance is sealed, its `end_sample` is frozen before final inference starts.

Required `UtteranceState` fields:

- `segment_id`
- `revision`
- `start_sample`
- `end_sample: int | None`
- `last_speech_sample`
- `pcm_buffer`
- `first_preview_emitted: bool`
- `last_preview_sample`
- `sealed`
- `final_task`
- `last_non_empty_preview_text`

`last_non_empty_preview_text` is needed so final inference failures can fall back to the most recent usable preview text.

## Components

### 1. `WebRTCVADConfig`

Place this in [config.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/config.py), not in `asr.py`.

Required fields:

- `frame_ms=20`
- `vad_aggressiveness=2`
- `enter_speech_frames`
- `endpoint_silence_frames`
- `preview_interval_sec`
- `min_preview_audio_sec`
- `max_utterance_sec`
- `pre_roll_sec`
- `min_final_audio_sec`

These are realtime orchestration parameters and should be exposed through environment variables for tuning.

### 2. `PreviewFinalASRRouter`

Place this in [asr.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/asr.py).

Responsibilities:

- dispatch preview transcription to the 0.6B service
- dispatch final transcription to the 1.7B service
- normalize both outputs to one lightweight result shape

Required return object:

- `text`
- `duration_sec`
- `language: str | None`
- `confidence: float | None`

The transcriber should not depend on full ASR internals such as timestamp segments.

### 3. `WebRTCVADMeetingTranscriber`

Place this in [asr.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/asr.py).

Responsibilities:

- run WebRTC VAD on each 20 ms frame
- open utterances on speech start
- accumulate utterance audio
- trigger preview inference on a timer once enough new audio has accumulated
- seal utterances on endpoint silence or max utterance duration
- run final inference asynchronously
- emit final events in utterance order even if final inference completes out of order

### 4. Server emit helpers

Split the current final-only helper into two explicit paths:

- `emit_preview_segment(...)`
- `emit_final_segment(...)`

Both helpers must filter empty text before doing anything else.

## Preview and Final ASR Policy

Preview behavior:

- starts only after minimum preview audio is available
- reruns on the full utterance buffer every 1.0-1.5 seconds
- emits only if text is non-empty
- does not persist
- does not affect summary cadence
- does not affect meeting QA context

Final behavior:

- runs when endpoint silence is reached
- also runs when utterance duration exceeds `max_utterance_sec`
- must also run on forced flush scenarios
- emits only if text is non-empty or a fallback preview exists
- persists into transcript and database once emitted

## Ordering Rules

Preview events may arrive as soon as their inference finishes.

Final events must be committed in utterance order, not completion order.

This is required because `MeetingSession.add_transcript_segment()` is append-based. If utterance 2 finishes final inference before utterance 1, the server must hold utterance 2 until utterance 1 is resolved, then emit and persist them in `segment_id` order.

## Force-Finalization Paths

The server must force finalization whenever buffered speech exists and any of the following happens:

- websocket `stop`
- websocket disconnect
- manual meeting end
- server shutdown
- session cleanup

If an utterance is:

- `in_speech`: seal it immediately and run final
- `pending_final`: await the existing final task and commit in order

This rule exists to prevent tail speech loss.

## WebSocket Protocol

Keep `type: "transcript"` unchanged.

Segment payload rules:

- realtime rendering key: `segment_id`
- persistence key: `id`
- preview events:
  - `id=""`
  - `is_final=false`
  - `event_type="preview"`
  - `cut_reason="preview_tick"`
- final events:
  - `id=<persisted segment id>`
  - `is_final=true`
  - `event_type="final"`
  - `cut_reason` in:
    - `endpoint`
    - `max_duration`
    - `flush`
    - `disconnect`
    - `shutdown`
    - `final_fallback`

`revision` must increase on every accepted preview and on the final event.

## Persistence and Context Boundaries

Preview data:

- does not enter `session.transcript`
- does not enter SQLite
- does not count toward `SUMMARY_INTERVAL_TURNS`
- does not enter LLM context
- stays in in-memory live preview state owned by the transcriber and may be mirrored into a session-local live preview cache for same-session reconnect recovery

Final data:

- is appended to `session.transcript`
- is persisted to SQLite
- is counted toward summary cadence
- is visible to meeting QA context

`MeetingSession.stream_llm_answer()` and any summary rebuild path must continue to read only final transcript content.

## Frontend Behavior

The frontend must replace append-only transcript rendering with `segment_id`-based upsert behavior.

Required rules:

- if `segment_id` does not exist, create a new live transcript row
- if `segment_id` exists and incoming `revision` is newer, replace text and timing in place
- if incoming `revision` is older or equal, ignore it
- if `is_final=true` and `id` is non-empty, upgrade the live row to persisted/finalized state

The frontend should maintain:

- `liveTranscriptBySegmentId`
- persisted transcript rendering state

Compatibility rule:

- if the backend temporarily emits only final events and no preview events, the frontend must still render correctly

This enables staged rollout.

## Runtime and Diagnostics

The server should expose two ASR services:

- preview ASR service using Qwen3-ASR 0.6B
- final ASR service using Qwen3-ASR 1.7B

Startup and shutdown should initialize and tear down both services.

Health and readiness must surface at least:

- `preview_asr_model`
- `final_asr_model`
- `preview_asr_state`
- `final_asr_state`
- `preview_enabled`

For compatibility, keep the existing single `asr_model` field pointing to the final model.

## Failure and Degradation Rules

### Preview model initialization fails, final model is healthy

- allow meetings to start
- disable preview emission
- expose `preview_enabled=false`
- continue in final-only mode

### Final model initialization fails

- reject realtime meeting start
- return an ASR-specific error as part of the existing startup flow

### Preview inference fails

- log and drop that preview tick
- keep the utterance alive
- allow later preview ticks and final inference to continue

### Final inference fails

- if a non-empty preview text exists for that utterance, emit it as final fallback
- mark `cut_reason="final_fallback"`
- if no usable preview exists, drop the utterance without creating an empty transcript segment

### Empty text handling

- preview empty text: suppress emission
- final empty text: suppress persistence and transcript creation

## Rollout Plan

Allow incremental rollout in three steps:

1. backend supports the new transcriber but may emit final-only
2. frontend ships `segment_id` upsert logic
3. backend enables preview pushes

This keeps the protocol forward-compatible and reduces deployment risk.

## Testing

### Python transcriber tests

Update realtime transcriber coverage in [tests/test_realtime_transcriber.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/tests/test_realtime_transcriber.py):

- speech start allocates one `segment_id`
- preview revisions increment monotonically on one utterance
- final reuses the same `segment_id`
- endpoint silence triggers final
- max utterance duration triggers final
- `pending_final` does not block the next utterance
- final emission order stays aligned with utterance order
- flush, disconnect, and shutdown force finalization
- preview suppression on empty text
- final fallback from last non-empty preview

### WebSocket integration tests

Extend [tests/test_server_websocket.py](/Users/kennethkwok/Documents/Projects/meeting_realtime_voice/tests/test_server_websocket.py):

- one utterance produces multiple preview transcript messages and one final transcript message
- preview events do not persist
- final events do persist
- final-only degraded mode still works when preview ASR is unavailable
- stop and disconnect do not lose the final half sentence

### Frontend tests

Add frontend coverage for transcript upsert behavior:

- create row on first `segment_id`
- replace row on newer revision
- ignore stale revisions
- promote live row to persisted/finalized state on final
- final-only backend mode still renders correctly

## Open Decisions Already Fixed For This Change

- VAD backend is WebRTC only.
- Preview and final use separate models.
- Preview stays out of formal transcript storage.
- Final transcript ordering is utterance order, not inference completion order.
