// Local trial only: binds Vite to loopback and uses the public-beta entry point.
// The production Vite config retains its HTTPS and security-header checks.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
    plugins: [react(), {
        name: 'local-public-entry',
        transformIndexHtml: {
            order: 'pre' as const,
            handler: (html: string) => html
                .replace('/src/main.tsx', '/src/public-main.tsx')
                .replace(/<link[^>]+href="https:\/\/fonts\.(?:googleapis|gstatic)\.com[^\"]*"[^>]*>/g, ''),
        },
    }],
    publicDir: 'public-beta',
    server: { host: '127.0.0.1', port: 5173, strictPort: true },
});
