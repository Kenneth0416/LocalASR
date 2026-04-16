import test from 'node:test';
import assert from 'node:assert/strict';

import { createFinalTranscriptStore } from '../static/final-transcript-store.mjs';

test('accepts the first persisted segment id and rejects duplicates', () => {
    const store = createFinalTranscriptStore();

    assert.equal(store.accept({ id: 'seg-1' }), true);
    assert.equal(store.accept({ id: 'seg-1' }), false);
    assert.equal(store.accept({ id: 'seg-2' }), true);
});

test('ignores empty ids so upload/history callers can guard upstream', () => {
    const store = createFinalTranscriptStore();

    assert.equal(store.accept({ id: '' }), false);
    assert.equal(store.accept({}), false);
});

test('reset clears tracked ids between meetings', () => {
    const store = createFinalTranscriptStore();

    assert.equal(store.accept({ id: 'seg-1' }), true);
    store.reset();
    assert.equal(store.accept({ id: 'seg-1' }), true);
});
