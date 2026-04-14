import test from 'node:test';
import assert from 'node:assert/strict';

import {
    createLiveTranscriptState,
    findLiveTranscriptGroup,
    upsertLiveTranscriptGroup,
} from '../static/live-transcript-state.mjs';

test('creates a live entry on first preview segment', () => {
    const state = createLiveTranscriptState();
    const result = state.apply({
        id: '',
        segment_id: 7,
        revision: 1,
        is_final: false,
        text: '我们这周先把接口',
        start: 0,
        end: 1,
    });

    assert.equal(result.changed, true);
    assert.equal(state.values().length, 1);
    assert.equal(state.values()[0].segmentId, '7');
    assert.equal(state.values()[0].persisted, false);
});

test('replaces an existing entry only when revision is newer', () => {
    const state = createLiveTranscriptState();
    state.apply({ id: '', segment_id: 7, revision: 2, is_final: false, text: '旧文本', start: 0, end: 1 });
    const stale = state.apply({ id: '', segment_id: 7, revision: 1, is_final: false, text: '更旧文本', start: 0, end: 1 });
    const fresh = state.apply({ id: '', segment_id: 7, revision: 3, is_final: false, text: '新文本', start: 0, end: 2 });

    assert.equal(stale.changed, false);
    assert.equal(fresh.changed, true);
    assert.equal(state.values()[0].text, '新文本');
    assert.equal(state.values()[0].revision, 3);
});

test('promotes a live entry to persisted on final segment', () => {
    const state = createLiveTranscriptState();
    state.apply({ id: '', segment_id: 7, revision: 1, is_final: false, text: '预览', start: 0, end: 1 });
    state.apply({ id: 'seg-db-1', segment_id: 7, revision: 2, is_final: true, text: '定稿。', start: 0, end: 1.5 });

    assert.equal(state.values()[0].persisted, true);
    assert.equal(state.values()[0].id, 'seg-db-1');
    assert.equal(state.values()[0].text, '定稿。');
});

test('still works when backend emits only final segments', () => {
    const state = createLiveTranscriptState();
    const result = state.apply({ id: 'seg-db-2', segment_id: 8, revision: 1, is_final: true, text: '只有定稿。', start: 2, end: 3 });

    assert.equal(result.changed, true);
    assert.equal(state.values()[0].persisted, true);
    assert.equal(state.values()[0].segmentId, '8');
});

test('accepts persisted id-only segments from upload or history flows', () => {
    const state = createLiveTranscriptState();
    const result = state.apply({ id: 'seg-upload-1', text: '上传转录结果', start: 4, end: 6 });

    assert.equal(result.changed, true);
    assert.equal(state.values()[0].segmentId, 'seg-upload-1');
    assert.equal(state.values()[0].persisted, true);
    assert.equal(state.values()[0].text, '上传转录结果');
});

test('findLiveTranscriptGroup resolves the outer wrapper for an existing live segment', () => {
    const groupNode = {
        classList: { contains: (name) => name === 'segment-group' },
    };
    const innerBlock = {
        classList: { contains: () => false },
        closest: (selector) => selector === '.segment-group' ? groupNode : null,
    };
    const listEl = {
        querySelector: (selector) => selector === '[data-live-segment-id="7"]' ? innerBlock : null,
    };

    const resolved = findLiveTranscriptGroup(listEl, 7);
    assert.equal(resolved, groupNode);
});

test('upsertLiveTranscriptGroup replaces the existing outer wrapper instead of nesting', () => {
    const nextNode = { name: 'next-group' };
    const groupNode = {
        classList: { contains: (name) => name === 'segment-group' },
        replaceWith(node) {
            this.replaced = node;
        },
    };
    const listEl = {
        querySelector: () => groupNode,
        appendChild() {
            throw new Error('should not append when an existing group is present');
        },
    };

    const applied = upsertLiveTranscriptGroup(listEl, 9, nextNode);
    assert.equal(applied, nextNode);
    assert.equal(groupNode.replaced, nextNode);
});
