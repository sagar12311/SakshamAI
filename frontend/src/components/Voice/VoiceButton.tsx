/**
 * Saksham AI - Voice Button Component with Real Microphone Recording
 */

import { useState, useRef, useCallback } from 'react';
import { motion } from 'framer-motion';
import { Mic, MicOff } from 'lucide-react';
import './VoiceButton.css';

interface VoiceButtonProps {
    isListening: boolean;
    onStart: () => void;
    onEnd: () => void;
    onTranscription?: (text: string) => void;
}

export default function VoiceButton({ isListening, onStart, onEnd, onTranscription }: VoiceButtonProps) {
    const [permissionDenied, setPermissionDenied] = useState(false);
    const mediaRecorderRef = useRef<MediaRecorder | null>(null);
    const chunksRef = useRef<Blob[]>([]);

    const startRecording = useCallback(async () => {
        try {
            // Request microphone permission
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    sampleRate: 16000,
                }
            });

            setPermissionDenied(false);
            chunksRef.current = [];

            const mediaRecorder = new MediaRecorder(stream, {
                mimeType: 'audio/webm;codecs=opus'
            });

            mediaRecorder.ondataavailable = (event) => {
                if (event.data.size > 0) {
                    chunksRef.current.push(event.data);
                }
            };

            mediaRecorder.onstop = async () => {
                // Stop all tracks
                stream.getTracks().forEach(track => track.stop());

                // Create audio blob
                const audioBlob = new Blob(chunksRef.current, { type: 'audio/webm' });

                // Send to backend for transcription
                try {
                    const formData = new FormData();
                    formData.append('audio', audioBlob, 'recording.webm');

                    const response = await fetch('/api/chat/voice', {
                        method: 'POST',
                        body: formData,
                    });

                    if (response.ok) {
                        const data = await response.json();
                        if (data.transcription && onTranscription) {
                            onTranscription(data.transcription);
                        }
                    }
                } catch (error) {
                    console.error('Failed to send audio:', error);
                }
            };

            mediaRecorderRef.current = mediaRecorder;
            mediaRecorder.start(100); // Collect data every 100ms
            onStart();

        } catch (error) {
            console.error('Microphone access denied:', error);
            setPermissionDenied(true);
        }
    }, [onStart, onTranscription]);

    const stopRecording = useCallback(() => {
        if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
            mediaRecorderRef.current.stop();
        }
        onEnd();
    }, [onEnd]);

    const handleClick = () => {
        if (isListening) {
            stopRecording();
        } else {
            startRecording();
        }
    };

    return (
        <div className="voice-button-container">
            <motion.button
                className={`voice-button ${isListening ? 'listening' : ''} ${permissionDenied ? 'denied' : ''}`}
                onClick={handleClick}
                whileTap={{ scale: 0.95 }}
                aria-label={isListening ? 'Stop listening' : 'Start listening'}
            >
                {/* Ripple effect when listening */}
                {isListening && (
                    <>
                        <span className="ripple ripple-1" />
                        <span className="ripple ripple-2" />
                        <span className="ripple ripple-3" />
                    </>
                )}

                <span className="voice-button-icon">
                    {isListening ? <MicOff size={24} /> : <Mic size={24} />}
                </span>
            </motion.button>

            {permissionDenied && (
                <p className="permission-error">
                    Microphone access denied. Please allow microphone in browser settings.
                </p>
            )}
        </div>
    );
}
