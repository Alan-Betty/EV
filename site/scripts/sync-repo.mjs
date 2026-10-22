// Reads E.V.'s own git history and test suite (the repo this site lives in)
// into src/data/repo.json, so the page states real numbers. Runs before dev
// and build. Without git or the parent repo it keeps the committed JSON.
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const out = join(here, '..', 'src', 'data', 'repo.json');

function git(...args) {
  return execFileSync('git', ['-C', root, ...args], { encoding: 'utf8' }).trim();
}

try {
  // E.V.'s own commits: anything that touched the project outside this site.
  const commits = git('log', '--format=%H%x09%ad%x09%s', '--date=short', '--', '.', ':(exclude)site')
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      const [hash, date, subject] = line.split('\t');
      return { hash, date, subject: subject.trim() };
    })
    .filter((c) => c.subject.replace(/[.\s]/g, '').length > 2);

  const iterationCommits = commits.filter((c) => /iteration/i.test(c.subject));
  const latest = iterationCommits[0] ?? commits[0];
  const number = Number(latest.subject.match(/(\d+)\s*(?:st|nd|rd|th|h)?\s+iteration/i)?.[1]) || iterationCommits.length;

  const testsDir = join(root, 'tests');
  const testFiles = readdirSync(testsDir)
    .filter((f) => /^test_.*\.py$/.test(f))
    .map((f) => ({ file: `tests/${f}`, tests: (readFileSync(join(testsDir, f), 'utf8').match(/^\s*(?:async\s+)?def test_/gm) ?? []).length }))
    .filter((t) => t.tests > 0)
    .sort((a, b) => b.tests - a.tests);

  const data = {
    repo: 'https://github.com/Alan-Betty/EV',
    head: commits[0],
    iteration: { number, ...latest },
    commitCount: commits.length,
    tests: testFiles.reduce((n, t) => n + t.tests, 0),
    testFiles,
    commits,
  };
  writeFileSync(out, `${JSON.stringify(data, null, 2)}\n`);
  console.log(`[sync-repo] iteration ${number} @ ${latest.hash.slice(0, 7)}, ${commits.length} commits, ${data.tests} tests`);
} catch (err) {
  if (!existsSync(out)) throw err;
  console.warn(`[sync-repo] kept existing repo.json (${err.message.split('\n')[0]})`);
}
