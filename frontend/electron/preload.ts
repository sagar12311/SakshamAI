/**
 * Saksham AI - Preload Script
 * Secure bridge between Electron and React
 */

import { contextBridge, ipcRenderer } from 'electron';

// Expose secure API to renderer
contextBridge.exposeInMainWorld('saksham', {
    backendBaseUrl: process.env.SAKSHAM_BACKEND_URL || 'http://127.0.0.1:8420',

    // Mode management
    getMode: () => ipcRenderer.invoke('get-mode'),
    setMode: (mode: string) => ipcRenderer.invoke('set-mode', mode),

    // Chat
    sendMessage: (message: string) => ipcRenderer.invoke('send-message', message),

    // Voice
    startVoice: () => ipcRenderer.invoke('start-voice'),
    stopVoice: () => ipcRenderer.invoke('stop-voice'),

    // Window
    minimize: () => ipcRenderer.send('window-minimize'),
    maximize: () => ipcRenderer.send('window-maximize'),
    close: () => ipcRenderer.send('window-close'),

    // Events
    onModeChange: (callback: (mode: string) => void) => {
        ipcRenderer.on('mode-changed', (_, mode) => callback(mode));
    },
    onResponse: (callback: (response: any) => void) => {
        ipcRenderer.on('response', (_, response) => callback(response));
    },
    onVoiceStatus: (callback: (status: any) => void) => {
        ipcRenderer.on('voice-status', (_, status) => callback(status));
    },
});

// Type declarations for the exposed API
declare global {
    interface Window {
        saksham: {
            backendBaseUrl: string;
            getMode: () => Promise<string>;
            setMode: (mode: string) => Promise<{ success: boolean; mode: string }>;
            sendMessage: (message: string) => Promise<any>;
            startVoice: () => Promise<{ recording: boolean }>;
            stopVoice: () => Promise<{ recording: boolean }>;
            minimize: () => void;
            maximize: () => void;
            close: () => void;
            onModeChange: (callback: (mode: string) => void) => void;
            onResponse: (callback: (response: any) => void) => void;
            onVoiceStatus: (callback: (status: any) => void) => void;
        };
    }
}
