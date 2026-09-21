/**
 * Audio Feedback Utility - Generates notification tones using Web Audio API
 * No external sound files needed - tones are synthesized
 */

let audioContext: AudioContext | null = null;

function getAudioContext(): AudioContext {
    if (!audioContext) {
        audioContext = new (window.AudioContext || (window as any).webkitAudioContext)();
    }
    return audioContext;
}

/**
 * Play a tone with the given frequency and duration
 */
function playTone(frequency: number, duration: number, volume: number = 0.3): void {
    try {
        const ctx = getAudioContext();
        const oscillator = ctx.createOscillator();
        const gainNode = ctx.createGain();

        oscillator.connect(gainNode);
        gainNode.connect(ctx.destination);

        oscillator.frequency.value = frequency;
        oscillator.type = 'sine';

        // Fade in/out to prevent clicks
        gainNode.gain.setValueAtTime(0, ctx.currentTime);
        gainNode.gain.linearRampToValueAtTime(volume, ctx.currentTime + 0.02);
        gainNode.gain.linearRampToValueAtTime(0, ctx.currentTime + duration);

        oscillator.start(ctx.currentTime);
        oscillator.stop(ctx.currentTime + duration);
    } catch (e) {
        console.warn('Audio feedback unavailable:', e);
    }
}

/**
 * Listening started - Rising tone (like "I'm ready")
 * Two-note ascending chime
 */
export function playListeningStarted(): void {
    playTone(440, 0.1, 0.6);  // A4 (Louder)
    setTimeout(() => playTone(587, 0.15, 0.6), 100);  // D5
}

/**
 * Prompt registered - Confirmation tone (like "Got it")
 * Higher pitched double-beep
 */
export function playPromptRegistered(): void {
    playTone(880, 0.1, 0.6);   // A5
    setTimeout(() => playTone(1175, 0.15, 0.6), 120);  // D6 (Ascending "Success" Chime)
}

/**
 * Response finished - Descending tone (like "Done speaking")
 */
export function playResponseFinished(): void {
    playTone(587, 0.1, 0.2);  // D5
    setTimeout(() => playTone(440, 0.15, 0.2), 100);  // A4
}

/**
 * Error tone - Low buzz
 */
export function playError(): void {
    playTone(220, 0.2, 0.15);  // A3
}
