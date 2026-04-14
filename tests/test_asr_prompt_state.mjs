import test from 'node:test';
import assert from 'node:assert/strict';

import {
    ASR_PROMPT_EXAMPLES,
    buildAsrPromptStatus,
    createAsrPromptDraft,
} from '../static/asr-prompt-state.mjs';

test('buildAsrPromptStatus reports unset prompt when empty', () => {
    assert.deepEqual(buildAsrPromptStatus('   '), {
        hasPrompt: false,
        statusText: '未设置',
        previewText: '可选：术语、人名、产品名、语言约束',
    });
});

test('buildAsrPromptStatus reports configured prompt with preview', () => {
    assert.deepEqual(
        buildAsrPromptStatus('  术语：Qwen3-ASR，Codex，Meeting Realtime Voice  '),
        {
            hasPrompt: true,
            statusText: '已设置 · 41 字',
            previewText: '术语：Qwen3-ASR，Codex，Meeting Realt…',
        },
    );
});

test('createAsrPromptDraft preserves value after snapshot for transcription', () => {
    const draft = createAsrPromptDraft();
    draft.setValue('  术语：Qwen3-ASR，Codex  ');

    assert.equal(draft.snapshotForTranscription(), '术语：Qwen3-ASR，Codex');
    assert.equal(draft.getValue(), '术语：Qwen3-ASR，Codex');
});

test('createAsrPromptDraft applies example prompt and clears only on explicit clear', () => {
    const draft = createAsrPromptDraft();
    draft.applyExample(ASR_PROMPT_EXAMPLES[1].prompt);

    assert.equal(draft.getValue(), ASR_PROMPT_EXAMPLES[1].prompt);
    draft.clear();
    assert.equal(draft.getValue(), '');
});
