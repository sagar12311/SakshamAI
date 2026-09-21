/**
 * Saksham AI - Ambient Listening Hook
 * Always-on voice capture with real audio playback for responses
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { playListeningStarted, playPromptRegistered, playResponseFinished } from '../utils/audioFeedback';

interface TranscriptionEvent {
    text: string;
    timestamp: Date;
    shouldRespond: boolean;
    sessionActive?: boolean;
}

interface UseAmbientListeningOptions {
    conversationId?: string;
    onTranscription?: (event: TranscriptionEvent) => void;
    onResponse?: (text: string) => void;
    onSpeaking?: (isSpeaking: boolean) => void;
    onVoiceActivity?: (level: number) => void;  // 0-1 audio level
    onError?: (error: Error) => void;
    enabled?: boolean;
}

export function useAmbientListening(options: UseAmbientListeningOptions = {}) {
    const {
        conversationId = 'default',
        onTranscription,
        onResponse,
        onSpeaking,
        onVoiceActivity,
        onError,
        enabled = true,
    } = options;

    const [isListening, setIsListening] = useState(false);
    const [hasPermission, setHasPermission] = useState<boolean | null>(null);
    const [transcriptionBuffer, setTranscriptionBuffer] = useState<TranscriptionEvent[]>([]);
    const [isSpeaking, setIsSpeaking] = useState(false);
    const [isUserSpeaking, setIsUserSpeaking] = useState(false);
    const [audioLevel, setAudioLevel] = useState(0);

    const wsRef = useRef<WebSocket | null>(null);
    const streamRef = useRef<MediaStream | null>(null);
    const audioContextRef = useRef<AudioContext | null>(null);
    const processorRef = useRef<ScriptProcessorNode | null>(null);
    const audioQueueRef = useRef<Blob[]>([]);
    const isPlayingRef = useRef(false);
    const currentAudioRef = useRef<HTMLAudioElement | null>(null);
    const currentAudioUrlRef = useRef<string | null>(null);
    const playbackTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const playbackResumeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const isListeningRef = useRef(false);
    const isSpeakingRef = useRef(false);
    const startInFlightRef = useRef(false);
    const startAttemptRef = useRef(0);

    // Update refs to keep them fresh without triggering re-renders of dependencies
    useEffect(() => {
        isListeningRef.current = isListening;
    }, [isListening]);

    useEffect(() => {
        isSpeakingRef.current = isSpeaking;
    }, [isSpeaking]);

    const callbacksRef = useRef({ onTranscription, onResponse, onSpeaking, onVoiceActivity, onError });
    useEffect(() => {
        callbacksRef.current = { onTranscription, onResponse, onSpeaking, onVoiceActivity, onError };
    }, [onTranscription, onResponse, onSpeaking, onVoiceActivity, onError]);

    const getVoiceWebSocketUrl = useCallback(() => {
        const backendBaseUrl = (window as Window & {
            saksham?: {
                backendBaseUrl?: string;
            };
        }).saksham?.backendBaseUrl;

        const baseUrl = backendBaseUrl
            ? backendBaseUrl.replace(/^http/i, 'ws') + '/ws/voice'
            : `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.hostname || '127.0.0.1'}:8420/ws/voice`;
        return `${baseUrl}?conversation_id=${encodeURIComponent(conversationId)}`;
    }, [conversationId]);

    const notifyPlaybackState = useCallback((speaking: boolean) => {
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'playback_state', speaking }));
        }
    }, []);

    const stopAudioPlayback = useCallback(() => {
        if (playbackTimeoutRef.current) {
            clearTimeout(playbackTimeoutRef.current);
            playbackTimeoutRef.current = null;
        }
        if (playbackResumeTimeoutRef.current) {
            clearTimeout(playbackResumeTimeoutRef.current);
            playbackResumeTimeoutRef.current = null;
        }

        const audio = currentAudioRef.current;
        if (audio) {
            audio.onended = null;
            audio.onerror = null;
            audio.pause();
            try {
                audio.currentTime = 0;
            } catch {
                // Some browsers reject seeking while media metadata is unavailable.
            }
            currentAudioRef.current = null;
        }
        if (currentAudioUrlRef.current) {
            URL.revokeObjectURL(currentAudioUrlRef.current);
            currentAudioUrlRef.current = null;
        }

        audioQueueRef.current = [];
        isPlayingRef.current = false;
        isSpeakingRef.current = false;
        setIsSpeaking(false);
        callbacksRef.current.onSpeaking?.(false);
        notifyPlaybackState(false);
    }, [notifyPlaybackState]);

    // Play audio from queue
    const playNextAudio = useCallback(async () => {
        if (isPlayingRef.current || audioQueueRef.current.length === 0) return;

        isPlayingRef.current = true;
        isSpeakingRef.current = true;
        setIsSpeaking(true);
        callbacksRef.current.onSpeaking?.(true);

        const audioBlob = audioQueueRef.current.shift()!;

        // Fail safe for a stalled media element, not a normal response-length limit.
        playbackTimeoutRef.current = setTimeout(() => {
            console.warn('⚠️ Audio playback timeout - resetting state');
            stopAudioPlayback();
        }, 120000);

        try {
            const audioUrl = URL.createObjectURL(audioBlob);
            const audio = new Audio(audioUrl);
            currentAudioRef.current = audio;
            currentAudioUrlRef.current = audioUrl;

            audio.onended = () => {
                if (playbackTimeoutRef.current) {
                    clearTimeout(playbackTimeoutRef.current);
                    playbackTimeoutRef.current = null;
                }
                URL.revokeObjectURL(audioUrl);
                currentAudioRef.current = null;
                currentAudioUrlRef.current = null;

                // Add short delay before listening again to prevent echo/reverb capture
                playbackResumeTimeoutRef.current = setTimeout(() => {
                    playbackResumeTimeoutRef.current = null;
                    isPlayingRef.current = false;

                    if (audioQueueRef.current.length > 0) {
                        void playNextAudio();
                    } else {
                        isSpeakingRef.current = false;
                        setIsSpeaking(false);
                        callbacksRef.current.onSpeaking?.(false);
                        notifyPlaybackState(false);
                        playResponseFinished();  // Audio feedback: you can speak now
                    }
                }, 500); // 500ms deaf period
            };

            audio.onerror = (e) => {
                console.error('Audio playback error:', e);
                stopAudioPlayback();
            };

            await audio.play();
            notifyPlaybackState(true);
        } catch (error) {
            console.error('Failed to play audio:', error);
            stopAudioPlayback();
        }
    }, [notifyPlaybackState, stopAudioPlayback]);

    // Connect to WebSocket for real-time processing
    const connectWebSocket = useCallback(() => {
        if (wsRef.current && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) {
            return wsRef.current;
        }

        const ws = new WebSocket(getVoiceWebSocketUrl());
        ws.binaryType = 'arraybuffer'; // Important for receiving audio

        ws.onopen = () => {
            console.log('🎤 Voice WebSocket connected');
        };

        ws.onmessage = (event) => {
            // Check if it's binary (audio) or text (JSON)
            if (event.data instanceof ArrayBuffer) {
                // Audio response - add to queue and play
                console.log('🔊 Received audio bytes:', event.data.byteLength);

                // Detect Audio Format: msgpack or raw bytes? No, we send raw bytes.
                // Check if WAV (starts with "RIFF") to support Piper TTS
                let mimeType = 'audio/mpeg'; // Default to MP3 (EdgeTTS)
                try {
                    const header = new Uint8Array(event.data, 0, 4);
                    // 'R', 'I', 'F', 'F' -> 82, 73, 70, 70
                    if (header[0] === 82 && header[1] === 73 && header[2] === 70 && header[3] === 70) {
                        mimeType = 'audio/wav';
                    }
                } catch (e) {
                    // Ignore, fallback to mpeg
                }

                const audioBlob = new Blob([event.data], { type: mimeType });
                audioQueueRef.current.push(audioBlob);
                playNextAudio();
            } else {
                // JSON message
                try {
                    const data = JSON.parse(event.data);

                    if (data.type === 'transcription' && data.text) {
                        const transcriptionEvent: TranscriptionEvent = {
                            text: data.text,
                            timestamp: new Date(),
                            shouldRespond: data.should_respond || false,
                            sessionActive: data.session_active,
                        };

                        setTranscriptionBuffer(prev => [...prev.slice(-50), transcriptionEvent]);
                        callbacksRef.current.onTranscription?.(transcriptionEvent);
                    }

                    if (data.type === 'response' && data.text) {
                        callbacksRef.current.onResponse?.(data.text);
                    }

                    if (data.type === 'response_chunk') {
                        console.log('Response chunk:', data.text);
                    }

                    if (data.type === 'playback_control' && data.action === 'stop') {
                        console.log('🛑 Stopping audio playback:', data.reason);
                        stopAudioPlayback();
                    }
                } catch (e) {
                    console.error('Failed to parse WebSocket message:', e);
                }
            }
        };

        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
            callbacksRef.current.onError?.(new Error('Voice websocket connection failed'));
        };

        ws.onclose = () => {
            console.log('🎤 Voice WebSocket disconnected');
            wsRef.current = null;
            // Reconnect logic using Ref (stable)
            if (isListeningRef.current) {
                setTimeout(connectWebSocket, 3000);
            }
        };

        wsRef.current = ws;
        return ws;
    }, [getVoiceWebSocketUrl, playNextAudio, stopAudioPlayback]);

    // Start ambient listening
    const startListening = useCallback(async () => {
        if (startInFlightRef.current || streamRef.current || audioContextRef.current) {
            return;
        }

        startInFlightRef.current = true;
        const startAttempt = ++startAttemptRef.current;
        let audioContext: AudioContext | null = null;
        let stream: MediaStream | null = null;

        try {
            // Create and resume the context while this function is still running from
            // the Listening button's user gesture. Chromium may otherwise defer it
            // until the next click or key press.
            const AudioContextClass = window.AudioContext || (window as any).webkitAudioContext;
            if (!AudioContextClass) {
                throw new Error('AudioContext is not available in this environment');
            }

            audioContext = new AudioContextClass({ latencyHint: 'interactive' });
            audioContextRef.current = audioContext;
            const initialResume = audioContext.state === 'suspended'
                ? audioContext.resume()
                : Promise.resolve();

            // Request microphone permission
            stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                    sampleRate: 16000,
                    channelCount: 1,
                },
            });

            if (startAttempt !== startAttemptRef.current) {
                stream.getTracks().forEach(track => track.stop());
                await audioContext.close();
                return;
            }

            setHasPermission(true);
            streamRef.current = stream;

            await initialResume;
            if (audioContext.state === 'suspended') {
                await audioContext.resume();
            }

            if (audioContext.state !== 'running') {
                throw new Error(`Microphone audio engine did not start (${audioContext.state})`);
            }

            console.log('🎤 AudioContext started. State:', audioContext.state);

            const activeAudioContext = audioContext;
            const source = activeAudioContext.createMediaStreamSource(stream);
            const processor = activeAudioContext.createScriptProcessor(2048, 1, 1);
            const muteNode = activeAudioContext.createGain();
            muteNode.gain.value = 0;
            processorRef.current = processor;

            // Connect WebSocket
            connectWebSocket();

            // Buffer for audio chunks
            let audioBuffer: Float32Array[] = [];
            let prerollBuffer: Float32Array[] = [];
            let noiseFloor = 0.003;
            let silenceDurationMs = 0;
            let speechDurationMs = 0;
            let consecutiveSpeechDurationMs = 0;
            let bufferedDurationMs = 0;
            let hasValidSpeechStart = false;
            let isBargeInCapture = false;

            const MIN_SIGNAL_THRESHOLD = 0.008;
            const SPEECH_MULTIPLIER = 2.5;
            const BARGE_IN_SIGNAL_THRESHOLD = 0.015;
            const BARGE_IN_SPEECH_MULTIPLIER = 3.5;
            const MIN_CONSECUTIVE_SPEECH_MS = 120;
            const MIN_SPEECH_MS = 400;
            const BARGE_IN_MIN_SPEECH_MS = 180;
            // Allow natural mid-sentence pauses before treating speech as a full turn.
            // The backend NLU layer performs a second semantic merge across chunks.
            const END_OF_UTTERANCE_SILENCE_MS = 950;
            const MAX_PREROLL_MS = 200;
            const MAX_BUFFER_MS = 7000;

            const notifySpeechActivity = (speaking: boolean) => {
                const ws = wsRef.current;
                if (ws?.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: 'speech_activity', speaking }));
                }
            };

            const resetCaptureState = () => {
                audioBuffer = [];
                prerollBuffer = [];
                silenceDurationMs = 0;
                speechDurationMs = 0;
                consecutiveSpeechDurationMs = 0;
                bufferedDurationMs = 0;
                hasValidSpeechStart = false;
                isBargeInCapture = false;
                setIsUserSpeaking(false);
                setAudioLevel(0);
                callbacksRef.current.onVoiceActivity?.(0);
            };

            const flushAudioBuffer = (reason: 'silence' | 'buffer_limit') => {
                if (audioBuffer.length === 0) {
                    resetCaptureState();
                    return;
                }

                const ws = wsRef.current;
                if (!ws || ws.readyState !== WebSocket.OPEN) {
                    console.warn('🎤 Dropping audio chunk because voice websocket is not open');
                    resetCaptureState();
                    return;
                }

                const combinedBuffer = flattenBuffers(audioBuffer);
                const wavBuffer = encodeWAV(combinedBuffer, activeAudioContext.sampleRate);
                ws.send(wavBuffer);
                if (!isBargeInCapture) {
                    playPromptRegistered();
                }
                console.log(`🎤 Sent ${Math.round(bufferedDurationMs)}ms of audio (${reason}, ${wavBuffer.byteLength} bytes)`);
                resetCaptureState();
            };

            processor.onaudioprocess = (e) => {
                const inputData = e.inputBuffer.getChannelData(0);
                const inputArray = new Float32Array(inputData);
                const frameDurationMs = (inputArray.length / activeAudioContext.sampleRate) * 1000;
                const bargeInMode = (
                    isBargeInCapture
                    || isSpeakingRef.current
                    || isPlayingRef.current
                );

                // Calculate RMS for voice activity detection
                let sum = 0;
                for (let i = 0; i < inputArray.length; i++) {
                    sum += inputArray[i] * inputArray[i];
                }
                const rms = Math.sqrt(sum / inputArray.length);

                noiseFloor = hasValidSpeechStart
                    ? noiseFloor
                    : (noiseFloor * 0.92) + (Math.min(rms, 0.02) * 0.08);

                const speechThreshold = Math.max(
                    bargeInMode ? BARGE_IN_SIGNAL_THRESHOLD : MIN_SIGNAL_THRESHOLD,
                    noiseFloor * (bargeInMode ? BARGE_IN_SPEECH_MULTIPLIER : SPEECH_MULTIPLIER),
                );
                const normalizedLevel = Math.min(rms / Math.max(speechThreshold * 3, 0.03), 1);
                callbacksRef.current.onVoiceActivity?.(normalizedLevel);

                const frameCopy = inputArray.slice();
                const isSpeechFrame = rms >= speechThreshold;

                if (!hasValidSpeechStart) {
                    prerollBuffer.push(frameCopy);
                    const maxPrerollFrames = Math.max(1, Math.ceil(MAX_PREROLL_MS / Math.max(frameDurationMs, 1)));
                    if (prerollBuffer.length > maxPrerollFrames) {
                        prerollBuffer.shift();
                    }
                }

                if (isSpeechFrame) {
                    silenceDurationMs = 0;
                    consecutiveSpeechDurationMs += frameDurationMs;

                    const minimumConsecutiveSpeechMs = bargeInMode
                        ? 180
                        : MIN_CONSECUTIVE_SPEECH_MS;
                    if (!hasValidSpeechStart && consecutiveSpeechDurationMs >= minimumConsecutiveSpeechMs) {
                        hasValidSpeechStart = true;
                        notifySpeechActivity(true);
                        if (bargeInMode) {
                            isBargeInCapture = true;
                            console.log('🛑 User barge-in detected; pausing Saksham playback');
                            stopAudioPlayback();
                        }
                        audioBuffer = prerollBuffer.map((buffer) => buffer.slice());
                        bufferedDurationMs = audioBuffer.length * frameDurationMs;
                        prerollBuffer = [];
                    }

                    if (hasValidSpeechStart) {
                        audioBuffer.push(frameCopy);
                        bufferedDurationMs += frameDurationMs;
                        speechDurationMs += frameDurationMs;
                        setIsUserSpeaking(true);
                        setAudioLevel(normalizedLevel);
                    }
                } else {
                    consecutiveSpeechDurationMs = 0;

                    if (!hasValidSpeechStart) {
                        setIsUserSpeaking(false);
                        setAudioLevel(0);
                        return;
                    }

                    silenceDurationMs += frameDurationMs;
                    audioBuffer.push(frameCopy);
                    bufferedDurationMs += frameDurationMs;

                    const endOfUtteranceMs = bargeInMode ? 350 : END_OF_UTTERANCE_SILENCE_MS;
                    const minimumSpeechMs = bargeInMode ? BARGE_IN_MIN_SPEECH_MS : MIN_SPEECH_MS;
                    if (silenceDurationMs >= endOfUtteranceMs) {
                        if (speechDurationMs >= minimumSpeechMs) {
                            flushAudioBuffer('silence');
                        } else {
                            console.log(`🗑️ Discarding short audio (${Math.round(speechDurationMs)}ms speech)`);
                            notifySpeechActivity(false);
                            resetCaptureState();
                        }
                        return;
                    }

                    if (silenceDurationMs >= 150) {
                        setIsUserSpeaking(false);
                        setAudioLevel(0);
                    }
                }

                if (hasValidSpeechStart && bufferedDurationMs >= MAX_BUFFER_MS) {
                    flushAudioBuffer('buffer_limit');
                }
            };

            source.connect(processor);
            processor.connect(muteNode);
            muteNode.connect(activeAudioContext.destination);

            setIsListening(true);
            playListeningStarted();  // Audio feedback: listening started
            console.log('🎤 Ambient listening started');

        } catch (error) {
            console.error('Failed to start listening:', error);

            if (stream) {
                stream.getTracks().forEach(track => track.stop());
            }
            if (streamRef.current === stream) {
                streamRef.current = null;
            }
            if (audioContext && audioContext.state !== 'closed') {
                void audioContext.close().catch(() => undefined);
            }
            if (audioContextRef.current === audioContext) {
                audioContextRef.current = null;
            }

            setHasPermission(false);
            onError?.(error as Error);
        } finally {
            startInFlightRef.current = false;
        }
    }, [connectWebSocket, onError, stopAudioPlayback]);

    // Stop listening
    const stopListening = useCallback(() => {
        startAttemptRef.current += 1;
        isListeningRef.current = false;
        startInFlightRef.current = false;

        if (streamRef.current) {
            streamRef.current.getTracks().forEach(track => track.stop());
            streamRef.current = null;
        }

        if (processorRef.current) {
            processorRef.current.onaudioprocess = null;
            processorRef.current.disconnect();
            processorRef.current = null;
        }

        if (audioContextRef.current) {
            void audioContextRef.current.close().catch((error) => {
                console.error('Failed to close audio context:', error);
            });
            audioContextRef.current = null;
        }

        stopAudioPlayback();

        if (wsRef.current) {
            wsRef.current.onclose = null;
            wsRef.current.close();
            wsRef.current = null;
        }

        setIsUserSpeaking(false);
        setAudioLevel(0);
        setIsSpeaking(false);
        callbacksRef.current.onSpeaking?.(false);
        setIsListening(false);
        console.log('🎤 Ambient listening stopped');
    }, [stopAudioPlayback]);

    // Auto-start if enabled
    useEffect(() => {
        if (enabled && !isListening && hasPermission !== false) {
            void startListening();
        } else if (!enabled && isListening) {
            stopListening();
        }
    }, [enabled, hasPermission, isListening, startListening, stopListening]);

    useEffect(() => {
        return () => {
            stopListening();
        };
    }, [stopListening]);

    // Send JSON message
    const sendMessage = useCallback((data: any) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify(data));
        }
    }, []);

    return {
        isListening,
        hasPermission,
        transcriptionBuffer,
        isSpeaking,
        isUserSpeaking,
        audioLevel,
        startListening,
        stopListening,
        sendMessage, // Exported
    };
}

// Helper: Flatten audio buffers
function flattenBuffers(buffers: Float32Array[]): Float32Array {
    const totalLength = buffers.reduce((acc, buf) => acc + buf.length, 0);
    const result = new Float32Array(totalLength);
    let offset = 0;
    for (const buffer of buffers) {
        result.set(buffer, offset);
        offset += buffer.length;
    }
    return result;
}

// Helper: Encode Float32Array to WAV Blob
function encodeWAV(samples: Float32Array, sampleRate: number): ArrayBuffer {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);

    // WAV header
    writeString(view, 0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    writeString(view, 8, 'WAVE');
    writeString(view, 12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(view, 36, 'data');
    view.setUint32(40, samples.length * 2, true);

    // Convert float samples to 16-bit PCM
    const pcmOffset = 44;
    for (let i = 0; i < samples.length; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        view.setInt16(pcmOffset + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }

    return buffer;
}

function writeString(view: DataView, offset: number, string: string) {
    for (let i = 0; i < string.length; i++) {
        view.setUint8(offset + i, string.charCodeAt(i));
    }
}
