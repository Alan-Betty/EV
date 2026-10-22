# NEXUS site

Landing page and download portal. Vite, vanilla ES modules, Tailwind 3.

```bash
npm install
npm run dev       # http://localhost:5173
npm run build     # static output in dist/
```

Release data (versions, file names, sizes, checksums, counters) lives in
`src/data/release.js`. The checksums there are placeholders: point CI at that
file, or replace it with a fetched manifest, before publishing.
