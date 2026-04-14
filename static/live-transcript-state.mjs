function normalizeSegment(input = {}) {
    const id = String(input.id || '');
    const segmentId = String(input.segment_id ?? input.id ?? '');

    return {
        id,
        segmentId,
        revision: Number(input.revision || 0),
        isFinal: Boolean(input.is_final),
        persisted: Boolean(id),
        speaker: String(input.speaker || '发言人'),
        text: String(input.text || ''),
        start: Number(input.start ?? input.start_time ?? 0),
        end: Number(input.end ?? input.end_time ?? 0),
        cutReason: String(input.cut_reason || ''),
    };
}

function compareSegmentIds(a, b) {
    const aNum = Number(a);
    const bNum = Number(b);
    if (Number.isFinite(aNum) && Number.isFinite(bNum)) {
        return aNum - bNum;
    }
    return String(a).localeCompare(String(b));
}

export function createLiveTranscriptState() {
    const bySegmentId = new Map();

    return {
        apply(input) {
            const next = normalizeSegment(input);
            if (!next.segmentId) {
                return { changed: false, entry: null };
            }

            const current = bySegmentId.get(next.segmentId);
            if (current && next.revision <= current.revision) {
                return { changed: false, entry: current };
            }

            const merged = current
                ? { ...current, ...next, persisted: current.persisted || next.persisted }
                : next;

            bySegmentId.set(next.segmentId, merged);
            return { changed: true, entry: merged };
        },

        get(segmentId) {
            return bySegmentId.get(String(segmentId)) || null;
        },

        values() {
            return [...bySegmentId.values()].sort((a, b) => compareSegmentIds(a.segmentId, b.segmentId));
        },

        reset() {
            bySegmentId.clear();
        },
    };
}

export function findLiveTranscriptGroup(root, segmentId) {
    const selector = `[data-live-segment-id="${String(segmentId)}"]`;
    const match = root?.querySelector?.(selector);
    if (!match) {
        return null;
    }
    if (match.classList?.contains?.('segment-group')) {
        return match;
    }
    return match.closest?.('.segment-group') || null;
}

export function upsertLiveTranscriptGroup(listEl, segmentId, nextNode) {
    const existing = findLiveTranscriptGroup(listEl, segmentId);
    if (existing) {
        existing.replaceWith(nextNode);
        return nextNode;
    }
    listEl.appendChild(nextNode);
    return nextNode;
}
