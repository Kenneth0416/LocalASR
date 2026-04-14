# ASR Prompt Modal Design

## Goal

Optimize the temporary ASR prompt input so it does not crowd the header and does not clear itself after a successful realtime start or upload.

## Scope

- Replace the inline header textarea with a compact trigger button.
- Open a dedicated modal dialog for editing the ASR prompt.
- Show several example prompts inside the dialog and allow one-click apply.
- Preserve the current prompt value after successful realtime start and upload.
- Keep the value ephemeral to the current page session only.

## Non-Goals

- No persistence to meeting history or database.
- No localStorage or cross-reload persistence.
- No backend API changes beyond reusing the existing `asr_prompt` passthrough.

## UX

- Header shows a single `ASR 提示词` trigger with current status.
- Status is `未设置` when empty, otherwise `已设置 · N 字`.
- Clicking the trigger opens a modal dialog with:
  - short guidance on intended usage
  - a multiline textarea
  - 3-4 example prompt chips/buttons
  - a `清空` action
  - a `完成` action
- Clicking an example fills the textarea immediately.
- Closing the dialog keeps the current value.
- Successful meeting start and upload read the current prompt value but do not clear it.

## Implementation Notes

- Introduce a small frontend prompt-state helper so the preservation behavior can be unit-tested.
- Reuse existing dialog/drawer visual language from the history panel where practical.
- Keep the existing request payload shape:
  - WebSocket `start` uses `asr_prompt`
  - `/api/upload` form uses `asr_prompt`

## Testing

- Add frontend unit tests for:
  - prompt status text generation
  - example prompt application
  - transcription snapshot reads without clearing current value
- Run existing transcription option tests and a syntax check for `static/app.js`.
