# Dual-Mode Upload UX Enhancement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the upload mode UX: bottom button changes to "上传并转录" with upload icon, empty state shows upload SVG, transcript rendering already works.

**Architecture:** Single-file frontend change in `static/app.js`. Button state driven by `syncStartButton()` called from `setMode()`. Empty state SVG updated in `setTranscriptEmptyState('upload')`.

**Tech Stack:** Vanilla JS, CSS (no build step)

---

## Task 1: Add SVG Icon Constants

**Files:**
- Modify: `static/app.js:17-15` (near top of file, after imports)

- [ ] **Step 1: Read current app.js constants area**

Run: Read `static/app.js` lines 1-20 to confirm import area.

- [ ] **Step 2: Add SVG constants after imports**

```javascript
// SVG icons for buttons
const MIC_ICON = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <polygon points="5 3 19 12 5 21 5 3" fill="currentColor" stroke="none"/>
</svg>`;

const UPLOAD_ICON = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="17 8 12 3 7 8"/>
    <line x1="12" x2="12" y1="3" y2="15"/>
</svg>`;
```

Add these after line 15 (after `const HISTORY_CHAT_PLACEHOLDER`).

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat(upload-mode): add SVG icon constants for button states"
```

---

## Task 2: Add `syncStartButton()` Function

**Files:**
- Modify: `static/app.js` — add new function, call it from `setMode()` and `resetUI()`

- [ ] **Step 1: Write the function after `setMode()` (around line 322)**

```javascript
function syncStartButton() {
    if (state.currentMode === 'upload') {
        el.startBtn.innerHTML = `${UPLOAD_ICON}<span>上传并转录</span>`;
        el.startBtn.setAttribute('aria-label', '上传并转录');
        el.startBtn.disabled = false;
    } else {
        el.startBtn.innerHTML = `${MIC_ICON}<span>开始会议</span>`;
        el.startBtn.setAttribute('aria-label', '开始会议');
    }
}
```

- [ ] **Step 2: Call `syncStartButton()` at the end of `setMode()`**

Read line 321 (end of `setMode()`). Add `syncStartButton();` as the last line of `setMode()`.

The function should look like:

```javascript
function setMode(mode) {
    state.currentMode = mode;

    if (mode === 'realtime') {
        el.modeRealtimeBtn.classList.add('mode-btn--active');
        el.modeRealtimeBtn.setAttribute('aria-pressed', 'true');
        el.modeUploadBtn.classList.remove('mode-btn--active');
        el.modeUploadBtn.setAttribute('aria-pressed', 'false');
        el.fileUploadArea.style.display = 'none';
    } else {
        el.modeRealtimeBtn.classList.remove('mode-btn--active');
        el.modeRealtimeBtn.setAttribute('aria-pressed', 'false');
        el.modeUploadBtn.classList.add('mode-btn--active');
        el.modeUploadBtn.setAttribute('aria-pressed', 'true');
        el.fileUploadArea.style.display = '';
    }

    syncStartButton();
}
```

- [ ] **Step 3: Call `syncStartButton()` in `resetUI()` (around line 1418)**

Read line 1417-1431 (`resetUI` function). Add `syncStartButton();` as the last line of `resetUI()`.

```javascript
function resetUI() {
    setTranscriptEmptyState(state.currentMode === 'upload' ? 'upload' : 'live');
    setChatEmptyState('live');
    setSummaryEmptyState('live');
    hideMeetingBanner();
    state.chatMessageNodes.clear();
    state.pendingAudioFrames = [];
    state.lastCapturedSeq = -1;
    state.currentEditingSegmentId = null;
    removeThinkingIndicator();
    el.timerDisplay.textContent = '00:00';
    el.recordingTimer.classList.remove('active');
    el.summaryBadge.style.display = 'none';
    setChatComposerEnabled(false, DEFAULT_CHAT_PLACEHOLDER);
    syncStartButton();
}
```

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat(upload-mode): add syncStartButton() to toggle button label/icon by mode"
```

---

## Task 3: Update Empty State SVG for Upload Mode

**Files:**
- Modify: `static/app.js:426-455` (`setTranscriptEmptyState` function)

- [ ] **Step 1: Read current empty state function**

Run: Read `static/app.js` lines 426-456.

- [ ] **Step 2: Add upload SVG as constant and update the upload branch**

Add this constant near the top with the other icons:

```javascript
const UPLOAD_EMPTY_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="17 8 12 3 7 8"/>
    <line x1="12" x2="12" y1="3" y2="15"/>
</svg>`;
```

Then update `setTranscriptEmptyState()` — in the `mode === 'upload'` branch, use `UPLOAD_EMPTY_ICON` instead of the hardcoded SVG:

```javascript
} else if (mode === 'upload') {
    copy = {
        title: '上传音频文件进行转录',
        hint: '选择音频文件后点击上方「上传并转录」按钮',
    };
    el.transcriptList.innerHTML = `
        <div class="empty-state">
            ${UPLOAD_EMPTY_ICON}
            <p>${escapeHtml(copy.title)}</p>
            <p class="hint">${escapeHtml(copy.hint)}</p>
        </div>
    `;
}
```

Note: The current code uses a template literal that always renders the mic SVG. Replace the entire function body with a conditional that uses the correct SVG:

```javascript
function setTranscriptEmptyState(mode = 'live') {
    let svgContent, title, hint;
    if (mode === 'history') {
        svgContent = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M12 8v5l3 3"/>
            <path d="M3.05 11A9 9 0 1 0 5 5.3L3 7"/>
            <path d="M3 3v4h4"/>
        </svg>`;
        title = '从右侧历史面板选择会议';
        hint = '已保存会议的逐段转录会显示在这里，也可以直接在此编辑。';
    } else if (mode === 'upload') {
        svgContent = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="17 8 12 3 7 8"/>
            <line x1="12" x2="12" y1="3" y2="15"/>
        </svg>`;
        title = '上传音频文件进行转录';
        hint = '选择音频文件后点击上方「上传并转录」按钮';
    } else {
        svgContent = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
            <line x1="12" x2="12" y1="19" y2="22"/>
        </svg>`;
        title = '点击下方「开始会议」按钮启动转录';
        hint = '开始后，AI 将持续识别并转录会议内容';
    }

    el.transcriptList.innerHTML = `
        <div class="empty-state">
            ${svgContent}
            <p>${escapeHtml(title)}</p>
            <p class="hint">${escapeHtml(hint)}</p>
        </div>
    `;
}
```

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat(upload-mode): show upload SVG in transcript empty state for upload mode"
```

---

## Task 4: E2E Verification with Chrome DevTools

**Files:**
- No file changes — verification only

- [ ] **Step 1: Switch to upload mode, verify button changes**

Navigate to: `http://localhost:8800/static/index.html`
Take snapshot to verify:
- `startBtn` label shows "上传并转录"
- Button has upload arrow icon

Run: Take snapshot and check for "上传并转录" text.

- [ ] **Step 2: Verify empty state shows upload SVG**

Switch to upload mode (click "上传录音" button). Take snapshot and verify:
- Transcript empty state SVG is the upload arrow icon
- Title text: "上传音频文件进行转录"

- [ ] **Step 3: Switch back to realtime, verify button restores**

Click "实时转录" button. Take snapshot and verify:
- `startBtn` label shows "开始会议"
- Button has mic polygon icon

- [ ] **Step 4: Upload a file and verify transcript renders**

Switch to upload mode, select a test audio file, click "上传并转录". Verify:
- Segments appear in transcript column
- Button is hidden (stopBtn shown)

---

## Spec Coverage Checklist

| Spec Requirement | Task |
|-----------------|------|
| Button label changes to "上传并转录" in upload mode | Task 2 |
| Button icon changes to upload arrow in upload mode | Task 2 |
| Realtime mode restores original button | Task 2 |
| Empty state shows upload SVG icon | Task 3 |
| Transcript renders after upload (already works) | N/A — confirmed |
| resetUI resets button to correct mode state | Task 2 |
