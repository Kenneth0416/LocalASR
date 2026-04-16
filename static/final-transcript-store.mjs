export function createFinalTranscriptStore() {
    const seenIds = new Set();

    return {
        accept(segment = {}) {
            const id = String(segment.id || '');
            if (!id || seenIds.has(id)) {
                return false;
            }
            seenIds.add(id);
            return true;
        },

        reset() {
            seenIds.clear();
        },
    };
}
