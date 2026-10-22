# E.V. site

Landing page and install guide for E.V., the voice assistant in the parent folder. Vite, vanilla ES modules, Tailwind 3.

```bash
npm install
npm run dev       # http://localhost:5173
npm run build     # static output in dist/
```

Repo facts (iteration, commits, test counts) are read from git by
`scripts/sync-repo.mjs` into `src/data/repo.json` before every dev and build
run. Install steps per platform live in `src/data/release.js`.
