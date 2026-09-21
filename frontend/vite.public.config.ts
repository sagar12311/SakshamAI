import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
    const env = { ...loadEnv(mode, process.cwd(), 'VITE_'), ...process.env };
    for (const name of ['VITE_SUPABASE_URL', 'VITE_SUPABASE_ANON_KEY', 'VITE_SAKSHAM_GATEWAY_URL']) {
        if (!env[name] || /replace-with|your-project/.test(env[name]!)) {
            throw new Error(`Public build requires a configured ${name}`);
        }
    }
    for (const name of ['VITE_SUPABASE_URL', 'VITE_SAKSHAM_GATEWAY_URL']) {
        const url = new URL(env[name]!);
        if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
            throw new Error(`${name} must be an HTTPS URL without credentials, query, or fragment`);
        }
    }
    const key = env.VITE_SUPABASE_ANON_KEY!;
    if (!key.startsWith('sb_publishable_')) {
        let role: unknown;
        try { role = JSON.parse(Buffer.from(key.split('.')[1], 'base64url').toString()).role; }
        catch { throw new Error('Use a Supabase publishable key or legacy anon key in the public build'); }
        if (role !== 'anon') throw new Error('A privileged Supabase key must never enter the public build');
    }
    return {
        plugins: [react(), {
            name: 'public-only-entry',
            transformIndexHtml: {
                order: 'pre' as const,
                handler: (html: string) => html
                    .replace('/src/main.tsx', '/src/public-main.tsx')
                    .replace(/<link[^>]+href="https:\/\/fonts\.(?:googleapis|gstatic)\.com[^\"]*"[^>]*>/g, '')
                    .replace('Your Personal Jarvis - Cognitive Intelligence System', 'Saksham public beta — chat and meeting transcription'),
            },
        }],
        publicDir: 'public-beta',
        build: { outDir: 'dist-public', sourcemap: false },
    };
});
