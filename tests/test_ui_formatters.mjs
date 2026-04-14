import test from 'node:test';
import assert from 'node:assert/strict';

import { formatSummary } from '../static/ui-formatters.mjs';

test('formatSummary escapes model output before injecting markup', () => {
    const rendered = formatSummary('<img src=x onerror=alert(1)> **bold**\n*safe*');

    assert.equal(rendered.includes('<img'), false);
    assert.match(rendered, /&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(rendered, /<strong>bold<\/strong>/);
    assert.match(rendered, /<em>safe<\/em>/);
});
