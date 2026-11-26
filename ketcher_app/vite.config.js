import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: './',
  plugins: [react()],
  define: { 'process.env': {}, global: 'globalThis' },
  build: { outDir: 'dist', chunkSizeWarningLimit: 50000, sourcemap: false, assetsInlineLimit: 0 },
});
