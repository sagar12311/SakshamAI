/**
 * Saksham AI - Electron Main Process
 * Handles window management, tray, and IPC
 */

import {
    app,
    BrowserWindow,
    Tray,
    Menu,
    ipcMain,
    globalShortcut,
    nativeImage,
    systemPreferences,
    type WebContents,
} from 'electron';
import path from 'path';
import { fileURLToPath } from 'url';

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;

const isDev = process.env.NODE_ENV === 'development' || !app.isPackaged;
const backendBaseUrl = process.env.SAKSHAM_BACKEND_URL || 'http://127.0.0.1:8420';
const trustedDevRendererOrigins = new Set([
    'http://localhost:5173',
    'http://127.0.0.1:5173',
]);

let isQuitting = false;

async function backendRequest<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${backendBaseUrl}${path}`, init);
    if (!response.ok) {
        throw new Error(`Backend request failed: ${response.status}`);
    }
    return response.json() as Promise<T>;
}

/**
 * Only the bundled renderer (or the local Vite server while developing) is
 * allowed to use privileged Electron capabilities. This deliberately rejects
 * other localhost ports and every network origin in packaged builds.
 */
function isTrustedSakshamRendererUrl(candidateUrl: string): boolean {
    try {
        const parsedUrl = new URL(candidateUrl);

        if (isDev) {
            return trustedDevRendererOrigins.has(parsedUrl.origin);
        }

        if (parsedUrl.protocol !== 'file:') {
            return false;
        }

        const bundledIndex = path.resolve(__dirname, '../dist/index.html');
        return path.resolve(fileURLToPath(parsedUrl)) === bundledIndex;
    } catch {
        return false;
    }
}

function isTrustedSakshamRenderer(webContents: WebContents | null, requestedUrl?: string): boolean {
    if (!mainWindow || webContents !== mainWindow.webContents) {
        return false;
    }

    return isTrustedSakshamRendererUrl(requestedUrl || webContents.getURL());
}

function configureRendererSecurity(window: BrowserWindow): void {
    const { webContents } = window;
    const { session } = webContents;

    // Only allow an audio request made by Saksham's top-level renderer. In
    // particular, camera/video, screen capture, notifications, clipboard and
    // every other Electron permission are denied by default.
    session.setPermissionRequestHandler((requestingContents, permission, callback, details) => {
        const isAudioRequest = details.mediaTypes?.includes('audio') === true
            && !details.mediaTypes.includes('video');
        const allowed = permission === 'media'
            && details.isMainFrame
            && isAudioRequest
            && isTrustedSakshamRenderer(requestingContents, details.requestingUrl);

        callback(allowed);
    });

    // Electron requires a check handler as well as a request handler for
    // complete permission control. `unknown` is permitted here only until the
    // request handler confirms that the actual request is audio-only.
    session.setPermissionCheckHandler((requestingContents, permission, _requestingOrigin, details) => {
        return permission === 'media'
            && details.isMainFrame
            && details.mediaType !== 'video'
            && isTrustedSakshamRenderer(requestingContents, details.requestingUrl);
    });

    // Prevent a compromised renderer or an arbitrary link from replacing the
    // trusted app with remote content. Saksham currently has no use for popup
    // windows, so all new-window requests are denied.
    webContents.on('will-navigate', (event, navigationUrl) => {
        if (!isTrustedSakshamRendererUrl(navigationUrl)) {
            event.preventDefault();
            console.warn('Blocked renderer navigation to an untrusted URL');
        }
    });

    webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 800,
        height: 600,
        minWidth: 400,
        minHeight: 300,
        frame: false,
        titleBarStyle: 'hiddenInset',
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            preload: path.join(__dirname, 'preload.js'),
            // The current preload reads process.env directly, so sandboxing
            // requires a small IPC/config migration before it can be enabled.
            sandbox: false,
            webSecurity: true,
            allowRunningInsecureContent: false,
            webviewTag: false,
        },
    });

    configureRendererSecurity(mainWindow);

    // Handle renderer crashes
    mainWindow.webContents.on('render-process-gone', (event, details) => {
        console.error('Renderer process gone:', details.reason, details.exitCode);
    });

    mainWindow.webContents.on('did-fail-load', (event, errorCode, errorDescription, validatedURL) => {
        console.error('Failed to load:', validatedURL, errorCode, errorDescription);
    });

    // Load the app with a slight delay
    const loadApp = () => {
        if (isDev) {
            console.log('Attempting to load Vite at http://localhost:5173...');
            mainWindow?.loadURL('http://localhost:5173').catch((err) => {
                console.log('Failed to load Vite, retrying in 2s...');
                setTimeout(loadApp, 2000);
            });
        } else {
            mainWindow?.loadFile(path.join(__dirname, '../dist/index.html'));
        }
    };

    // Give Vite a moment to start up properly
    setTimeout(loadApp, 2000);

    // Show window when ready
    mainWindow.once('ready-to-show', () => {
        console.log('Window ready to show');
        mainWindow?.show();
    });

    // Handle window close
    mainWindow.on('close', (event) => {
        if (!isQuitting) {
            event.preventDefault();
            mainWindow?.hide();
        }
    });
}

function createTray() {
    // Create a simple icon
    const icon = nativeImage.createFromDataURL(
        'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
    );

    tray = new Tray(icon);

    const contextMenu = Menu.buildFromTemplate([
        {
            label: 'Open Saksham',
            click: () => {
                mainWindow?.show();
                mainWindow?.focus();
            },
        },
        { type: 'separator' },
        {
            label: 'Quit Saksham',
            click: () => {
                isQuitting = true;
                app.quit();
            },
        },
    ]);

    tray.setToolTip('Saksham AI');
    tray.setContextMenu(contextMenu);

    tray.on('click', () => {
        if (mainWindow?.isVisible()) {
            mainWindow.hide();
        } else {
            mainWindow?.show();
            mainWindow?.focus();
        }
    });
}

function registerShortcuts() {
    globalShortcut.register('CommandOrControl+Shift+S', () => {
        if (mainWindow?.isVisible()) {
            mainWindow.hide();
        } else {
            mainWindow?.show();
            mainWindow?.focus();
        }
    });
}

function registerIpcHandlers() {
    ipcMain.handle('get-mode', async () => {
        const data = await backendRequest<{ mode: string }>('/api/modes/current');
        return data.mode;
    });

    ipcMain.handle('set-mode', async (_event, mode: string) => {
        const data = await backendRequest<{ mode: string }>('/api/modes/switch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode }),
        });

        mainWindow?.webContents.send('mode-changed', data.mode);
        return { success: true, mode: data.mode };
    });

    ipcMain.handle('send-message', async (_event, message: string) => {
        return backendRequest('/api/chat/message', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message }),
        });
    });

    ipcMain.handle('start-voice', async () => ({ recording: false }));
    ipcMain.handle('stop-voice', async () => ({ recording: false }));

    ipcMain.on('window-minimize', () => {
        mainWindow?.minimize();
    });

    ipcMain.on('window-maximize', () => {
        if (!mainWindow) {
            return;
        }

        if (mainWindow.isMaximized()) {
            mainWindow.unmaximize();
        } else {
            mainWindow.maximize();
        }
    });

    ipcMain.on('window-close', () => {
        mainWindow?.hide();
    });
}

// App lifecycle
app.whenReady().then(async () => {
    // Request microphone permission on macOS
    if (process.platform === 'darwin') {
        const micStatus = systemPreferences.getMediaAccessStatus('microphone');
        console.log('Microphone permission status:', micStatus);

        if (micStatus !== 'granted') {
            console.log('Requesting microphone permission...');
            const granted = await systemPreferences.askForMediaAccess('microphone');
            console.log('Microphone permission granted:', granted);
        }
    }

    createWindow();
    createTray();
    registerShortcuts();
    registerIpcHandlers();

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

app.on('before-quit', () => {
    isQuitting = true;
});

app.on('will-quit', () => {
    globalShortcut.unregisterAll();
});
