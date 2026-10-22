import { defineConfig } from 'vite';
import { adminApi } from './scripts/admin-api.mjs';

// Relative base so `dist/` can be served from any path (GitHub Pages, a subfolder, file share).
// Only index.html is built: admin.html and its API exist in the dev server alone.
export default defineConfig({
  base: './',
  build: { target: 'es2022', cssMinify: true },
  plugins: [adminApi()],
  // The face is drawn from ../ev/face/expressions.json, outside this folder.
  server: { fs: { allow: ['..'] } },
});
