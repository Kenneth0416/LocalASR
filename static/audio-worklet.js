/**
 * Audio Worklet Processor
 * Captures microphone audio, downsamples it to 16 kHz Int16 PCM, and emits fixed frames.
 */

class AudioCaptureProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.targetSampleRate = 16000;
        this.frameSamples = 320;
        this.sourceCarry = new Float32Array(0);
        this.frameCarry = new Int16Array(0);
        this.nextSeq = 0;
        this.isDraining = false;

        this.port.onmessage = (event) => {
            const msg = event.data || {};
            if (msg.type === 'configure') {
                this.targetSampleRate = Number(msg.targetSampleRate) || this.targetSampleRate;
                this.frameSamples = Math.max(1, Math.floor(Number(msg.frameSamples) || this.frameSamples));
                this.sourceCarry = new Float32Array(0);
                this.frameCarry = new Int16Array(0);
                this.nextSeq = 0;
                this.isDraining = false;
                return;
            }

            if (msg.type === 'drain') {
                this.isDraining = true;
                this._flushAll();
                this.port.postMessage({
                    type: 'drained',
                    lastSeq: this.nextSeq - 1,
                });
            }
        };
    }

    process(inputs) {
        if (this.isDraining) {
            return true;
        }

        const input = inputs[0];
        if (input && input.length > 0 && input[0] && input[0].length > 0) {
            this._ingestSamples(input[0]);
        }

        return true;
    }

    _ingestSamples(samples) {
        const pcm = this._downsampleChunk(samples);
        if (!pcm.length) return;

        this._appendFrameSamples(pcm);
        this._emitReadyFrames();
    }

    _flushAll() {
        const residual = this._flushResidualSource();
        if (residual.length) {
            this._appendFrameSamples(residual);
        }
        this._emitReadyFrames();
        this._emitPartialFrame();
    }

    _downsampleChunk(samples) {
        const merged = this.sourceCarry.length ? this._concatFloat32(this.sourceCarry, samples) : new Float32Array(samples);
        if (sampleRate <= this.targetSampleRate) {
            this.sourceCarry = new Float32Array(0);
            return this._floatToInt16(merged);
        }

        const ratio = sampleRate / this.targetSampleRate;
        const outputLength = Math.floor(merged.length / ratio);
        if (outputLength <= 0) {
            this.sourceCarry = merged;
            return new Int16Array(0);
        }

        const output = new Int16Array(outputLength);
        let offset = 0;
        for (let i = 0; i < outputLength; i++) {
            const next = Math.min(merged.length, Math.floor((i + 1) * ratio));
            const span = Math.max(1, next - offset);
            let sum = 0;
            for (let j = offset; j < next; j++) sum += merged[j];
            output[i] = this._clampToInt16(sum / span);
            offset = next;
        }

        this.sourceCarry = merged.slice(offset);
        return output;
    }

    _flushResidualSource() {
        if (!this.sourceCarry.length) {
            return new Int16Array(0);
        }

        if (sampleRate <= this.targetSampleRate) {
            const output = this._floatToInt16(this.sourceCarry);
            this.sourceCarry = new Float32Array(0);
            return output;
        }

        const ratio = sampleRate / this.targetSampleRate;
        const outputLength = Math.max(1, Math.round(this.sourceCarry.length / ratio));
        const output = new Int16Array(outputLength);
        let offset = 0;
        for (let i = 0; i < outputLength; i++) {
            const next = i === outputLength - 1
                ? this.sourceCarry.length
                : Math.min(this.sourceCarry.length, Math.floor((i + 1) * ratio));
            const span = Math.max(1, next - offset);
            let sum = 0;
            for (let j = offset; j < next; j++) sum += this.sourceCarry[j];
            output[i] = this._clampToInt16(sum / span);
            offset = next;
        }

        this.sourceCarry = new Float32Array(0);
        return output;
    }

    _appendFrameSamples(samples) {
        if (!samples.length) return;
        this.frameCarry = this.frameCarry.length
            ? this._concatInt16(this.frameCarry, samples)
            : samples;
    }

    _emitReadyFrames() {
        while (this.frameCarry.length >= this.frameSamples) {
            const frame = this.frameCarry.slice(0, this.frameSamples);
            this.frameCarry = this.frameCarry.slice(this.frameSamples);
            this._postFrame(frame);
        }
    }

    _emitPartialFrame() {
        if (!this.frameCarry.length) return;
        const frame = this.frameCarry;
        this.frameCarry = new Int16Array(0);
        this._postFrame(frame);
    }

    _postFrame(frame) {
        const buffer = frame.buffer.slice(frame.byteOffset, frame.byteOffset + frame.byteLength);
        this.port.postMessage({
            type: 'audio_frame',
            seq: this.nextSeq++,
            sampleCount: frame.length,
            pcm: buffer,
        }, [buffer]);
    }

    _concatFloat32(a, b) {
        const merged = new Float32Array(a.length + b.length);
        merged.set(a, 0);
        merged.set(b, a.length);
        return merged;
    }

    _concatInt16(a, b) {
        const merged = new Int16Array(a.length + b.length);
        merged.set(a, 0);
        merged.set(b, a.length);
        return merged;
    }

    _floatToInt16(samples) {
        const output = new Int16Array(samples.length);
        for (let i = 0; i < samples.length; i++) {
            output[i] = this._clampToInt16(samples[i]);
        }
        return output;
    }

    _clampToInt16(sample) {
        const clamped = Math.max(-1, Math.min(1, sample));
        return clamped < 0 ? Math.round(clamped * 0x8000) : Math.round(clamped * 0x7fff);
    }
}

registerProcessor('audio-capture-processor', AudioCaptureProcessor);
