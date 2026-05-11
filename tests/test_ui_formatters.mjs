import test from 'node:test';
import assert from 'node:assert/strict';

import { formatSegmentCapture, formatSegmentRange, formatSummary } from '../static/ui-formatters.mjs';

test('formatSummary escapes model output before injecting markup', () => {
    const rendered = formatSummary('<img src=x onerror=alert(1)> **bold**\n*safe*');

    assert.equal(rendered.includes('<img'), false);
    assert.match(rendered, /&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(rendered, /<strong>bold<\/strong>/);
    assert.match(rendered, /<em>safe<\/em>/);
});

test('formatSegmentRange uses spoken timeline while formatSegmentCapture uses capture window', () => {
    const segment = {
        start_time: 0.1,
        end_time: 18.1,
        capture_start_time: 0.0,
        capture_duration: 18.8,
    };

    assert.equal(formatSegmentRange(segment), '0.1s – 18.1s');
    assert.equal(formatSegmentCapture(segment), '采集 0.0s · 长度 18.8s');
});
