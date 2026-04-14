/**
 * UI Formatters — Utilities for safe HTML rendering
 */

const ESCAPE_MAP = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
};

export function escapeHtml(text = '') {
    return String(text).replace(/[&<>"']/g, (ch) => ESCAPE_MAP[ch]);
}

/**
 * Format a meeting summary with basic markdown-like syntax.
 * Supports:
 *   **bold**
 *   *italic*
 *   numbered lists (1. item)
 *   bullet lists (- item or • item)
 *   paragraphs (double newline)
 */
export function formatSummary(text) {
    if (!text) return '<p class="empty-hint">暂无摘要</p>';

    // Split into blocks by double newlines
    const blocks = text.trim().split(/\n{2,}/);

    return blocks.map(block => {
        const trimmed = block.trim();
        if (!trimmed) return '';

        // Numbered list: each line starts with "N. "
        if (/^\d+\.\s/m.test(trimmed)) {
            const items = trimmed.split('\n')
                .filter(line => /^\d+\.\s/.test(line))
                .map(line => `<li>${formatInline(line.replace(/^\d+\.\s/, ''))}</li>`)
                .join('');
            return items ? `<ol>${items}</ol>` : `<p>${formatInline(trimmed)}</p>`;
        }

        // Bullet list
        if (/^[-•]\s/m.test(trimmed)) {
            const items = trimmed.split('\n')
                .filter(line => /^[-•]\s/.test(line))
                .map(line => `<li>${formatInline(line.replace(/^[-•]\s/, ''))}</li>`)
                .join('');
            return items ? `<ul>${items}</ul>` : `<p>${formatInline(trimmed)}</p>`;
        }

        // Paragraph
        return `<p>${formatInline(trimmed)}</p>`;
    }).join('');
}

function formatInline(text) {
    return escapeHtml(text)
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
}
