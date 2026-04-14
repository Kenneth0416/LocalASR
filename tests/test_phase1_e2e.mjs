import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

const HOST = '127.0.0.1';
const PORT = 8810;
const ORIGIN = `http://${HOST}:${PORT}`;
const CDP_PORT = 9222;
const CHROME_PATH = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

async function waitForServer(url, timeoutMs = 15000) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
        try {
            const response = await fetch(url);
            if (response.ok) return;
        } catch (_) {}
        await delay(250);
    }
    throw new Error(`Server did not become ready: ${url}`);
}

async function waitForChrome(timeoutMs = 15000) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
        try {
            const response = await fetch(`http://${HOST}:${CDP_PORT}/json/list`);
            if (response.ok) return;
        } catch (_) {}
        await delay(250);
    }
    throw new Error('Chrome DevTools endpoint did not become ready');
}

async function getPageWebSocketUrl(targetUrl) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < 15000) {
        const response = await fetch(`http://${HOST}:${CDP_PORT}/json/list`);
        const targets = await response.json();
        const page = targets.find((entry) => entry.type === 'page' && entry.url.startsWith(targetUrl));
        if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
        await delay(250);
    }
    throw new Error(`No page target found for ${targetUrl}`);
}

class CDPClient {
    constructor(url) {
        this.url = url;
        this.socket = null;
        this.nextId = 1;
        this.pending = new Map();
        this.listeners = new Map();
    }

    async connect() {
        this.socket = new WebSocket(this.url);
        await new Promise((resolve, reject) => {
            this.socket.addEventListener('open', resolve, { once: true });
            this.socket.addEventListener('error', reject, { once: true });
        });

        this.socket.addEventListener('message', (event) => {
            const payload = JSON.parse(event.data);
            if (payload.id) {
                const pending = this.pending.get(payload.id);
                if (!pending) return;
                this.pending.delete(payload.id);
                if (payload.error) {
                    pending.reject(new Error(payload.error.message));
                    return;
                }
                pending.resolve(payload.result);
                return;
            }

            const handlers = this.listeners.get(payload.method) || [];
            for (const handler of handlers) handler(payload.params || {});
        });
    }

    send(method, params = {}) {
        const id = this.nextId++;
        this.socket.send(JSON.stringify({ id, method, params }));
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
        });
    }

    on(method, handler) {
        const handlers = this.listeners.get(method) || [];
        handlers.push(handler);
        this.listeners.set(method, handlers);
    }

    async evaluate(expression) {
        const result = await this.send('Runtime.evaluate', {
            expression,
            awaitPromise: true,
            returnByValue: true,
        });
        if (result.exceptionDetails) {
            throw new Error(result.exceptionDetails.text || 'Runtime.evaluate failed');
        }
        return result.result?.value;
    }

    async close() {
        if (!this.socket) return;
        this.socket.close();
        await delay(100);
    }
}

async function waitForCondition(client, expression, timeoutMs = 10000) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
        const value = await client.evaluate(expression);
        if (value) return value;
        await delay(200);
    }
    throw new Error(`Condition timed out: ${expression}`);
}

function assert(condition, message) {
    if (!condition) throw new Error(message);
}

async function waitForExit(child) {
    if (child.exitCode !== null || child.signalCode !== null) return;
    await new Promise((resolve) => child.once('exit', resolve));
}

async function main() {
    const chromeProfileDir = await mkdtemp(path.join(tmpdir(), 'meeting-realtime-chrome-'));

    const serverProcess = spawn('venv/bin/python', ['tests/e2e_server.py', '--port', String(PORT)], {
        cwd: process.cwd(),
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    const serverOutput = [];
    serverProcess.stdout.on('data', (chunk) => serverOutput.push(chunk.toString()));
    serverProcess.stderr.on('data', (chunk) => serverOutput.push(chunk.toString()));

    const chromeProcess = spawn(CHROME_PATH, [
        '--headless=new',
        '--disable-gpu',
        '--no-first-run',
        '--no-default-browser-check',
        `--user-data-dir=${chromeProfileDir}`,
        `--remote-debugging-port=${CDP_PORT}`,
        `${ORIGIN}/`,
    ], {
        cwd: process.cwd(),
        stdio: ['ignore', 'pipe', 'pipe'],
    });
    const chromeOutput = [];
    chromeProcess.stdout.on('data', (chunk) => chromeOutput.push(chunk.toString()));
    chromeProcess.stderr.on('data', (chunk) => chromeOutput.push(chunk.toString()));

    try {
        await waitForServer(`${ORIGIN}/api/health`);
        await waitForChrome();

        const webSocketUrl = await getPageWebSocketUrl(`${ORIGIN}/`);
        const client = new CDPClient(webSocketUrl);
        await client.connect();
        await client.send('Page.enable');
        await client.send('Runtime.enable');

        await waitForCondition(
            client,
            `document.readyState === 'complete' && !!document.querySelector('#historyBtn')`,
            15000,
        );
        await waitForCondition(
            client,
            `document.querySelectorAll('.history-card').length === 1`,
            15000,
        );

        await client.evaluate(`document.querySelector('#historyBtn').click()`);
        await waitForCondition(client, `document.querySelector('#historyDrawer').classList.contains('is-open')`);

        const historyCardText = await client.evaluate(`
            (() => {
                const card = document.querySelector('.history-card');
                return card ? card.innerText : '';
            })()
        `);
        assert(historyCardText.includes('周五上线'), 'History card preview did not render expected meeting summary');

        await client.evaluate(`document.querySelector('.history-card').click()`);
        await waitForCondition(client, `document.querySelector('#meetingBanner') && document.querySelector('#meetingBanner').hidden === false`);
        await waitForCondition(client, `document.querySelectorAll('.transcript-block').length === 1`);

        const initialSummary = await client.evaluate(`document.querySelector('#summaryText').innerText`);
        const initialTranscript = await client.evaluate(`document.querySelector('.segment-text').innerText`);
        assert(initialSummary.includes('周五上线'), 'Persisted summary did not render in main view');
        assert(initialTranscript.includes('周五上线'), 'Persisted transcript did not render in main view');

        await client.evaluate(`
            (() => {
                window.__downloads = [];
                HTMLAnchorElement.prototype.click = function clickOverride() {
                    window.__downloads.push(this.href);
                };
                window.confirm = () => true;
                return true;
            })()
        `);

        await client.evaluate(`document.querySelector('#historyBtn').click()`);
        await waitForCondition(client, `document.querySelector('#historyDrawer').classList.contains('is-open')`);
        await waitForCondition(client, `document.querySelector('#downloadJsonBtn').disabled === false`);

        await client.evaluate(`document.querySelector('#downloadTxtBtn').click()`);
        await client.evaluate(`document.querySelector('#downloadMdBtn').click()`);
        await client.evaluate(`document.querySelector('#downloadJsonBtn').click()`);
        await client.evaluate(`document.querySelector('#downloadRecordingBtn').click()`);

        const downloads = await client.evaluate(`window.__downloads.slice()`);
        assert(downloads.includes(`${ORIGIN}/api/meetings/e2e-meeting-001/export.txt`), 'TXT export was not triggered');
        assert(downloads.includes(`${ORIGIN}/api/meetings/e2e-meeting-001/export.md`), 'Markdown export was not triggered');
        assert(downloads.includes(`${ORIGIN}/api/meetings/e2e-meeting-001/export.json`), 'JSON export was not triggered');
        assert(downloads.includes(`${ORIGIN}/api/meetings/e2e-meeting-001/recording`), 'Recording download was not triggered');

        await client.evaluate(`document.querySelector('#historyCloseBtn').click()`);
        await waitForCondition(client, `!document.querySelector('#historyDrawer').classList.contains('is-open')`);

        await client.evaluate(`document.querySelector('[data-action="edit"]').click()`);
        await waitForCondition(client, `!!document.querySelector('.segment-editor-text')`);
        await client.evaluate(`
            (() => {
                document.querySelector('.segment-editor-speaker').value = '主持人';
                document.querySelector('.segment-editor-text').value = '我们决定在周五上线，并在周四完成回归和验收。';
                document.querySelector('[data-action="save"]').click();
                return true;
            })()
        `);
        await waitForCondition(
            client,
            `document.querySelector('.segment-speaker') && document.querySelector('.segment-speaker').innerText.includes('主持人')`,
        );
        const editedTranscript = await client.evaluate(`document.querySelector('.segment-text').innerText`);
        assert(editedTranscript.includes('回归和验收'), 'Edited transcript did not persist back into the page');

        await client.evaluate(`document.querySelector('#historyBtn').click()`);
        await waitForCondition(client, `document.querySelector('#historyDrawer').classList.contains('is-open')`);
        await client.evaluate(`document.querySelector('#regenerateStoredSummaryBtn').click()`);
        await waitForCondition(
            client,
            `document.querySelector('#summaryText').innerText.includes('重算摘要')`,
            10000,
        );

        await client.evaluate(`document.querySelector('#deleteMeetingBtn').click()`);
        await waitForCondition(client, `document.querySelectorAll('.history-card').length === 0`, 10000);
        await waitForCondition(
            client,
            `document.querySelector('#summaryText').innerText.includes('会议进行中时') || document.querySelector('#summaryText').innerText.includes('这场会议还没有摘要')`,
            10000,
        );

        await client.close();
        console.log('E2E PASS: history panel, meeting detail, transcript edit, exports, summary regenerate, delete');
    } finally {
        serverProcess.kill('SIGTERM');
        chromeProcess.kill('SIGTERM');
        await Promise.allSettled([
            waitForExit(serverProcess),
            waitForExit(chromeProcess),
            rm(chromeProfileDir, { recursive: true, force: true }),
        ]);

        if (serverProcess.exitCode && serverProcess.exitCode !== 0) {
            console.error(serverOutput.join(''));
        }
        if (chromeProcess.exitCode && chromeProcess.exitCode !== 0) {
            console.error(chromeOutput.join(''));
        }
    }
}

main().catch((error) => {
    console.error(`E2E FAIL: ${error.message}`);
    process.exitCode = 1;
});
