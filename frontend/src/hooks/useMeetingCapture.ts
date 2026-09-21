import { useCallback, useEffect, useRef, useState } from 'react';
import type { MeetingEvent } from '../types/meeting';

type StoredFrame = {
    meetingId: string;
    sequence: number;
    pcm: ArrayBuffer;
    createdAt: number;
};

type MeetingCaptureOptions = {
    onEvent: (event: MeetingEvent) => void;
    onError: (message: string) => void;
};

const DB_NAME = 'saksham-meeting-capture';
const STORE_NAME = 'frames';
const MAX_BUFFERED_FRAMES = 600;

function openDatabase(): Promise<IDBDatabase> {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, 1);
        request.onupgradeneeded = () => {
            const database = request.result;
            if (!database.objectStoreNames.contains(STORE_NAME)) {
                const store = database.createObjectStore(STORE_NAME, {
                    keyPath: ['meetingId', 'sequence'],
                });
                store.createIndex('meetingId', 'meetingId', { unique: false });
            }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
}

async function transact<T>(
    mode: IDBTransactionMode,
    operation: (store: IDBObjectStore, resolve: (value: T) => void, reject: (reason?: unknown) => void) => void,
): Promise<T> {
    const database = await openDatabase();
    return new Promise<T>((resolve, reject) => {
        const transaction = database.transaction(STORE_NAME, mode);
        let result: T;
        let hasResult = false;
        let failure: unknown;
        const stageResult = (value: T) => {
            result = value;
            hasResult = true;
        };
        const stageFailure = (reason?: unknown) => {
            failure = reason;
            try {
                transaction.abort();
            } catch {
                // The transaction may already have failed or completed.
            }
        };
        transaction.oncomplete = () => {
            database.close();
            if (hasResult) resolve(result);
            else reject(failure || new Error('IndexedDB transaction completed without a result.'));
        };
        transaction.onerror = () => {
            database.close();
            reject(failure || transaction.error);
        };
        transaction.onabort = () => {
            database.close();
            reject(failure || transaction.error || new Error('IndexedDB transaction was aborted.'));
        };
        try {
            operation(transaction.objectStore(STORE_NAME), stageResult, stageFailure);
        } catch (error) {
            stageFailure(error);
        }
    });
}

function putFrame(frame: StoredFrame): Promise<void> {
    return transact('readwrite', (store, resolve, reject) => {
        const request = store.put(frame);
        request.onsuccess = () => resolve();
        request.onerror = () => reject(request.error);
    });
}

function listFrames(meetingId: string): Promise<StoredFrame[]> {
    return transact('readonly', (store, resolve, reject) => {
        const request = store.index('meetingId').getAll(IDBKeyRange.only(meetingId));
        request.onsuccess = () => resolve(
            (request.result as StoredFrame[]).sort((left, right) => left.sequence - right.sequence),
        );
        request.onerror = () => reject(request.error);
    });
}

function deleteFramesThrough(meetingId: string, sequence: number): Promise<void> {
    return transact('readwrite', (store, resolve, reject) => {
        const request = store.openCursor(
            IDBKeyRange.bound([meetingId, 0], [meetingId, sequence]),
        );
        request.onsuccess = () => {
            const cursor = request.result;
            if (!cursor) {
                resolve();
                return;
            }
            cursor.delete();
            cursor.continue();
        };
        request.onerror = () => reject(request.error);
    });
}

function meetingWebSocketUrl(meetingId: string): string {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}/ws/meetings/${encodeURIComponent(meetingId)}`;
}

function packetFor(frame: StoredFrame): ArrayBuffer {
    const packet = new ArrayBuffer(4 + frame.pcm.byteLength);
    const view = new DataView(packet);
    view.setUint32(0, frame.sequence, false);
    new Uint8Array(packet, 4).set(new Uint8Array(frame.pcm));
    return packet;
}

export function playMeetingChime(kind: 'start' | 'stop'): void {
    const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    if (!AudioContextClass) return;
    const context = new AudioContextClass();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = 'sine';
    oscillator.frequency.value = kind === 'start' ? 660 : 440;
    gain.gain.setValueAtTime(0.0001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.22);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.24);
    oscillator.onended = () => void context.close();
}

export function useMeetingCapture({ onEvent, onError }: MeetingCaptureOptions) {
    const [isCapturing, setIsCapturing] = useState(false);
    const [connectionState, setConnectionState] = useState<'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error'>('idle');
    const [bufferedFrames, setBufferedFrames] = useState(0);
    const [bufferWarning, setBufferWarning] = useState('');
    const meetingIdRef = useRef<string | null>(null);
    const websocketRef = useRef<WebSocket | null>(null);
    const streamRef = useRef<MediaStream | null>(null);
    const contextRef = useRef<AudioContext | null>(null);
    const workletRef = useRef<AudioWorkletNode | null>(null);
    const sequenceRef = useRef(0);
    const activeRef = useRef(false);
    const stopInFlightRef = useRef<Promise<void> | null>(null);
    const reconnectTimerRef = useRef<number | null>(null);
    const writeQueueRef = useRef<Promise<void>>(Promise.resolve());
    const callbacksRef = useRef({ onEvent, onError });
    callbacksRef.current = { onEvent, onError };

    const refreshBufferedCount = useCallback(async () => {
        const meetingId = meetingIdRef.current;
        if (!meetingId) return;
        const frames = await listFrames(meetingId);
        setBufferedFrames(frames.length);
        if (frames.length >= MAX_BUFFERED_FRAMES * 0.9) {
            setBufferWarning('Capture reconnect buffer is nearly full. Restore the backend connection now.');
        } else {
            setBufferWarning('');
        }
    }, []);

    const sendStoredFrames = useCallback(async (minimumSequence = 0) => {
        const meetingId = meetingIdRef.current;
        const websocket = websocketRef.current;
        if (!meetingId || websocket?.readyState !== WebSocket.OPEN) return;
        const frames = await listFrames(meetingId);
        for (const frame of frames) {
            if (frame.sequence < minimumSequence || websocket.readyState !== WebSocket.OPEN) continue;
            websocket.send(packetFor(frame));
        }
        setBufferedFrames(frames.length);
    }, []);

    const connect = useCallback((meetingId: string) => {
        if (!activeRef.current) return;
        const current = websocketRef.current;
        if (current && (
            current.readyState === WebSocket.OPEN
            || current.readyState === WebSocket.CONNECTING
        )) return;
        setConnectionState(current ? 'reconnecting' : 'connecting');
        const websocket = new WebSocket(meetingWebSocketUrl(meetingId));
        websocketRef.current = websocket;

        websocket.onopen = () => setConnectionState('connected');
        websocket.onmessage = (message) => {
            if (typeof message.data !== 'string') return;
            try {
                const event = JSON.parse(message.data) as MeetingEvent;
                if (event.type === 'meeting_ready') {
                    void deleteFramesThrough(meetingId, event.ack)
                        .then(() => sendStoredFrames(event.ack + 1))
                        .then(refreshBufferedCount);
                } else if (event.type === 'audio_ack') {
                    void deleteFramesThrough(meetingId, event.ack).then(refreshBufferedCount);
                } else if (event.type === 'audio_nack') {
                    void sendStoredFrames(event.expected);
                }
                callbacksRef.current.onEvent(event);
            } catch (error) {
                callbacksRef.current.onError(`Invalid meeting event: ${String(error)}`);
            }
        };
        websocket.onerror = () => setConnectionState('error');
        websocket.onclose = () => {
            websocketRef.current = null;
            if (!activeRef.current) {
                setConnectionState('idle');
                return;
            }
            setConnectionState('reconnecting');
            reconnectTimerRef.current = window.setTimeout(() => connect(meetingId), 2000);
        };
    }, [refreshBufferedCount, sendStoredFrames]);

    const persistAndSend = useCallback((pcm: ArrayBuffer) => {
        const meetingId = meetingIdRef.current;
        if (!meetingId) return;
        const frame: StoredFrame = {
            meetingId,
            sequence: sequenceRef.current,
            pcm,
            createdAt: Date.now(),
        };
        sequenceRef.current += 1;
        writeQueueRef.current = writeQueueRef.current
            .then(async () => {
                await putFrame(frame);
                const websocket = websocketRef.current;
                if (websocket?.readyState === WebSocket.OPEN) websocket.send(packetFor(frame));
                await refreshBufferedCount();
            })
            .catch((error) => callbacksRef.current.onError(`Could not buffer meeting audio: ${String(error)}`));
    }, [refreshBufferedCount]);

    const startCapture = useCallback(async (meetingId: string) => {
        if (activeRef.current) return;
        activeRef.current = true;
        meetingIdRef.current = meetingId;
        const existingFrames = await listFrames(meetingId);
        sequenceRef.current = existingFrames.length
            ? Math.max(...existingFrames.map((frame) => frame.sequence)) + 1
            : 0;
        try {
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                    channelCount: 1,
                },
            });
            const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
            const context = new AudioContextClass({ latencyHint: 'interactive' });
            await context.audioWorklet.addModule('/meeting-audio-worklet.js');
            if (context.state === 'suspended') await context.resume();
            const source = context.createMediaStreamSource(stream);
            const worklet = new AudioWorkletNode(context, 'saksham-meeting-capture', {
                numberOfInputs: 1,
                numberOfOutputs: 1,
                outputChannelCount: [1],
            });
            const mute = context.createGain();
            mute.gain.value = 0;
            worklet.port.onmessage = (event) => {
                if (event.data?.type === 'pcm' && event.data.pcm instanceof ArrayBuffer) {
                    persistAndSend(event.data.pcm);
                }
            };
            source.connect(worklet);
            worklet.connect(mute);
            mute.connect(context.destination);
            streamRef.current = stream;
            contextRef.current = context;
            workletRef.current = worklet;
            connect(meetingId);
            setIsCapturing(true);
            playMeetingChime('start');
        } catch (error) {
            activeRef.current = false;
            meetingIdRef.current = null;
            callbacksRef.current.onError(`Microphone capture did not start: ${String(error)}`);
            throw error;
        }
    }, [connect, persistAndSend]);

    const stopCapture = useCallback((options: { serverAlreadyStopped?: boolean } = {}) => {
        if (stopInFlightRef.current) return stopInFlightRef.current;
        if (!activeRef.current) return Promise.resolve();

        const operation = (async () => {
            const meetingId = meetingIdRef.current;
            if (options.serverAlreadyStopped) {
                activeRef.current = false;
                streamRef.current?.getTracks().forEach((track) => track.stop());
                await writeQueueRef.current;
                if (meetingId) await deleteFramesThrough(meetingId, 0xffffffff);
            } else {
                workletRef.current?.port.postMessage({ type: 'flush' });
                await new Promise((resolve) => window.setTimeout(resolve, 250));
                await writeQueueRef.current;
                const deadline = Date.now() + 10000;
                while (meetingId && Date.now() < deadline) {
                    const pending = await listFrames(meetingId);
                    if (!pending.length) break;
                    if (websocketRef.current?.readyState === WebSocket.OPEN) {
                        await sendStoredFrames(pending[0].sequence);
                    }
                    await new Promise((resolve) => window.setTimeout(resolve, 250));
                }
                if (meetingId && (await listFrames(meetingId)).length) {
                    throw new Error('Waiting to upload buffered audio. Restore the backend connection, then stop again.');
                }
                if (websocketRef.current?.readyState === WebSocket.OPEN) {
                    websocketRef.current.send(JSON.stringify({ type: 'stop' }));
                }
                activeRef.current = false;
            }

            if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
            streamRef.current?.getTracks().forEach((track) => track.stop());
            workletRef.current?.disconnect();
            await contextRef.current?.close().catch(() => undefined);
            websocketRef.current?.close();
            streamRef.current = null;
            workletRef.current = null;
            contextRef.current = null;
            websocketRef.current = null;
            meetingIdRef.current = null;
            setIsCapturing(false);
            setConnectionState('idle');
            setBufferedFrames(0);
            setBufferWarning('');
            playMeetingChime('stop');
        })();
        stopInFlightRef.current = operation.finally(() => {
            stopInFlightRef.current = null;
        });
        return stopInFlightRef.current;
    }, [sendStoredFrames]);

    useEffect(() => () => {
        activeRef.current = false;
        if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
        streamRef.current?.getTracks().forEach((track) => track.stop());
        workletRef.current?.disconnect();
        void contextRef.current?.close();
        websocketRef.current?.close();
    }, []);

    useEffect(() => {
        if (!isCapturing) return;
        const warnBeforeUnload = (event: BeforeUnloadEvent) => {
            event.preventDefault();
            event.returnValue = '';
        };
        window.addEventListener('beforeunload', warnBeforeUnload);
        return () => window.removeEventListener('beforeunload', warnBeforeUnload);
    }, [isCapturing]);

    return {
        isCapturing,
        connectionState,
        bufferedFrames,
        bufferWarning,
        startCapture,
        stopCapture,
    };
}

export function pcmFramesToWav(frames: ArrayBuffer[], sampleRate = 16000): Blob {
    const byteLength = frames.reduce((total, frame) => total + frame.byteLength, 0);
    const output = new ArrayBuffer(44 + byteLength);
    const view = new DataView(output);
    const write = (offset: number, value: string) => {
        for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index));
    };
    write(0, 'RIFF');
    view.setUint32(4, 36 + byteLength, true);
    write(8, 'WAVE');
    write(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    write(36, 'data');
    view.setUint32(40, byteLength, true);
    const bytes = new Uint8Array(output, 44);
    let offset = 0;
    for (const frame of frames) {
        bytes.set(new Uint8Array(frame), offset);
        offset += frame.byteLength;
    }
    return new Blob([output], { type: 'audio/wav' });
}
