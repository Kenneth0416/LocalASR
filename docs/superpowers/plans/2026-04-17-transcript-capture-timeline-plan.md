# Transcript Capture Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist both the spoken timeline and the ASR capture window for each transcript segment so the UI can show when a sentence was spoken and what audio span was captured.

**Architecture:** Keep `start_time/end_time` as the spoken timeline, add `capture_start_time/capture_duration` as separate metadata, and make the realtime VAD pipeline emit both. Persist the new fields through SQLite and API responses, then render both labels in the transcript UI.

**Tech Stack:** Python 3, FastAPI, SQLite, vanilla JS modules, node test, unittest

---

### Task 1: Realtime timing model

**Files:**
- Modify: `asr_types.py`
- Modify: `vad.py`
- Test: `tests/test_realtime_transcriber.py`

- [ ] Add capture-window fields to realtime events and utterance state.
- [ ] Make VAD preserve pre-roll audio for ASR while using first/last speech frames for spoken timing.
- [ ] Run `venv/bin/python -m unittest tests.test_realtime_transcriber -v`.

### Task 2: Persistence and API

**Files:**
- Modify: `session.py`
- Modify: `persistence.py`
- Modify: `ws_handler.py`
- Modify: `http_endpoints.py`
- Modify: `server.py`
- Test: `tests/test_persistence.py`
- Test: `tests/test_server_endpoints.py`
- Test: `tests/test_server_websocket.py`

- [ ] Extend transcript segments with `capture_start_time/capture_duration`.
- [ ] Migrate SQLite rows and round-trip the new fields through websocket and HTTP payloads.
- [ ] Run `venv/bin/python -m unittest tests.test_persistence tests.test_server_endpoints tests.test_server_websocket -v`.

### Task 3: UI formatting

**Files:**
- Modify: `static/ui-formatters.mjs`
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Test: `tests/test_ui_formatters.mjs`

- [ ] Add pure formatters for spoken range and capture window.
- [ ] Render both labels in transcript blocks.
- [ ] Run `node --test tests/test_ui_formatters.mjs`.
