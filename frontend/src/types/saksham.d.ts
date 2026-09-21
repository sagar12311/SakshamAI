export {};

declare global {
    interface Window {
        saksham?: {
            getMode: () => Promise<string>;
            setMode: (mode: string) => Promise<{ success: boolean; mode: string }>;
            sendMessage: (message: string) => Promise<unknown>;
            startVoice: () => Promise<{ recording: boolean }>;
            stopVoice: () => Promise<{ recording: boolean }>;
            minimize: () => void;
            maximize: () => void;
            close: () => void;
            onModeChange: (callback: (mode: string) => void) => void;
            onResponse: (callback: (response: unknown) => void) => void;
            onVoiceStatus: (callback: (status: unknown) => void) => void;
        };
    }
}
