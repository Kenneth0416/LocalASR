import { normalizeAsrPrompt } from './transcription-options.mjs';

export const EMPTY_PROMPT_PREVIEW = '可选：术语、人名、产品名、语言约束';
const PREVIEW_LIMIT = 32;

export const ASR_PROMPT_EXAMPLES = [
    {
        label: '术语优先',
        prompt: '这是一场芯片设计会议。常见术语：SerDes、PLL、retimer、PCIe Gen5、UCIe。保留英文缩写，不要意译。',
    },
    {
        label: '人名与产品名',
        prompt: '这是一场产品评审会议。请优先识别人名、公司名、产品名，并保留原文：Codex、Qwen3-ASR、Meeting Realtime Voice、Mira。',
    },
    {
        label: '中英夹杂',
        prompt: '这是一场中文夹英文会议。请输出中文转写，保留英文术语与缩写原文，不要自动翻译产品名。',
    },
    {
        label: '粤语会议',
        prompt: '这是一场粤语夹普通话会议。请按实际语种转写，保留人名、项目代号和英文产品名原文。',
    },
];

export function buildAsrPromptStatus(value) {
    const prompt = normalizeAsrPrompt(value);
    if (!prompt) {
        return {
            hasPrompt: false,
            statusText: '未设置',
            previewText: EMPTY_PROMPT_PREVIEW,
        };
    }

    const characters = Array.from(prompt);
    const previewText = characters.length > PREVIEW_LIMIT
        ? `${characters.slice(0, PREVIEW_LIMIT).join('')}…`
        : prompt;

    return {
        hasPrompt: true,
        statusText: `已设置 · ${characters.length} 字`,
        previewText,
    };
}

export function createAsrPromptDraft(initialValue = '') {
    let currentValue = normalizeAsrPrompt(initialValue);

    return {
        getValue() {
            return currentValue;
        },
        setValue(value) {
            currentValue = normalizeAsrPrompt(value);
            return buildAsrPromptStatus(currentValue);
        },
        applyExample(value) {
            currentValue = normalizeAsrPrompt(value);
            return buildAsrPromptStatus(currentValue);
        },
        clear() {
            currentValue = '';
            return buildAsrPromptStatus(currentValue);
        },
        snapshotForTranscription() {
            return currentValue;
        },
        getStatus() {
            return buildAsrPromptStatus(currentValue);
        },
    };
}
