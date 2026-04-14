import test from 'node:test';
import assert from 'node:assert/strict';

import {
    appendTranscriptionOptions,
    buildRealtimeStartPayload,
} from '../static/transcription-options.mjs';

test('buildRealtimeStartPayload trims and includes one-shot asr_prompt', () => {
    assert.deepEqual(
        buildRealtimeStartPayload({
            language: 'Chinese',
            asrPrompt: '  术语：Qwen3-ASR，Codex  ',
        }),
        {
            type: 'start',
            language: 'Chinese',
            asr_prompt: '术语：Qwen3-ASR，Codex',
        },
    );
});

test('appendTranscriptionOptions appends language and one-shot asr_prompt', () => {
    const formData = {
        appended: [],
        append(key, value) {
            this.appended.push([key, value]);
        },
    };

    appendTranscriptionOptions(formData, {
        language: 'English',
        asrPrompt: '  person names: Codex, Mira  ',
    });

    assert.deepEqual(formData.appended, [
        ['language', 'English'],
        ['asr_prompt', 'person names: Codex, Mira'],
    ]);
});

test('appendTranscriptionOptions omits empty asr_prompt', () => {
    const formData = {
        appended: [],
        append(key, value) {
            this.appended.push([key, value]);
        },
    };

    appendTranscriptionOptions(formData, {
        language: '',
        asrPrompt: '   ',
    });

    assert.deepEqual(formData.appended, [['language', '']]);
});
