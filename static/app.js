/**
 * Meeting Realtime Voice — Three-Column Layout App
 * Connects to the WebSocket server, handles audio capture, and manages history.
 */

import { requestRealtimeAudioStream, describeRealtimeAudioSupport } from '/static/audio-support.mjs';
import { ASR_PROMPT_EXAMPLES, createAsrPromptDraft } from '/static/asr-prompt-state.mjs';
import { createFinalTranscriptStore } from '/static/final-transcript-store.mjs';
import { appendTranscriptionOptions, buildRealtimeStartPayload } from '/static/transcription-options.mjs';
import { escapeHtml, formatSummary } from '/static/ui-formatters.mjs';

const TARGET_SAMPLE_RATE = 16000;
const AUDIO_FRAME_MS = 20;
const AUDIO_FRAME_SAMPLES = Math.round((TARGET_SAMPLE_RATE * AUDIO_FRAME_MS) / 1000);
const AUDIO_BATCH_FRAMES = 4;
const MESSAGE_TIMEOUT_MS = 10000;
const MODEL_INIT_TIMEOUT_PADDING_MS = 5000;
const DEFAULT_CHAT_PLACEHOLDER = '输入问题，按 Enter 发送...';
const HISTORY_CHAT_PLACEHOLDER = '历史会议为只读内容；开始新会议后可继续提问';

// SVG icons for buttons
const MIC_ICON = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <polygon points="5 3 19 12 5 21 5 3" fill="currentColor" stroke="none"/>
</svg>`;

const UPLOAD_ICON = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="17 8 12 3 7 8"/>
    <line x1="12" x2="12" y1="3" y2="15"/>
</svg>`;

const UPLOAD_EMPTY_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
    <polyline points="17 8 12 3 7 8"/>
    <line x1="12" x2="12" y1="3" y2="15"/>
</svg>`;

const state = {
    ws: null,
    stream: null,
    audioContext: null,
    sourceNode: null,
    processorNode: null,
    isRecording: false,
    isConnected: false,
    isMeetingActive: false,
    sessionId: null,
    startTime: 0,
    recordingTimerId: null,
    chatMessageNodes: new Map(),
    thinkingIndicator: null,
    logPanelOpen: false,
    pendingAudioFrames: [],
    lastCapturedSeq: -1,
    drainResolver: null,
    drainRejector: null,
    historyPanelOpen: false,
    meetingHistory: [],
    selectedMeetingId: null,
    selectedMeeting: null,
    currentEditingSegmentId: null,
    lastCompletedSessionId: null,
    // Mode toggle state
    currentMode: 'realtime', // 'realtime' or 'upload'
    selectedFile: null,
    isUploading: false,
    asrPromptDialogOpen: false,
    asrPromptDraft: createAsrPromptDraft(),
    finalTranscriptStore: createFinalTranscriptStore(),
};

const $ = (id) => document.getElementById(id);

const el = {
    startBtn: $('startBtn'),
    stopBtn: $('stopBtn'),
    chatInput: $('chatInput'),
    sendBtn: $('sendBtn'),
    refreshSummaryBtn: $('refreshSummaryBtn'),
    toggleLogBtn: $('toggleLogBtn'),
    historyBtn: $('historyBtn'),

    transcriptList: $('transcriptList'),
    segmentCount: $('segmentCount'),
    chatList: $('chatList'),
    msgCount: $('msgCount'),
    summaryText: $('summaryText'),
    summaryBadge: $('summaryBadge'),
    logContent: $('logContent'),
    logPanel: $('logPanel'),
    meetingBanner: $('meetingBanner'),
    meetingBannerLabel: $('meetingBannerLabel'),
    meetingBannerMeta: $('meetingBannerMeta'),

    statusValue: $('statusValue'),
    statusItem: $('statusItem'),
    micStatus: $('micStatus'),
    micStatusItem: $('micStatusItem'),
    processingStatus: $('processingStatus'),
    connectionBadge: $('connectionBadge'),
    languageSelect: $('languageSelect'),
    recordingTimer: $('recordingTimer'),
    timerDisplay: $('timerDisplay'),

    // Mode toggle elements
    modeToggle: $('modeToggle'),
    modeRealtimeBtn: $('modeRealtimeBtn'),
    modeUploadBtn: $('modeUploadBtn'),
    fileUploadArea: $('fileUploadArea'),
    audioFileInput: $('audioFileInput'),
    fileUploadLabel: $('fileUploadLabel'),
    fileName: $('fileName'),
    uploadProgressContainer: $('uploadProgressContainer'),
    uploadProgressBar: $('uploadProgressBar'),
    asrPromptTrigger: $('asrPromptTrigger'),
    asrPromptTriggerStatus: $('asrPromptTriggerStatus'),
    asrPromptTriggerPreview: $('asrPromptTriggerPreview'),
    asrPromptDialog: $('asrPromptDialog'),
    asrPromptBackdrop: $('asrPromptBackdrop'),
    asrPromptCloseBtn: $('asrPromptCloseBtn'),
    asrPromptDoneBtn: $('asrPromptDoneBtn'),
    asrPromptClearBtn: $('asrPromptClearBtn'),
    asrPromptExamples: $('asrPromptExamples'),
    asrPromptInput: $('asrPromptInput'),

    historyDrawer: $('historyDrawer'),
    historyBackdrop: $('historyBackdrop'),
    historyCloseBtn: $('historyCloseBtn'),
    historyRefreshBtn: $('historyRefreshBtn'),
    historyResetViewBtn: $('historyResetViewBtn'),
    historyList: $('historyList'),
    historyEmpty: $('historyEmpty'),
    historySelectionMeta: $('historySelectionMeta'),
    downloadTxtBtn: $('downloadTxtBtn'),
    downloadMdBtn: $('downloadMdBtn'),
    downloadJsonBtn: $('downloadJsonBtn'),
    downloadRecordingBtn: $('downloadRecordingBtn'),
    regenerateStoredSummaryBtn: $('regenerateStoredSummaryBtn'),
    deleteMeetingBtn: $('deleteMeetingBtn'),
};

let latestSegmentEl = null;

function formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function formatMeetingTime(value) {
    if (!value) return '未知时间';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', { hour12: false });
}

function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
    return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function getSegmentStart(segment) {
    const value = segment?.start_time ?? segment?.start ?? 0;
    return Number.isFinite(Number(value)) ? Number(value) : 0;
}

function getSegmentEnd(segment) {
    const value = segment?.end_time ?? segment?.end ?? 0;
    return Number.isFinite(Number(value)) ? Number(value) : 0;
}

function formatSegmentRange(segment) {
    return `${getSegmentStart(segment).toFixed(1)}s – ${getSegmentEnd(segment).toFixed(1)}s`;
}

function getMeetingStatusLabel(status) {
    switch (status) {
        case 'completed':
            return '已完成';
        case 'disconnected':
            return '中断结束';
        case 'active':
            return '进行中';
        default:
            return status || '未知';
    }
}

function getMeetingStatusClass(status) {
    switch (status) {
        case 'completed':
            return 'is-completed';
        case 'disconnected':
            return 'is-disconnected';
        case 'active':
            return 'is-active';
        default:
            return '';
    }
}

function meetingToHistorySummary(meeting) {
    const summary = typeof meeting.summary === 'string' ? meeting.summary : '';
    return {
        session_id: meeting.session_id,
        created_at: meeting.created_at,
        updated_at: meeting.updated_at,
        ended_at: meeting.ended_at,
        status: meeting.status,
        transcript_count: meeting.transcript_count ?? meeting.transcript?.length ?? 0,
        chat_count: meeting.chat_count ?? meeting.chat_history?.length ?? 0,
        summary,
        summary_preview: (meeting.summary_preview || summary || '').slice(0, 160),
        summary_updated_at: meeting.summary_updated_at,
        summary_turn_count: meeting.summary_turn_count ?? 0,
        recording_path: meeting.recording_path,
        recording_bytes: meeting.recording_bytes ?? 0,
        recording_error: meeting.recording_error,
    };
}

function upsertMeetingHistorySummary(meeting) {
    const summary = meetingToHistorySummary(meeting);
    const index = state.meetingHistory.findIndex((item) => item.session_id === summary.session_id);
    if (index >= 0) {
        state.meetingHistory[index] = summary;
    } else {
        state.meetingHistory.unshift(summary);
    }
    state.meetingHistory.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    renderMeetingHistoryList();
}

function removeMeetingHistorySummary(sessionId) {
    state.meetingHistory = state.meetingHistory.filter((item) => item.session_id !== sessionId);
    renderMeetingHistoryList();
}

function updateTranscriptCount() {
    const count = el.transcriptList.querySelectorAll('.transcript-block').length;
    el.segmentCount.textContent = `${count} 条`;
}

function updateMessageCount() {
    const count = el.chatList.querySelectorAll('.chat-message').length;
    el.msgCount.style.display = count ? '' : 'none';
    el.msgCount.textContent = `${count} 条`;
}

function appendLog(message, type = 'info') {
    const ts = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    const item = document.createElement('div');
    item.className = `log-entry ${type}`;
    item.textContent = `[${ts}] ${message}`;
    el.logContent.prepend(item);
    while (el.logContent.children.length > 60) {
        el.logContent.lastChild.remove();
    }
}

function setStatus(text, type = '') {
    el.statusValue.textContent = text;
    el.statusItem.className = 'status-cell' + (type ? ` is-${type}` : '');
}

function setMicStatus(text, type = '') {
    el.micStatus.textContent = text;
    el.micStatusItem.className = 'status-cell' + (type ? ` is-${type}` : '');
}

function setProcessingStatus(text) {
    el.processingStatus.textContent = text;
}

function setConnectionBadge(stateType) {
    const badge = el.connectionBadge;
    badge.className = 'connection-badge ' + (stateType || '');
    const labelEl = badge.querySelector('.status-text');
    switch (stateType) {
        case 'connected':
            labelEl.textContent = '已连接';
            break;
        case 'ready':
            labelEl.textContent = '就绪';
            break;
        case 'error':
            labelEl.textContent = '错误';
            break;
        default:
            labelEl.textContent = '未连接';
            break;
    }
}

function setChatComposerEnabled(enabled, placeholder = DEFAULT_CHAT_PLACEHOLDER) {
    el.chatInput.disabled = !enabled;
    el.sendBtn.disabled = !enabled;
    el.chatInput.placeholder = placeholder;
}

function getAsrPromptValue() {
    return state.asrPromptDraft.snapshotForTranscription();
}

function syncAsrPromptTrigger() {
    const { hasPrompt, statusText, previewText } = state.asrPromptDraft.getStatus();
    if (el.asrPromptTriggerStatus) {
        el.asrPromptTriggerStatus.textContent = statusText;
    }
    if (el.asrPromptTriggerPreview) {
        el.asrPromptTriggerPreview.textContent = previewText;
    }
    el.asrPromptTrigger?.classList.toggle('is-configured', hasPrompt);
    if (el.asrPromptClearBtn) {
        el.asrPromptClearBtn.disabled = !hasPrompt;
    }
    if (el.asrPromptExamples) {
        const currentValue = state.asrPromptDraft.getValue();
        for (const button of el.asrPromptExamples.querySelectorAll('.asr-prompt-example')) {
            button.classList.toggle('is-active', button.dataset.prompt === currentValue);
        }
    }
}

function syncAsrPromptDialogValue() {
    if (el.asrPromptInput && el.asrPromptInput.value !== state.asrPromptDraft.getValue()) {
        el.asrPromptInput.value = state.asrPromptDraft.getValue();
    }
    syncAsrPromptTrigger();
}

function renderAsrPromptExamples() {
    if (!el.asrPromptExamples) return;
    el.asrPromptExamples.innerHTML = '';
    for (const example of ASR_PROMPT_EXAMPLES) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'asr-prompt-example';
        button.dataset.prompt = example.prompt;

        const label = document.createElement('span');
        label.className = 'asr-prompt-example-label';
        label.textContent = example.label;

        const preview = document.createElement('span');
        preview.className = 'asr-prompt-example-text';
        preview.textContent = example.prompt;

        button.append(label, preview);
        el.asrPromptExamples.append(button);
    }
    syncAsrPromptTrigger();
}

function openAsrPromptDialog() {
    if (!el.asrPromptDialog || state.asrPromptDialogOpen) return;
    state.asrPromptDialogOpen = true;
    el.asrPromptDialog.classList.add('is-open');
    el.asrPromptDialog.setAttribute('aria-hidden', 'false');
    el.asrPromptTrigger?.setAttribute('aria-expanded', 'true');
    syncAsrPromptDialogValue();
    queueMicrotask(() => {
        el.asrPromptInput?.focus();
        const length = el.asrPromptInput?.value.length ?? 0;
        el.asrPromptInput?.setSelectionRange(length, length);
    });
}

function closeAsrPromptDialog({ restoreFocus = true } = {}) {
    if (!el.asrPromptDialog || !state.asrPromptDialogOpen) return;
    state.asrPromptDialogOpen = false;
    el.asrPromptDialog.classList.remove('is-open');
    el.asrPromptDialog.setAttribute('aria-hidden', 'true');
    el.asrPromptTrigger?.setAttribute('aria-expanded', 'false');
    if (restoreFocus) {
        el.asrPromptTrigger?.focus();
    }
}

function handleAsrPromptInput() {
    state.asrPromptDraft.setValue(el.asrPromptInput?.value || '');
    syncAsrPromptTrigger();
}

function handleAsrPromptExampleClick(event) {
    const target = event.target instanceof Element ? event.target : null;
    const button = target?.closest('.asr-prompt-example[data-prompt]');
    if (!button) return;
    state.asrPromptDraft.applyExample(button.dataset.prompt || '');
    syncAsrPromptDialogValue();
    el.asrPromptInput?.focus();
}

function clearAsrPromptDraft() {
    state.asrPromptDraft.clear();
    syncAsrPromptDialogValue();
    el.asrPromptInput?.focus();
}

function toggleLogPanel() {
    state.logPanelOpen = !state.logPanelOpen;
    el.logPanel.classList.toggle('open', state.logPanelOpen);
}

function openLogPanel() {
    if (state.logPanelOpen) return;
    state.logPanelOpen = true;
    el.logPanel.classList.add('open');
}

function openHistoryPanel() {
    state.historyPanelOpen = true;
    el.historyDrawer.classList.add('is-open');
    el.historyDrawer.setAttribute('aria-hidden', 'false');
}

function closeHistoryPanel() {
    state.historyPanelOpen = false;
    el.historyDrawer.classList.remove('is-open');
    el.historyDrawer.setAttribute('aria-hidden', 'true');
}

async function toggleHistoryPanel() {
    if (state.historyPanelOpen) {
        closeHistoryPanel();
        return;
    }
    openHistoryPanel();
    await loadMeetingHistory({ preserveSelection: true, announce: true });
}

// ─── Mode Toggle ───────────────────────────────────────────────────────────────

function setMode(mode) {
    state.currentMode = mode;

    // Update button states
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
    resetUI();
}

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

function handleFileSelect(event) {
    const file = event.target.files[0];
    if (file) {
        state.selectedFile = file;
        el.fileName.textContent = file.name;
        el.fileUploadLabel.classList.add('has-file');
    } else {
        state.selectedFile = null;
        el.fileName.textContent = '选择音频文件';
        el.fileUploadLabel.classList.remove('has-file');
    }
}

function waitForJsonMessage(predicate, timeoutMs = MESSAGE_TIMEOUT_MS, timeoutMessage = '请求超时') {
    return new Promise((resolve, reject) => {
        if (!state.ws) return reject(new Error('WebSocket 未连接'));

        const originalHandler = state.ws.onmessage;
        const timeoutId = setTimeout(() => {
            state.ws.onmessage = originalHandler;
            reject(new Error(timeoutMessage));
        }, timeoutMs);

        state.ws.onmessage = (event) => {
            if (typeof event.data !== 'string') {
                originalHandler(event);
                return;
            }
            try {
                const msg = JSON.parse(event.data);
                if (predicate(msg)) {
                    clearTimeout(timeoutId);
                    state.ws.onmessage = originalHandler;
                    resolve(msg);
                    return;
                }
                if (msg.type === 'error') {
                    clearTimeout(timeoutId);
                    state.ws.onmessage = originalHandler;
                    reject(new Error(msg.message || '请求失败'));
                    return;
                }
            } catch (_) {}

            originalHandler(event);
        };
    });
}

async function fetchJson(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
    }

    const response = await fetch(url, {
        ...options,
        headers,
    });

    const text = await response.text();
    let payload = null;
    if (text) {
        try {
            payload = JSON.parse(text);
        } catch (_) {
            payload = { detail: text };
        }
    }

    if (!response.ok) {
        throw new Error(payload?.detail || payload?.message || `请求失败 (${response.status})`);
    }

    return payload;
}

function triggerDownload(url) {
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = '';
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
}

function buildMeetingExportUrl(sessionId, kind) {
    const encoded = encodeURIComponent(sessionId);
    switch (kind) {
        case 'txt':
            return `/api/meetings/${encoded}/export.txt`;
        case 'md':
            return `/api/meetings/${encoded}/export.md`;
        case 'json':
            return `/api/meetings/${encoded}/export.json`;
        case 'recording':
            return `/api/meetings/${encoded}/recording`;
        default:
            return '';
    }
}

function setTranscriptEmptyState(mode = 'live') {
    state.finalTranscriptStore.reset();
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
    latestSegmentEl = null;
    updateTranscriptCount();
}

function setChatEmptyState(mode = 'live') {
    const copy = mode === 'history'
        ? {
            title: '这场会议还没有问答记录',
            hint: '这里只会展示会议进行中产生的 AI 问答历史。',
        }
        : {
            title: '开始会议后，可以随时向 AI 提问',
            hint: 'AI 会基于当前会议内容实时回答',
        };

    el.chatList.innerHTML = `
        <div class="chat-empty">
            <p>${escapeHtml(copy.title)}</p>
            <p class="hint">${escapeHtml(copy.hint)}</p>
        </div>
    `;
    state.chatMessageNodes.clear();
    removeThinkingIndicator();
    updateMessageCount();
}

function setSummaryEmptyState(mode = 'live') {
    const text = mode === 'history'
        ? '这场会议还没有摘要，可在历史面板里点击「重算摘要」。'
        : '会议进行中时，AI 将自动更新摘要';
    el.summaryText.innerHTML = `<p class="empty-hint">${escapeHtml(text)}</p>`;
}

function hideMeetingBanner() {
    el.meetingBanner.hidden = true;
    el.meetingBannerLabel.textContent = '历史会议';
    el.meetingBannerMeta.textContent = '';
}

function showMeetingBanner(meeting) {
    const status = getMeetingStatusLabel(meeting.status);
    const createdAt = formatMeetingTime(meeting.created_at);
    const recordingLabel = meeting.recording_path
        ? `录音 ${formatBytes(meeting.recording_bytes)}`
        : (meeting.recording_error ? '录音异常' : '无录音');
    el.meetingBannerLabel.textContent = '历史会议';
    el.meetingBannerMeta.textContent = `${createdAt} · ${status} · ${meeting.transcript_count || 0} 段转录 · ${meeting.chat_count || 0} 条问答 · ${recordingLabel}`;
    el.meetingBanner.hidden = false;
}

function updateHistorySelectionMeta() {
    if (state.selectedMeeting) {
        const meeting = state.selectedMeeting;
        el.historySelectionMeta.textContent = `${formatMeetingTime(meeting.created_at)} · ${meeting.transcript_count || 0} 段转录`;
        return;
    }
    el.historySelectionMeta.textContent = state.meetingHistory.length
        ? '选择一场会议后可导出、重算摘要或编辑转录'
        : '未选择会议';
}

function updateHistoryActionButtons() {
    const hasSelection = Boolean(state.selectedMeeting);
    el.downloadTxtBtn.disabled = !hasSelection;
    el.downloadMdBtn.disabled = !hasSelection;
    el.downloadJsonBtn.disabled = !hasSelection;
    el.regenerateStoredSummaryBtn.disabled = !hasSelection;
    el.deleteMeetingBtn.disabled = !hasSelection;
    el.downloadRecordingBtn.disabled = !hasSelection || !state.selectedMeeting?.recording_path;
}

function renderMeetingHistoryList() {
    el.historyList.innerHTML = '';

    if (!state.meetingHistory.length) {
        el.historyList.hidden = true;
        el.historyEmpty.hidden = false;
        updateHistorySelectionMeta();
        updateHistoryActionButtons();
        return;
    }

    el.historyList.hidden = false;
    el.historyEmpty.hidden = true;

    for (const meeting of state.meetingHistory) {
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'history-card' + (meeting.session_id === state.selectedMeetingId ? ' is-active' : '');
        card.dataset.sessionId = meeting.session_id;

        const chips = [
            `${meeting.transcript_count || 0} 段转录`,
            `${meeting.chat_count || 0} 条问答`,
            meeting.recording_path ? `录音 ${formatBytes(meeting.recording_bytes)}` : '无录音',
        ];

        const preview = meeting.summary_preview?.trim() || '暂无摘要，可在底部直接重算。';

        card.innerHTML = `
            <div class="history-card-head">
                <span class="history-card-time">${escapeHtml(formatMeetingTime(meeting.created_at))}</span>
                <span class="history-card-status ${escapeHtml(getMeetingStatusClass(meeting.status))}">
                    ${escapeHtml(getMeetingStatusLabel(meeting.status))}
                </span>
            </div>
            <div class="history-card-meta">
                ${chips.map((chip) => `<span class="history-card-chip">${escapeHtml(chip)}</span>`).join('')}
            </div>
            <div class="history-card-preview">${escapeHtml(preview)}</div>
        `;

        el.historyList.appendChild(card);
    }

    updateHistorySelectionMeta();
    updateHistoryActionButtons();
}

function buildTranscriptBlockMarkup(segment, options = {}) {
    const segmentId = String(segment.id || '');
    const speaker = String(segment.speaker || '发言人');
    const timeLabel = formatSegmentRange(segment);
    const editable = Boolean(options.editable);
    const isEditing = editable && state.currentEditingSegmentId === segmentId;

    if (isEditing) {
        return `
            <div class="segment-group">
                <div class="transcript-block is-editing" data-segment-id="${escapeHtml(segmentId)}">
                    <div class="segment-meta">
                        <div class="segment-editor-meta">
                            <input class="segment-editor-speaker" type="text" value="${escapeHtml(speaker)}" maxlength="40" aria-label="编辑说话人">
                            <span class="segment-editor-time">${escapeHtml(timeLabel)}</span>
                        </div>
                    </div>
                    <textarea class="segment-editor-text" aria-label="编辑转录内容">${escapeHtml(String(segment.text || ''))}</textarea>
                    <div class="segment-editor-actions">
                        <button class="segment-action" type="button" data-action="cancel" data-segment-id="${escapeHtml(segmentId)}">取消</button>
                        <button class="segment-action" type="button" data-action="save" data-segment-id="${escapeHtml(segmentId)}">保存</button>
                    </div>
                </div>
            </div>
        `;
    }

    const actionMarkup = editable
        ? `
            <div class="segment-actions">
                <button class="segment-action" type="button" data-action="edit" data-segment-id="${escapeHtml(segmentId)}">编辑</button>
            </div>
        `
        : '';

    return `
        <div class="segment-group">
            <div class="transcript-block${options.latest ? ' latest' : ''}" data-segment-id="${escapeHtml(segmentId)}">
                <div class="segment-meta">
                    <span class="segment-speaker">${escapeHtml(speaker)}</span>
                    ${actionMarkup || `<span class="segment-time">${escapeHtml(timeLabel)}</span>`}
                </div>
                ${actionMarkup ? `<div class="segment-time">${escapeHtml(timeLabel)}</div>` : ''}
                <div class="segment-text">${escapeHtml(String(segment.text || ''))}</div>
            </div>
        </div>
    `;
}

function renderTranscriptList(segments, options = {}) {
    latestSegmentEl = null;
    state.finalTranscriptStore.reset();

    if (!segments.length) {
        setTranscriptEmptyState(options.emptyMode || 'history');
        return;
    }

    el.transcriptList.innerHTML = segments.map((segment) => buildTranscriptBlockMarkup(segment, options)).join('');
    updateTranscriptCount();
}

function removeThinkingIndicator() {
    if (state.thinkingIndicator) {
        state.thinkingIndicator.remove();
        state.thinkingIndicator = null;
    }
    const existing = el.chatList.querySelector('#__thinking__');
    if (existing) existing.remove();
}

function createChatMessage(role, initialContent, messageId = null) {
    const empty = el.chatList.querySelector('.chat-empty');
    if (empty) empty.remove();

    const div = document.createElement('div');
    div.className = `chat-message ${role}`;
    if (messageId) div.dataset.messageId = messageId;

    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';

    const sender = document.createElement('div');
    sender.className = 'msg-sender';
    sender.textContent = role === 'user' ? '你' : 'AI';

    const content = document.createElement('div');
    content.className = 'msg-text';
    content.textContent = initialContent;

    bubble.appendChild(sender);
    bubble.appendChild(content);
    div.appendChild(bubble);
    el.chatList.appendChild(div);
    el.chatList.scrollTop = el.chatList.scrollHeight;

    const entry = { container: div, contentEl: content };
    if (messageId) state.chatMessageNodes.set(messageId, entry);
    updateMessageCount();
    return entry;
}

function ensureChatMessage(role, messageId, initialContent) {
    if (messageId && state.chatMessageNodes.has(messageId)) {
        return state.chatMessageNodes.get(messageId);
    }
    return createChatMessage(role, initialContent, messageId);
}

function addChatMessage(role, content, messageId = null) {
    const entry = ensureChatMessage(role, messageId, content);
    entry.contentEl.textContent = content;
    entry.contentEl.classList.remove('is-streaming');
    updateMessageCount();
}

function appendChatDelta(messageId, delta) {
    if (!messageId) return;
    const entry = ensureChatMessage('assistant', messageId, '');
    entry.contentEl.textContent += delta;
    entry.contentEl.classList.add('is-streaming');
    el.chatList.scrollTop = el.chatList.scrollHeight;
}

function finalizeChatMessage(messageId, content) {
    if (!messageId) {
        addChatMessage('assistant', content);
        return;
    }
    const entry = ensureChatMessage('assistant', messageId, content);
    entry.contentEl.textContent = content;
    entry.contentEl.classList.remove('is-streaming');
    updateMessageCount();
    el.chatList.scrollTop = el.chatList.scrollHeight;
}

function discardEmptyChatMessage(messageId) {
    if (!messageId) return;
    const entry = state.chatMessageNodes.get(messageId);
    if (!entry || entry.contentEl.textContent.trim()) return;
    entry.container.remove();
    state.chatMessageNodes.delete(messageId);
    updateMessageCount();
}

function showThinkingIndicator() {
    removeThinkingIndicator();
    const wrapper = document.createElement('div');
    wrapper.className = 'chat-message assistant';
    wrapper.id = '__thinking__';
    wrapper.innerHTML = `
        <div class="chat-bubble">
            <div class="msg-sender">AI</div>
            <div class="thinking-bubble">
                <span class="dot"></span><span class="dot"></span><span class="dot"></span>
            </div>
        </div>
    `;
    el.chatList.appendChild(wrapper);
    el.chatList.scrollTop = el.chatList.scrollHeight;
    state.thinkingIndicator = wrapper;
}

function renderChatHistory(messages, mode = 'history') {
    state.chatMessageNodes.clear();
    removeThinkingIndicator();
    el.chatList.innerHTML = '';

    if (!messages.length) {
        setChatEmptyState(mode);
        return;
    }

    for (const message of messages) {
        addChatMessage(message.role, message.content || '', message.id || null);
    }
}

function updateSummary(text) {
    if (!text || !text.trim()) {
        setSummaryEmptyState(state.selectedMeeting ? 'history' : 'live');
        return;
    }
    el.summaryText.innerHTML = formatSummary(text);
}

function clearHistorySelection() {
    state.selectedMeetingId = null;
    state.selectedMeeting = null;
    state.currentEditingSegmentId = null;
    hideMeetingBanner();
    renderMeetingHistoryList();
}

function renderPersistedMeeting(meeting) {
    state.selectedMeeting = meeting;
    state.selectedMeetingId = meeting.session_id;
    state.currentEditingSegmentId = null;

    showMeetingBanner(meeting);
    renderTranscriptList(meeting.transcript || [], { editable: true, emptyMode: 'history' });
    renderChatHistory(meeting.chat_history || [], 'history');
    updateSummary(meeting.summary || '');
    el.summaryBadge.style.display = 'none';
    el.refreshSummaryBtn.disabled = true;
    setChatComposerEnabled(false, HISTORY_CHAT_PLACEHOLDER);

    el.startBtn.disabled = false;
    el.startBtn.style.display = '';
    el.stopBtn.disabled = true;
    el.stopBtn.style.display = 'none';
    el.languageSelect.disabled = false;

    setStatus('浏览历史', 'warn');
    setMicStatus('只读', 'warn');
    setProcessingStatus('已存档');
    setConnectionBadge('');

    renderMeetingHistoryList();
}

function restoreIdleWorkspace() {
    if (state.isMeetingActive) {
        appendLog('会议进行中时无法切换到空白历史视图', 'warning');
        return;
    }
    clearHistorySelection();
    resetUI();
    setStatus('待机');
    setMicStatus('待机');
    setProcessingStatus('—');
    setConnectionBadge('');
}

async function loadMeetingHistory(options = {}) {
    const {
        preserveSelection = true,
        selectSessionId = null,
        announce = false,
    } = options;

    try {
        const payload = await fetchJson('/api/meetings');
        state.meetingHistory = Array.isArray(payload?.meetings) ? payload.meetings : [];
        renderMeetingHistoryList();

        const desiredSessionId = selectSessionId || (preserveSelection ? state.selectedMeetingId : null);
        if (desiredSessionId && state.meetingHistory.some((item) => item.session_id === desiredSessionId)) {
            await loadMeetingDetail(desiredSessionId, { closeDrawer: false, announce: false });
        } else if (desiredSessionId && state.selectedMeetingId === desiredSessionId) {
            restoreIdleWorkspace();
        }

        if (announce) appendLog('会议历史已刷新', 'info');
    } catch (err) {
        appendLog(`加载会议历史失败: ${err.message}`, 'error');
        openLogPanel();
    }
}

async function loadMeetingDetail(sessionId, options = {}) {
    const {
        closeDrawer: shouldCloseDrawer = true,
        announce = true,
    } = options;

    if (state.isMeetingActive) {
        appendLog('请先结束当前会议，再查看历史归档', 'warning');
        return;
    }

    try {
        const meeting = await fetchJson(`/api/meetings/${encodeURIComponent(sessionId)}`);
        renderPersistedMeeting(meeting);
        upsertMeetingHistorySummary(meeting);
        if (shouldCloseDrawer) closeHistoryPanel();
        if (announce) appendLog(`已载入历史会议 ${sessionId.slice(0, 8)}`, 'success');
    } catch (err) {
        appendLog(`加载会议详情失败: ${err.message}`, 'error');
        openLogPanel();
    }
}

async function regenerateSelectedMeetingSummary() {
    if (!state.selectedMeetingId) return;
    try {
        appendLog('正在重算历史会议摘要...', 'info');
        const meeting = await fetchJson(`/api/meetings/${encodeURIComponent(state.selectedMeetingId)}/summary`, {
            method: 'POST',
        });
        renderPersistedMeeting(meeting);
        upsertMeetingHistorySummary(meeting);
        appendLog('历史会议摘要已更新', 'success');
    } catch (err) {
        appendLog(`摘要重算失败: ${err.message}`, 'error');
        openLogPanel();
    }
}

async function deleteSelectedMeeting() {
    if (!state.selectedMeetingId) return;
    const sessionId = state.selectedMeetingId;
    if (!window.confirm('确认删除这场会议及其录音文件？此操作不可撤销。')) {
        return;
    }

    try {
        await fetchJson(`/api/meetings/${encodeURIComponent(sessionId)}`, {
            method: 'DELETE',
        });
        clearHistorySelection();
        removeMeetingHistorySummary(sessionId);
        resetUI();
        setStatus('待机');
        setMicStatus('待机');
        setProcessingStatus('—');
        appendLog(`会议 ${sessionId.slice(0, 8)} 已删除`, 'success');
    } catch (err) {
        appendLog(`删除会议失败: ${err.message}`, 'error');
        openLogPanel();
    }
}

function downloadSelectedMeetingAsset(kind) {
    if (!state.selectedMeetingId) return;
    if (kind === 'recording' && !state.selectedMeeting?.recording_path) {
        appendLog('这场会议没有可下载的录音文件', 'warning');
        return;
    }
    triggerDownload(buildMeetingExportUrl(state.selectedMeetingId, kind));
}

function startSegmentEdit(segmentId) {
    if (!state.selectedMeeting) return;
    state.currentEditingSegmentId = segmentId;
    renderTranscriptList(state.selectedMeeting.transcript || [], { editable: true, emptyMode: 'history' });
}

function cancelSegmentEdit() {
    if (!state.selectedMeeting) return;
    state.currentEditingSegmentId = null;
    renderTranscriptList(state.selectedMeeting.transcript || [], { editable: true, emptyMode: 'history' });
}

async function saveSegmentEdit(segmentId) {
    if (!state.selectedMeetingId || !state.selectedMeeting) return;

    const block = el.transcriptList.querySelector(`.transcript-block[data-segment-id="${segmentId}"]`);
    const speakerInput = block?.querySelector('.segment-editor-speaker');
    const textArea = block?.querySelector('.segment-editor-text');

    const speaker = speakerInput?.value.trim() || '发言人';
    const text = textArea?.value.trim() || '';
    if (!text) {
        appendLog('转录内容不能为空', 'warning');
        return;
    }

    try {
        const payload = await fetchJson(
            `/api/meetings/${encodeURIComponent(state.selectedMeetingId)}/transcript/${encodeURIComponent(segmentId)}`,
            {
                method: 'PATCH',
                body: JSON.stringify({ speaker, text }),
            },
        );
        state.currentEditingSegmentId = null;
        renderPersistedMeeting(payload.meeting);
        upsertMeetingHistorySummary(payload.meeting);
        appendLog('转录片段已保存', 'success');
    } catch (err) {
        appendLog(`保存转录失败: ${err.message}`, 'error');
        openLogPanel();
    }
}

function handleTranscriptListClick(event) {
    const actionButton = event.target.closest('[data-action][data-segment-id]');
    if (!actionButton || !state.selectedMeetingId || state.isMeetingActive) return;

    const { action, segmentId } = actionButton.dataset;
    if (!segmentId) return;

    if (action === 'edit') {
        startSegmentEdit(segmentId);
        return;
    }

    if (action === 'cancel') {
        cancelSegmentEdit();
        return;
    }

    if (action === 'save') {
        saveSegmentEdit(segmentId).catch((err) => {
            appendLog(`保存转录失败: ${err.message}`, 'error');
            openLogPanel();
        });
    }
}

function handleStoppedMessage(msg) {
    if (msg.recording_path) {
        appendLog(`录音已保存: ${msg.recording_path}`, 'success');
    } else if (msg.recording_error) {
        appendLog(`录音保存失败: ${msg.recording_error}`, 'warning');
    } else if (Number.isFinite(msg.recording_bytes) && msg.recording_bytes === 0) {
        appendLog('本次会议未捕获到音频，未生成录音文件', 'warning');
    }

    state.lastCompletedSessionId = state.sessionId;
    setStatus('会议结束', 'warn');
    appendLog('会话已结束', 'info');
}

function handleClosedMessage() {
    setStatus('会议结束', 'warn');
    appendLog('会话已结束', 'info');
}

function buildAudioPacket(frames) {
    if (!frames.length) return null;

    let totalBytes = 6;
    for (const frame of frames) {
        totalBytes += 6 + frame.buffer.byteLength;
    }

    const packet = new ArrayBuffer(totalBytes);
    const header = new Uint8Array(packet, 0, 4);
    header.set([0x4d, 0x52, 0x56, 0x31]);

    const view = new DataView(packet);
    view.setUint16(4, frames.length, true);

    let offset = 6;
    for (const frame of frames) {
        view.setUint32(offset, frame.seq >>> 0, true);
        offset += 4;
        view.setUint16(offset, frame.sampleCount, true);
        offset += 2;
        new Uint8Array(packet, offset, frame.buffer.byteLength).set(new Uint8Array(frame.buffer));
        offset += frame.buffer.byteLength;
    }

    return packet;
}

function flushPendingAudio() {
    if (!state.pendingAudioFrames.length) return;

    const frames = state.pendingAudioFrames;
    state.pendingAudioFrames = [];

    if (state.ws?.readyState === WebSocket.OPEN) {
        const packet = buildAudioPacket(frames);
        if (packet) state.ws.send(packet);
    }
}

function queueAudioFrame(frame) {
    if (!frame?.buffer || !frame.sampleCount) return;

    state.pendingAudioFrames.push(frame);
    state.lastCapturedSeq = Math.max(state.lastCapturedSeq, frame.seq ?? -1);

    if (state.pendingAudioFrames.length >= AUDIO_BATCH_FRAMES) {
        flushPendingAudio();
    }
}

function resolveDrain(lastSeq) {
    const resolver = state.drainResolver;
    state.drainResolver = null;
    state.drainRejector = null;
    if (resolver) resolver(lastSeq);
}

function rejectDrain(error) {
    const rejector = state.drainRejector;
    state.drainResolver = null;
    state.drainRejector = null;
    if (rejector) rejector(error);
}

function requestWorkletDrain() {
    if (!state.processorNode) {
        return Promise.resolve(state.lastCapturedSeq);
    }

    return new Promise((resolve, reject) => {
        const timeoutId = setTimeout(() => {
            rejectDrain(new Error('音频 drain 超时'));
        }, 1500);

        state.drainResolver = (lastSeq) => {
            clearTimeout(timeoutId);
            resolve(lastSeq);
        };
        state.drainRejector = (error) => {
            clearTimeout(timeoutId);
            reject(error);
        };

        state.processorNode.port.postMessage({ type: 'drain' });
    });
}

function handleWorkletMessage(event) {
    const msg = event.data;
    if (!msg || typeof msg !== 'object') return;

    if (msg.type === 'audio_frame') {
        queueAudioFrame({
            seq: Number.isInteger(msg.seq) ? msg.seq : -1,
            sampleCount: Number.isInteger(msg.sampleCount) ? msg.sampleCount : 0,
            buffer: msg.pcm,
        });
        return;
    }

    if (msg.type === 'drained') {
        const lastSeq = Number.isInteger(msg.lastSeq) ? msg.lastSeq : state.lastCapturedSeq;
        state.lastCapturedSeq = Math.max(state.lastCapturedSeq, lastSeq);
        flushPendingAudio();
        resolveDrain(lastSeq);
    }
}

async function startRecording() {
    try {
        const audioSupport = describeRealtimeAudioSupport();
        if (!audioSupport.supported) {
            throw new Error(audioSupport.message);
        }

        state.stream = await requestRealtimeAudioStream({
            audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
            video: false,
        });

        state.audioContext = new audioSupport.audioContextCtor();
        await state.audioContext.audioWorklet.addModule('/static/audio-worklet.js');
        await state.audioContext.resume();

        state.sourceNode = state.audioContext.createMediaStreamSource(state.stream);
        state.processorNode = new AudioWorkletNode(state.audioContext, 'audio-capture-processor');
        state.processorNode.port.onmessage = handleWorkletMessage;
        state.processorNode.port.postMessage({
            type: 'configure',
            targetSampleRate: TARGET_SAMPLE_RATE,
            frameSamples: AUDIO_FRAME_SAMPLES,
        });

        state.sourceNode.connect(state.processorNode);
        state.processorNode.connect(state.audioContext.destination);

        state.isRecording = true;
        state.pendingAudioFrames = [];
        state.lastCapturedSeq = -1;
        state.startTime = Date.now();

        el.recordingTimer.classList.add('active');
        el.timerDisplay.textContent = '00:00';

        state.recordingTimerId = setInterval(() => {
            if (state.isRecording) {
                const elapsed = (Date.now() - state.startTime) / 1000;
                el.timerDisplay.textContent = formatTime(elapsed);
            }
        }, 1000);

        setMicStatus('录音中', 'active');
        appendLog('录音已开始', 'success');
    } catch (err) {
        appendLog(`录音启动失败: ${err.message}`, 'error');
        throw err;
    }
}

function teardownRecordingResources() {
    if (state.processorNode) {
        state.processorNode.port.onmessage = null;
        state.processorNode.disconnect();
    }
    if (state.sourceNode) state.sourceNode.disconnect();
    if (state.stream) state.stream.getTracks().forEach((track) => track.stop());
    if (state.audioContext) state.audioContext.close().catch(() => {});

    state.processorNode = null;
    state.sourceNode = null;
    state.stream = null;
    state.audioContext = null;
    rejectDrain(new Error('录音链路已关闭'));
}

async function stopRecording() {
    const hadActiveRecording = Boolean(
        state.isRecording || state.processorNode || state.sourceNode || state.stream || state.audioContext,
    );

    state.isRecording = false;
    clearInterval(state.recordingTimerId);
    state.recordingTimerId = null;
    let lastSeq = state.lastCapturedSeq;

    try {
        lastSeq = await requestWorkletDrain();
    } catch (err) {
        appendLog(`音频 drain 失败: ${err.message}`, 'warning');
        flushPendingAudio();
    }

    teardownRecordingResources();

    el.recordingTimer.classList.remove('active');
    el.timerDisplay.textContent = '00:00';

    if (hadActiveRecording) {
        setMicStatus('已停止', 'warn');
        appendLog('录音已停止', 'info');
    }

    return lastSeq;
}

async function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    state.ws = new WebSocket(`${protocol}://${location.host}/ws/meeting`);
    state.ws.binaryType = 'arraybuffer';

    const openP = new Promise((resolve, reject) => {
        state.ws.onopen = () => {
            appendLog('WebSocket 已连接');
            resolve();
        };
        state.ws.onerror = () => {
            appendLog('WebSocket 错误', 'error');
            reject(new Error('WebSocket 连接失败'));
        };
        state.ws.onclose = () => {
            appendLog('WebSocket 已断开', 'warning');
            setConnectionBadge('');
            cleanup();
        };
    });

    const readyP = new Promise((resolve) => {
        const handler = (event) => {
            if (typeof event.data !== 'string') return;
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'ready') {
                    state.ws.onmessage = handleServerMessage;
                    resolve(msg);
                }
            } catch (_) {}
        };
        state.ws.onmessage = handler;
    });

    await openP;
    const init = await readyP;
    if (init.asr_language !== undefined) {
        el.languageSelect.value = init.asr_language;
    }
    return init;
}

function handleServerMessage(event) {
    if (typeof event.data !== 'string') return;
    try {
        handleJsonMessage(JSON.parse(event.data));
    } catch (err) {
        console.error('JSON parse error:', err);
    }
}

function appendFinalTranscriptSegment(segment) {
    if (!state.finalTranscriptStore.accept(segment)) {
        return;
    }

    const empty = el.transcriptList.querySelector('.empty-state');
    if (empty) empty.remove();

    if (latestSegmentEl) {
        latestSegmentEl.classList.remove('latest');
    }

    const group = document.createElement('div');
    group.className = 'segment-group';
    group.innerHTML = buildTranscriptBlockMarkup(
        {
            id: segment.id,
            speaker: segment.speaker,
            text: segment.text,
            start: segment.start ?? segment.start_time,
            end: segment.end ?? segment.end_time,
        },
        {
            latest: true,
            editable: false,
        },
    );

    const nextNode = group.firstElementChild;
    el.transcriptList.appendChild(nextNode);
    latestSegmentEl = nextNode.querySelector('.transcript-block');
    el.transcriptList.scrollTop = el.transcriptList.scrollHeight;
    updateTranscriptCount();
}

function handleJsonMessage(msg) {
    switch (msg.type) {
        case 'ready':
            state.sessionId = msg.session_id;
            state.isConnected = true;
            setConnectionBadge('connected');
            setStatus('已连接');
            appendLog(`会话已创建: ${msg.session_id.slice(0, 8)}`);
            appendLog(`ASR: ${msg.asr_device} · LLM: ${msg.llm_model}`, 'info');
            break;

        case 'start_ack':
            setStatus('会议进行中', 'active');
            setConnectionBadge('ready');
            setProcessingStatus('监听中');
            appendLog('语音模型已就绪，会议开始', 'success');
            break;

        case 'status':
            setStatus(msg.message || '处理中', 'warn');
            break;

        case 'transcript':
            appendFinalTranscriptSegment(msg.segment);
            if (msg.processing_time) {
                setProcessingStatus(`${msg.processing_time.toFixed(2)}s`);
            }
            break;

        case 'transcribe_done':
            setProcessingStatus(state.isRecording ? '监听中' : '等待中');
            break;

        case 'chat_status':
            if (msg.status === 'thinking') {
                showThinkingIndicator();
                setChatComposerEnabled(false, DEFAULT_CHAT_PLACEHOLDER);
            }
            break;

        case 'chat_stream_start':
            removeThinkingIndicator();
            ensureChatMessage('assistant', msg.message_id, '');
            break;

        case 'chat_stream_delta':
            appendChatDelta(msg.message_id, msg.delta || '');
            break;

        case 'chat_response':
            finalizeChatMessage(msg.message_id, msg.answer || '');
            setChatComposerEnabled(true, DEFAULT_CHAT_PLACEHOLDER);
            el.chatInput.focus();
            break;

        case 'chat_error':
            removeThinkingIndicator();
            appendLog(`聊天错误: ${msg.message}`, 'error');
            discardEmptyChatMessage(msg.message_id);
            setChatComposerEnabled(true, DEFAULT_CHAT_PLACEHOLDER);
            break;

        case 'summary_status':
            el.summaryText.classList.add('is-updating');
            break;

        case 'summary_update':
            el.summaryText.classList.remove('is-updating');
            updateSummary(msg.summary);
            appendLog('摘要已更新', 'success');
            break;

        case 'error':
            appendLog(`错误: ${msg.message}`, 'error');
            openLogPanel();
            setStatus('启动失败', 'err');
            setMicStatus('未启动', 'err');
            setProcessingStatus('检查日志');
            setConnectionBadge('error');
            break;

        case 'stopped':
            handleStoppedMessage(msg);
            if (state.ws?.readyState === WebSocket.OPEN) state.ws.close();
            break;

        case 'closed':
            handleClosedMessage();
            if (state.ws?.readyState === WebSocket.OPEN) state.ws.close();
            break;

        case 'pong':
            break;

        default:
            console.debug('Unknown message type:', msg.type);
    }
}

function cleanup(options = {}) {
    const {
        preserveStatus = false,
        preserveMicStatus = false,
        preserveProcessingStatus = false,
        preserveConnectionBadge = false,
    } = options;

    state.isRecording = false;
    state.isMeetingActive = false;
    state.isConnected = false;
    state.currentEditingSegmentId = null;
    state.finalTranscriptStore.reset();
    teardownRecordingResources();
    latestSegmentEl = null;

    if (state.ws) {
        state.ws.onopen = null;
        state.ws.onmessage = null;
        state.ws.onerror = null;
        state.ws.onclose = null;
        if (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING) {
            try {
                state.ws.close();
            } catch (_) {}
        }
        state.ws = null;
    }

    state.sessionId = null;

    el.startBtn.disabled = false;
    el.startBtn.style.display = '';
    el.stopBtn.disabled = true;
    el.stopBtn.style.display = 'none';
    setChatComposerEnabled(false, state.selectedMeeting ? HISTORY_CHAT_PLACEHOLDER : DEFAULT_CHAT_PLACEHOLDER);
    el.refreshSummaryBtn.disabled = true;
    el.languageSelect.disabled = false;

    if (!preserveStatus) setStatus(state.selectedMeeting ? '浏览历史' : '待机');
    if (!preserveMicStatus) setMicStatus(state.selectedMeeting ? '只读' : '待机', state.selectedMeeting ? 'warn' : '');
    if (!preserveProcessingStatus) setProcessingStatus(state.selectedMeeting ? '已存档' : '—');
    if (!preserveConnectionBadge) setConnectionBadge('');
}

function resetUI() {
    setTranscriptEmptyState(state.currentMode === 'upload' ? 'upload' : 'live');
    setChatEmptyState('live');
    setSummaryEmptyState('live');
    hideMeetingBanner();
    state.chatMessageNodes.clear();
    state.pendingAudioFrames = [];
    state.lastCapturedSeq = -1;
    state.currentEditingSegmentId = null;
    state.finalTranscriptStore.reset();
    removeThinkingIndicator();
    el.timerDisplay.textContent = '00:00';
    el.recordingTimer.classList.remove('active');
    el.summaryBadge.style.display = 'none';
    setChatComposerEnabled(false, DEFAULT_CHAT_PLACEHOLDER);
    syncStartButton();
}

// ─── File Upload Handler ───────────────────────────────────────────────────────

async function handleFileUpload() {
    const file = state.selectedFile;
    if (!file) {
        appendLog('请先选择音频文件', 'warning');
        setStatus('待机', '');
        el.startBtn.disabled = false;
        return;
    }

    setStatus('上传中...', 'warn');
    appendLog(`正在上传并转录: ${file.name}`, 'info');
    openLogPanel();

    // Show progress bar
    el.uploadProgressContainer.style.display = '';
    el.uploadProgressBar.style.width = '0%';
    el.uploadProgressBar.classList.remove('complete');

    try {
        const formData = new FormData();
        formData.append('file', file);
        appendTranscriptionOptions(formData, {
            language: el.languageSelect.value,
            asrPrompt: getAsrPromptValue(),
        });

        const xhr = new XMLHttpRequest();

        // Progress tracking
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
                const percent = Math.round((e.loaded / e.total) * 100);
                setProcessingStatus(`${percent}%`);
            }
        };

        const result = await new Promise((resolve, reject) => {
            xhr.onload = () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve(JSON.parse(xhr.responseText));
                } else {
                    try {
                        const err = JSON.parse(xhr.responseText);
                        reject(new Error(err.detail || `上传失败 (${xhr.status})`));
                    } catch {
                        reject(new Error(`上传失败 (${xhr.status})`));
                    }
                }
            };
            xhr.onerror = () => reject(new Error('网络错误，请检查服务器连接'));
            xhr.onabort = () => reject(new Error('上传已取消'));

            xhr.open('POST', '/api/upload');
            xhr.send(formData);
        });

        appendLog(`转录完成: ${result.segments?.length || 0} 条记录`, 'success');
        setProcessingStatus(`${result.processing_time?.toFixed(1)}s`);

        // Animate progress bar to complete
        el.uploadProgressBar.classList.add('complete');

        // Display results
        state.sessionId = result.session_id;
        state.isMeetingActive = true;

        // Show meeting banner
        el.meetingBanner.hidden = false;
        el.meetingBannerLabel.textContent = '上传录音';
        el.meetingBannerMeta.textContent = `${file.name} · ${result.audio_duration?.toFixed(1) || 0}s`;

        // Render transcript segments
        if (result.segments && result.segments.length > 0) {
            for (const seg of result.segments) {
                appendFinalTranscriptSegment(seg);
            }
            updateTranscriptCount();
        }

        // Enable chat
        setChatComposerEnabled(true, DEFAULT_CHAT_PLACEHOLDER);
        el.refreshSummaryBtn.disabled = false;
        el.summaryBadge.style.display = '';

        // Show stop button for ending the "meeting"
        el.stopBtn.disabled = false;
        el.stopBtn.style.display = '';
        el.startBtn.style.display = 'none';

        setStatus('转录完成', 'active');
        appendLog('转录完成，可以向 AI 提问', 'success');

        // Fetch meeting detail for summary (in background)
        setTimeout(async () => {
            try {
                await loadMeetingDetail(result.session_id, { closeDrawer: false, announce: false });
            } catch (e) {
                appendLog('获取摘要失败', 'warning');
            }
        }, 1000);

    } catch (err) {
        appendLog(`上传失败: ${err.message}`, 'error');
        openLogPanel();
        setStatus('上传失败', 'err');
        el.startBtn.disabled = false;
        // Hide progress bar on error
        el.uploadProgressContainer.style.display = 'none';
    }
}

async function handleStart() {
    clearHistorySelection();
    state.lastCompletedSessionId = null;

    el.startBtn.disabled = true;
    el.stopBtn.disabled = true;
    resetUI();
    setStatus('处理中...', 'warn');

    // Check mode
    if (state.currentMode === 'upload') {
        // Upload mode
        if (!state.selectedFile) {
            appendLog('请先选择音频文件', 'warning');
            openLogPanel();
            setStatus('待机', '');
            el.startBtn.disabled = false;
            return;
        }
        await handleFileUpload();
        return;
    }

    // Realtime mode (default)
    setStatus('连接中...', 'warn');

    try {
        const init = await connectWebSocket();
        if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
            throw new Error('WebSocket 未连接');
        }

        el.languageSelect.disabled = true;
        state.ws.send(JSON.stringify(buildRealtimeStartPayload({
            language: el.languageSelect.value,
            asrPrompt: getAsrPromptValue(),
        })));
        setStatus('加载语音模型...', 'warn');
        appendLog('正在请求启动语音转录...', 'info');

        const modelInitTimeoutMs = Math.max(
            10000,
            Math.ceil((init?.asr_init_timeout_sec || 180) * 1000) + MODEL_INIT_TIMEOUT_PADDING_MS,
        );
        await waitForJsonMessage(
            (msg) => msg.type === 'start_ack',
            modelInitTimeoutMs,
            '语音模型初始化超时，请检查本地 ASR 模型和 server.log',
        ).catch((err) => {
            appendLog(err.message, 'error');
            setStatus('模型加载失败', 'err');
            throw err;
        });

        await startRecording();

        state.isMeetingActive = true;
        el.stopBtn.disabled = false;
        el.stopBtn.style.display = '';
        el.startBtn.style.display = 'none';
        setChatComposerEnabled(true, DEFAULT_CHAT_PLACEHOLDER);
        el.refreshSummaryBtn.disabled = false;
        el.summaryBadge.style.display = '';
        el.chatInput.focus();

        setStatus('会议进行中', 'active');
        appendLog('会议已启动', 'success');
    } catch (err) {
        if (err.message && !err.message.includes('timeout')) {
            appendLog(`启动失败: ${err.message}`, 'error');
        }
        openLogPanel();
        setStatus('启动失败', 'err');
        setMicStatus('未启动', 'err');
        setProcessingStatus('检查日志');
        setConnectionBadge('error');
        cleanup({
            preserveStatus: true,
            preserveMicStatus: true,
            preserveProcessingStatus: true,
            preserveConnectionBadge: true,
        });
    }
}

async function handleStop() {
    el.stopBtn.disabled = true;
    setChatComposerEnabled(false, DEFAULT_CHAT_PLACEHOLDER);
    el.refreshSummaryBtn.disabled = true;
    setStatus('正在结束...', 'warn');

    // If in upload mode, just clean up the UI
    if (state.currentMode === 'upload') {
        cleanup();
        setStatus('已完成', 'active');
        appendLog('转录会话已结束', 'success');
        return;
    }

    // Realtime mode - handle WebSocket and recording
    const lastSeq = await stopRecording();

    let stopReply = null;

    if (state.ws?.readyState === WebSocket.OPEN) {
        try {
            state.ws.send(JSON.stringify({ type: 'eos', last_seq: lastSeq }));
            stopReply = await waitForJsonMessage(
                (msg) => msg.type === 'stopped' || msg.type === 'closed',
                MESSAGE_TIMEOUT_MS,
                '结束会议超时',
            ).catch((err) => {
                appendLog(err.message, 'warning');
                return null;
            });
        } catch (_) {}

        if (stopReply?.type === 'stopped') {
            handleStoppedMessage(stopReply);
        } else if (stopReply?.type === 'closed') {
            handleClosedMessage();
        }

        if (state.ws?.readyState === WebSocket.OPEN) state.ws.close();
    }

    const completedSessionId = state.lastCompletedSessionId;
    cleanup();

    if (completedSessionId) {
        await loadMeetingHistory({
            preserveSelection: false,
            selectSessionId: completedSessionId,
            announce: false,
        });
    }
}

async function handleSendMessage() {
    const text = el.chatInput.value.trim();
    if (!text || state.selectedMeetingId) return;
    el.chatInput.value = '';

    addChatMessage('user', text);

    if (state.ws?.readyState === WebSocket.OPEN) {
        // Realtime mode: use WebSocket
        state.ws.send(JSON.stringify({ type: 'chat', question: text }));
    } else if (state.sessionId) {
        // Upload mode: use REST API
        try {
            showThinkingIndicator();
            const result = await fetchJson(`/api/meetings/${encodeURIComponent(state.sessionId)}/chat`, {
                method: 'POST',
                body: JSON.stringify({ question: text }),
            });
            removeThinkingIndicator();
            addChatMessage('assistant', result.answer || result.response || '');
        } catch (err) {
            removeThinkingIndicator();
            appendLog(`提问失败: ${err.message}`, 'error');
            openLogPanel();
        }
    }
}

function handleRefreshSummary() {
    if (state.selectedMeetingId) return;
    if (state.ws?.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: 'summary' }));
        appendLog('正在刷新摘要...', 'info');
    }
}

el.startBtn.addEventListener('click', () => {
    handleStart().catch((err) => {
        appendLog(`启动失败: ${err.message}`, 'error');
        openLogPanel();
    });
});

// Mode toggle event listeners
el.modeRealtimeBtn?.addEventListener('click', () => setMode('realtime'));
el.modeUploadBtn?.addEventListener('click', () => setMode('upload'));
el.audioFileInput?.addEventListener('change', handleFileSelect);
el.asrPromptTrigger?.addEventListener('click', openAsrPromptDialog);
el.asrPromptBackdrop?.addEventListener('click', () => closeAsrPromptDialog());
el.asrPromptCloseBtn?.addEventListener('click', () => closeAsrPromptDialog());
el.asrPromptDoneBtn?.addEventListener('click', () => closeAsrPromptDialog());
el.asrPromptClearBtn?.addEventListener('click', clearAsrPromptDraft);
el.asrPromptExamples?.addEventListener('click', handleAsrPromptExampleClick);
el.asrPromptInput?.addEventListener('input', handleAsrPromptInput);
el.stopBtn.addEventListener('click', () => {
    handleStop().catch((err) => {
        appendLog(`结束会议失败: ${err.message}`, 'error');
        openLogPanel();
    });
});
el.sendBtn.addEventListener('click', handleSendMessage);
el.chatInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        handleSendMessage();
    }
});
el.refreshSummaryBtn.addEventListener('click', handleRefreshSummary);
el.toggleLogBtn?.addEventListener('click', toggleLogPanel);
el.historyBtn?.addEventListener('click', () => {
    toggleHistoryPanel().catch((err) => {
        appendLog(`打开历史面板失败: ${err.message}`, 'error');
        openLogPanel();
    });
});
el.historyBackdrop?.addEventListener('click', closeHistoryPanel);
el.historyCloseBtn?.addEventListener('click', closeHistoryPanel);
el.historyRefreshBtn?.addEventListener('click', () => {
    loadMeetingHistory({ preserveSelection: true, announce: true }).catch((err) => {
        appendLog(`刷新历史失败: ${err.message}`, 'error');
        openLogPanel();
    });
});
el.historyResetViewBtn?.addEventListener('click', restoreIdleWorkspace);
el.downloadTxtBtn?.addEventListener('click', () => downloadSelectedMeetingAsset('txt'));
el.downloadMdBtn?.addEventListener('click', () => downloadSelectedMeetingAsset('md'));
el.downloadJsonBtn?.addEventListener('click', () => downloadSelectedMeetingAsset('json'));
el.downloadRecordingBtn?.addEventListener('click', () => downloadSelectedMeetingAsset('recording'));
el.regenerateStoredSummaryBtn?.addEventListener('click', () => {
    regenerateSelectedMeetingSummary().catch((err) => {
        appendLog(`摘要重算失败: ${err.message}`, 'error');
        openLogPanel();
    });
});
el.deleteMeetingBtn?.addEventListener('click', () => {
    deleteSelectedMeeting().catch((err) => {
        appendLog(`删除会议失败: ${err.message}`, 'error');
        openLogPanel();
    });
});
el.historyList?.addEventListener('click', (event) => {
    const card = event.target.closest('.history-card[data-session-id]');
    if (!card) return;
    loadMeetingDetail(card.dataset.sessionId).catch((err) => {
        appendLog(`加载会议详情失败: ${err.message}`, 'error');
        openLogPanel();
    });
});
el.transcriptList?.addEventListener('click', handleTranscriptListClick);
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.asrPromptDialogOpen) {
        closeAsrPromptDialog();
        return;
    }
    if (event.key === 'Escape' && state.historyPanelOpen) {
        closeHistoryPanel();
    }
});

renderAsrPromptExamples();
syncAsrPromptDialogValue();
setStatus('待机');
setMicStatus('待机');
setProcessingStatus('—');
setConnectionBadge('');
resetUI();
appendLog('页面已加载，点击「开始会议」启动', 'info');
loadMeetingHistory({ preserveSelection: true, announce: false }).catch(() => {});
