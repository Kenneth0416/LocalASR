export function normalizeAsrPrompt(value) {
    return String(value ?? '').trim();
}

export function buildRealtimeStartPayload({ language = '', asrPrompt = '' } = {}) {
    const payload = {
        type: 'start',
        language: language || '',
    };
    const prompt = normalizeAsrPrompt(asrPrompt);
    if (prompt) {
        payload.asr_prompt = prompt;
    }
    return payload;
}

export function appendTranscriptionOptions(formData, { language = '', asrPrompt = '' } = {}) {
    formData.append('language', language || '');
    const prompt = normalizeAsrPrompt(asrPrompt);
    if (prompt) {
        formData.append('asr_prompt', prompt);
    }
}
