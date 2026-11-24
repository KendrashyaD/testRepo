import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 5173,
      proxy: {
        // Same-origin in dev; streamed responses pass through untouched
        '/api': { target: env.VITE_API_TARGET || 'http://localhost:8000', changeOrigin: true },
      },
    },
  };
});
