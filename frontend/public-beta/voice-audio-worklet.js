// Resample the actual hardware rate to mono PCM16/24kHz, in 80ms packets.
class VoiceCapture extends AudioWorkletProcessor {
    constructor() {
        super();
        this.pending = [];
        this.position = 0;
        this.output = [];
        this.running = true;
        this.port.onmessage = ({ data }) => {
            if (data === 'pause') {
                this.running = false;
                this.emit();
                this.port.postMessage({ type: 'flushed' });
            }
        };
    }
    emit() {
        if (!this.output.length) return;
        const pcm = new Int16Array(this.output);
        this.output = [];
        this.port.postMessage({ type: 'pcm', pcm: pcm.buffer }, [pcm.buffer]);
    }
    process(inputs) {
        if (!this.running) return true;
        const input = inputs[0]?.[0];
        if (!input) return true;
        this.pending.push(...input);
        while (this.position + 1 < this.pending.length) {
            const i = Math.floor(this.position), fraction = this.position - i;
            const sample = Math.max(-1, Math.min(1, this.pending[i] * (1-fraction) + this.pending[i+1] * fraction));
            this.output.push(Math.round(sample * 32767));
            this.position += sampleRate / 24000;
            if (this.output.length >= 1920) this.emit();
        }
        const used = Math.floor(this.position);
        this.pending.splice(0, used);
        this.position -= used;
        return true;
    }
}
registerProcessor('saksham-voice-capture', VoiceCapture);
