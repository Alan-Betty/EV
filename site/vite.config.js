import { defineConfig } from 'vite';

// Relative base so `dist/` can be served from any path (GitHub Pages, a subfolder, file share).
export default defineConfig({
  base: './',
  build: { target: 'es2022', cssMinify: true },
});
