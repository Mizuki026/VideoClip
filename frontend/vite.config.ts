import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

const apiTarget = process.env.VIDEOCLIP_API_TARGET ?? 'http://127.0.0.1:58002';
const webPort = Number(process.env.VIDEOCLIP_WEB_PORT ?? '5174');
const apiProxy = {
  '/api': {
    target: apiTarget,
    changeOrigin: false,
    rewrite: (path: string) => path.replace(/^\/api(?=\/|$)/, '') || '/',
  },
};

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: webPort,
    strictPort: true,
    allowedHosts: ['127.0.0.1'],
    proxy: apiProxy,
  },
  preview: {
    host: '127.0.0.1',
    port: webPort,
    strictPort: true,
    allowedHosts: ['127.0.0.1'],
    proxy: apiProxy,
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test-setup.ts',
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
  },
});
