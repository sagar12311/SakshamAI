class SakshamMeetingCaptureProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.targetRate = 16000;
        this.ratio = sampleRate / this.targetRate;
        this.pendingInput = [];
        this.readPosition = 0;
        this.output = [];
        this.port.onmessage = (event) => {
            if (event.data?.type === 'flush') {
                this.emitFrame(true);
            }
        };
    }

    emitFrame(partial = false) {
        if (!this.output.length || (!partial && this.output.length < this.targetRate)) return;
        const length = partial ? this.output.length : this.targetRate;
        const pcm = new Int16Array(length);
        for (let index = 0; index < length; index += 1) {
            const sample = Math.max(-1, Math.min(1, this.output[index]));
            pcm[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
        }
        this.output.splice(0, length);
        this.port.postMessage({ type: 'pcm', pcm: pcm.buffer }, [pcm.buffer]);
    }

    process(inputs) {
        const input = inputs[0]?.[0];
        if (!input?.length) return true;
        for (let index = 0; index < input.length; index += 1) {
            this.pendingInput.push(input[index]);
        }

        while (this.readPosition + 1 < this.pendingInput.length) {
            const base = Math.floor(this.readPosition);
            const fraction = this.readPosition - base;
            const value = this.pendingInput[base] * (1 - fraction)
                + this.pendingInput[base + 1] * fraction;
            this.output.push(value);
            this.readPosition += this.ratio;
            if (this.output.length >= this.targetRate) this.emitFrame();
        }

        const consumed = Math.floor(this.readPosition);
        if (consumed > 0) {
            this.pendingInput.splice(0, consumed);
            this.readPosition -= consumed;
        }
        return true;
    }
}

registerProcessor('saksham-meeting-capture', SakshamMeetingCaptureProcessor);
