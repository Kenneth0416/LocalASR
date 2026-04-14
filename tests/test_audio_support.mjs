import test from 'node:test';
import assert from 'node:assert/strict';

import {
    describeRealtimeAudioSupport,
    requestRealtimeAudioStream,
} from '../static/audio-support.mjs';

test('describes insecure network origins that cannot access microphone APIs', () => {
    const result = describeRealtimeAudioSupport({
        navigatorLike: {},
        windowLike: { AudioContext: class AudioContext {} },
        locationLike: { hostname: '192.168.1.25' },
        isSecureContextValue: false,
    });

    assert.equal(result.supported, false);
    assert.match(result.message, /localhost/);
    assert.match(result.message, /HTTPS/);
});

test('describes unsupported browsers on secure contexts', () => {
    const result = describeRealtimeAudioSupport({
        navigatorLike: {},
        windowLike: { AudioContext: class AudioContext {} },
        locationLike: { hostname: 'localhost' },
        isSecureContextValue: true,
    });

    assert.equal(result.supported, false);
    assert.match(result.message, /Chrome/);
});

test('accepts legacy webkit audio and getUserMedia support', () => {
    function LegacyAudioContext() {}

    const result = describeRealtimeAudioSupport({
        navigatorLike: {
            webkitGetUserMedia() {},
        },
        windowLike: { webkitAudioContext: LegacyAudioContext },
        locationLike: { hostname: 'legacy-browser' },
        isSecureContextValue: true,
    });

    assert.equal(result.supported, true);
    assert.equal(result.audioContextCtor, LegacyAudioContext);
});

test('wraps legacy getUserMedia into a promise', async () => {
    const fakeStream = { id: 'legacy-stream' };
    const constraints = { audio: true, video: false };

    const stream = await requestRealtimeAudioStream(constraints, {
        navigatorLike: {
            webkitGetUserMedia(requested, resolve) {
                assert.deepEqual(requested, constraints);
                resolve(fakeStream);
            },
        },
        windowLike: { webkitAudioContext: class LegacyAudioContext {} },
        locationLike: { hostname: 'legacy-browser' },
        isSecureContextValue: true,
    });

    assert.equal(stream, fakeStream);
});
