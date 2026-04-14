# Dual-Mode Upload UX Enhancement — Design Spec

**Date:** 2026-04-07
**Status:** Approved

## 1. Overview

Improve the upload mode UX for the dual-mode transcription feature. Three areas are addressed: the bottom button, the transcript empty state, and transcript rendering after upload completes.

## 2. Bottom Button — Upload Mode

### Behavior
- When `state.currentMode === 'upload'`, the `startBtn` button changes:
  - **Label:** "上传并转录"
  - **Icon:** Upload arrow SVG (same as the one in the header file-upload-area)
  - **Click handler:** Calls `handleFileUpload()` (no logic change needed)
- When `state.currentMode === 'realtime'` (default), the button restores to original: "开始会议" + microphone polygon icon.

### Implementation
- Add a `syncStartButton()` function called from `setMode()`.
- `syncStartButton()` updates `el.startBtn`'s inner HTML and aria-label based on mode.
- `handleFileUpload()` already handles the upload logic — no change needed.

### States
| Mode | Button Label | Icon | Visible |
|------|-------------|------|---------|
| realtime | 开始会议 | mic polygon | yes |
| upload (idle, no file) | 上传并转录 | upload arrow | yes |
| upload (uploading) | 上传并转录 | upload arrow + disabled | yes |
| upload (done) | — | — | hidden (stopBtn shown) |

## 3. Transcript Empty State — Upload Mode

### Current Behavior
`setTranscriptEmptyState('upload')` already exists and sets:
- Title: "上传音频文件进行转录"
- Hint: "切换到「上传录音」模式，选择音频文件后点击开始"

### Enhancement
The empty state SVG is still the microphone icon. Change it to the upload icon:

```svg
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="17 8 12 3 7 8"/>
    <line x1="12" x2="12" y1="3" y2="15"/>
</svg>
```

No other copy changes needed — the `setTranscriptEmptyState('upload')` text is already correct.

## 4. Transcript Rendering — After Upload Completes

### Current Behavior
`handleFileUpload()` already calls `addTranscriptSegment()` for each segment returned from the server. This works correctly because:
- `addTranscriptSegment()` removes the empty state and renders each segment
- Segment data from `/api/upload` response is correctly shaped (has `id`, `speaker`, `text`, `start_time`, `end_time`)

### No Change Needed
This part already works correctly. The E2E test confirmed segments appear in the transcript panel.

## 5. Files to Change

| File | Changes |
|------|---------|
| `static/app.js` | Add `syncStartButton()`; call it from `setMode()`; add SVG constant for upload icon; update `setTranscriptEmptyState('upload')` SVG |
| `static/styles.css` | Any styling tweaks if needed for the new button text/icon |

## 6. Acceptance Criteria

- [ ] Switching to upload mode changes `startBtn` label to "上传并转录" with upload icon
- [ ] Switching back to realtime mode restores "开始会议" with mic icon
- [ ] Upload mode empty state shows upload SVG icon
- [ ] After file upload completes, transcript segments render correctly in the transcript column
- [ ] All existing functionality (realtime mode, history, chat) is unaffected
