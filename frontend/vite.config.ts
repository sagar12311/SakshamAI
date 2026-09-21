import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const backendTarget = process.env.SAKSHAM_BACKEND_URL || 'http://localhost:8420'
const backendWebSocketTarget = backendTarget.replace(/^http/, 'ws')

export default defineConfig({
    plugins: [react()],
    resolve: {
        alias: {
            '@': path.resolve(__dirname, './src'),
        },
    },
    build: {
        outDir: 'dist',
    },
    server: {
        port: 5173,
        proxy: {
            '/api': {
                target: backendTarget,
                changeOrigin: true,
            },
            '/ws': {
                target: backendWebSocketTarget,
                ws: true,
            },
        },
    },
})
