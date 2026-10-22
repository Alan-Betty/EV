// The site manager's backend: a Vite plugin that only exists while the dev
// server runs (`npm run admin`). The built site is static and has no idea it
// exists. It reads and writes content/site.json, re-reads git, takes download
// uploads into public/downloads (hashing them on the way in), and runs a build.
//
// It listens on the dev server, which binds to localhost, and refuses any
// request that is not from the manager page itself: a custom header forces a
// CORS preflight that a foreign page cannot pass, and the Origin must match.
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createWriteStream, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SITE = join(dirname(fileURLToPath(import.meta.url)), '..');
const CONTENT = join(SITE, 'content', 'site.json');
const REPO_JSON = join(SITE, 'src', 'data', 'repo.json');
const DOWNLOADS = join(SITE, 'public', 'downloads');
const MAX_UPLOAD = 2 * 1024 ** 3; // 2 GB

const PLATFORMS = ['linux', 'macos', 'windows'];
const SOURCES = ['github', 'file', 'url'];
const STATES = ['done', 'active', 'todo'];

function run(cmd, args) {
  return new Promise((resolve) => {
    execFile(cmd, args, { cwd: SITE, maxBuffer: 8 * 1024 * 1024 }, (err, stdout, stderr) =>
      resolve({ ok: !err, output: `${stdout}${stderr}`.trim() }),
    );
  });
}

// Only what the page can use goes to disk; anything malformed is a 400 with a reason.
function validate(c) {
  const fail = (msg) => {
    throw Object.assign(new Error(msg), { status: 400 });
  };
  if (!c || typeof c !== 'object' || Array.isArray(c)) fail('content must be an object');
  for (const key of ['hero', 'release']) if (typeof c[key] !== 'object' || !c[key]) fail(`"${key}" is missing`);
  const pin = c.release.pin;
  if (pin !== 'auto' && !/^[0-9a-f]{7,40}$/.test(pin ?? '')) fail('release.pin must be "auto" or a commit hash');
  for (const p of PLATFORMS) {
    const d = c.release.downloads?.[p];
    if (!d || !SOURCES.includes(d.source)) fail(`release.downloads.${p}.source must be one of ${SOURCES.join(', ')}`);
    if (d.source === 'url' && d.url && !/^https?:\/\//.test(d.url)) fail(`the ${p} link must start with http:// or https://`);
    if (d.sha256 && !/^[0-9a-f]{64}$/i.test(d.sha256)) fail(`the ${p} SHA-256 must be 64 hex characters`);
    if (d.source === 'file' && d.file && !existsSync(join(DOWNLOADS, basename(d.file)))) fail(`${basename(d.file)} is not in public/downloads`);
  }
  if (!Array.isArray(c.roadmap)) fail('roadmap must be a list');
  c.roadmap.forEach((m, i) => {
    if (typeof m.label !== 'string') fail(`roadmap item ${i + 1} needs a label`);
    if (!STATES.includes(m.state)) fail(`roadmap item ${i + 1} has an unknown state`);
  });
  return c;
}

function writeAtomic(path, text) {
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, text);
  renameSync(tmp, path); // a crash mid-write never leaves half a JSON file
}

const safeName = (name) => basename(String(name || '')).replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[.-]+/, '').slice(0, 120);

function uploads() {
  if (!existsSync(DOWNLOADS)) return [];
  return readdirSync(DOWNLOADS)
    .filter((f) => !f.startsWith('.'))
    .map((f) => ({ name: f, size: statSync(join(DOWNLOADS, f)).size }));
}

async function state() {
  const git = await run('git', ['status', '--porcelain', '--', 'content', 'public/downloads']);
  return {
    content: JSON.parse(readFileSync(CONTENT, 'utf8')),
    repo: JSON.parse(readFileSync(REPO_JSON, 'utf8')),
    uploads: uploads(),
    uncommitted: git.ok ? git.output.split('\n').filter(Boolean).length : null,
  };
}

const readBody = (req) =>
  new Promise((resolve, reject) => {
    let data = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 2_000_000) reject(Object.assign(new Error('content is too large'), { status: 413 }));
    });
    req.on('end', () => resolve(data));
    req.on('error', reject);
  });

// Streams to disk and hashes in the same pass, so a 1 GB installer is never in memory.
const receiveFile = (req, name) =>
  new Promise((resolve, reject) => {
    mkdirSync(DOWNLOADS, { recursive: true });
    const final = join(DOWNLOADS, name);
    const tmp = `${final}.part`;
    const out = createWriteStream(tmp);
    const hash = createHash('sha256');
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_UPLOAD) {
        req.destroy();
        out.destroy();
        try { unlinkSync(tmp); } catch {}
        reject(Object.assign(new Error('files over 2 GB belong on a release page; use a link instead'), { status: 413 }));
        return;
      }
      hash.update(chunk);
    });
    req.pipe(out);
    out.on('finish', () => {
      renameSync(tmp, final);
      resolve({ file: name, size, sha256: hash.digest('hex') });
    });
    out.on('error', reject);
    req.on('error', reject);
  });

export function adminApi() {
  return {
    name: 'ev-site-admin',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use('/__admin', async (req, res) => {
        const send = (status, body) => {
          res.statusCode = status;
          res.setHeader('Content-Type', 'application/json');
          res.setHeader('Cache-Control', 'no-store');
          res.end(JSON.stringify(body));
        };
        try {
          const origin = req.headers.origin;
          if (req.headers['x-ev-admin'] !== '1' || (origin && origin !== `http://${req.headers.host}`)) {
            return send(403, { error: 'only the site manager page can call this' });
          }
          const url = new URL(req.url, 'http://local');
          const route = `${req.method} ${url.pathname}`;

          if (route === 'GET /state') return send(200, await state());

          if (route === 'PUT /content') {
            const content = validate(JSON.parse(await readBody(req)));
            writeAtomic(CONTENT, `${JSON.stringify(content, null, 2)}\n`);
            return send(200, { ok: true });
          }

          if (route === 'POST /sync') {
            const result = await run(process.execPath, ['scripts/sync-repo.mjs']);
            return send(result.ok ? 200 : 500, { ...result, repo: JSON.parse(readFileSync(REPO_JSON, 'utf8')) });
          }

          if (route === 'PUT /upload') {
            const name = safeName(url.searchParams.get('name'));
            if (!name) return send(400, { error: 'the file needs a name' });
            return send(200, await receiveFile(req, name));
          }

          if (route === 'DELETE /upload') {
            const name = safeName(url.searchParams.get('name'));
            const path = join(DOWNLOADS, name);
            if (name && existsSync(path)) unlinkSync(path);
            return send(200, { ok: true, uploads: uploads() });
          }

          if (route === 'POST /build') {
            await run(process.execPath, ['scripts/sync-repo.mjs']);
            const result = await run(process.execPath, ['node_modules/vite/bin/vite.js', 'build']);
            return send(result.ok ? 200 : 500, { ok: result.ok, output: result.output.split('\n').slice(-12).join('\n') });
          }

          return send(404, { error: `no route for ${route}` });
        } catch (err) {
          send(err.status ?? 500, { error: err.message });
        }
      });
    },
  };
}
