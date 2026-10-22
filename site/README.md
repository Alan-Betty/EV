# E.V. site

Landing page and install guide for E.V., the voice assistant in the parent
folder. Vite, vanilla ES modules, Tailwind 3.

```bash
npm install
npm run admin     # site manager + live preview, opens in the browser
npm run dev       # just the site, http://localhost:5173
npm run build     # static output in dist/
```

## Changing what the site says

Run `npm run admin`. Everything saves as you type into
`content/site.json`, and the preview beside it updates live. Commit
`content/` and `public/downloads/` when you are happy with it.

What it does for you:

- **Iteration, commit, date, test and commit counts** are read from git
  every time the site starts or builds (`scripts/sync-repo.mjs` into
  `src/data/repo.json`). Make an "Nth Iteration" commit and Stable follows
  it on its own. "Re-read git" picks it up without a restart.
- **Downloads** default to GitHub's zip of the pinned commit. Drop a file
  on a platform to host it instead: it is copied into `public/downloads/`
  and its SHA-256 is computed on the way in, so the page shows the real
  checksum. Or paste a link to a release asset.
- **Text** anywhere on the page can use `{iteration}`, `{tests}` and
  `{commits}`, which fill themselves in.

The manager and its API only exist while the dev server runs. `npm run
build` ships the public page alone.
