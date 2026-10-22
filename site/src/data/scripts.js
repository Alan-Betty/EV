// Simulator scripts. Each is one recorded errand: what was heard, the plan,
// an optional spoken confirmation, the commands run, and the spoken reply.
// `ms` on a step is the duration shown next to it once it finishes.

export const SCRIPTS = [
  {
    id: 'refactor',
    label: 'Analyze screen & refactor current file',
    heard: 'Look at my screen and refactor the file I have open.',
    plan: 'capture focused window, read editor tree, plan edit, run tests',
    steps: [
      { cmd: 'nexus vision capture --window focused --ruler', ms: 182, out: ['frame 1600x1000  158 KB  "router.py - Visual Studio Code"'] },
      { cmd: 'nexus a11y tree --app code --depth 4', ms: 64, out: ['412 elements  focus: editor[router.py:218]'] },
      { cmd: 'nexus edit plan --file app/router.py --goal "flatten route()"', ms: 1340, out: ['3 hunks  -61 +19', 'nested if/elif chain -> dispatch table'] },
      { cmd: 'pytest tests/ -q', ms: 41800, out: ['752 passed in 41.80s'], tone: 'ok' },
    ],
    reply: 'Done. route() was a forty-line if chain, so I turned it into a dispatch table. Same behaviour, 42 lines shorter, and all 752 tests still pass.',
  },
  {
    id: 'diagnostic',
    label: 'Execute system diagnostic (RAM/Swap/CPU)',
    heard: 'Run a quick system diagnostic. Memory, swap and CPU.',
    plan: 'read memory, pressure stall info, top processes, thermals',
    steps: [
      { cmd: 'free -h --si', ms: 3, out: ['Mem:   32G total   11G used   18G available', 'Swap:  8.0G total   0B used'] },
      { cmd: 'cat /proc/pressure/cpu', ms: 1, out: ['some avg10=1.42 avg60=0.97 avg300=0.88'] },
      { cmd: 'ps -eo comm,%cpu,rss --sort=-%cpu | head -3', ms: 21, out: ['chromium      18.2   1.4G', 'code           6.9   812M', 'nexus          1.1    96M'] },
      { cmd: 'nexus diag thermals', ms: 140, out: ['cpu 54°C   gpu 47°C   fans 1180 rpm'], tone: 'ok' },
    ],
    reply: 'All healthy. Eighteen of thirty-two gigs free, swap untouched, CPU pressure under two percent. Chromium is the heaviest thing running at 1.4 gigs.',
  },
  {
    id: 'hyprland',
    label: 'Set up Hyprland workspace & launches',
    heard: 'Set up my dev workspace on three. Terminal left, editor right.',
    plan: 'switch workspace, launch kitty and code, set layout, wait for windows',
    steps: [
      { cmd: 'hyprctl dispatch workspace 3', ms: 2, out: ['ok'] },
      { cmd: 'hyprctl dispatch exec "[workspace 3 silent] kitty --class dev"', ms: 4, out: ['ok  pid 51822'] },
      { cmd: 'hyprctl dispatch exec "[workspace 3 silent] code ~/src/nexus"', ms: 5, out: ['ok  pid 51840'] },
      { cmd: 'hyprctl dispatch layoutmsg orientationleft', ms: 1, out: ['ok'] },
      { cmd: 'nexus ipc wait openwindow --class code --timeout 4s', ms: 812, out: ['openwindow>>5a1c,3,code,nexus - Visual Studio Code'], tone: 'ok' },
    ],
    reply: 'Workspace three is ready. Terminal on the left, editor on the right, both up in under a second.',
  },
  {
    id: 'release',
    label: 'Automate local git push & release draft',
    heard: 'Commit what I have, push it, and draft the release notes.',
    plan: 'inspect tree, commit, push, draft release from merged commits',
    confirm: 'That pushes to main and drafts v0.9.4. Confirm?',
    steps: [
      { cmd: 'git status --porcelain', ms: 9, out: [' M src/lib/orb.js', ' M src/sections/hero.js'] },
      { cmd: 'git commit -am "feat(hero): tune orb energy curve"', ms: 38, out: ['[main 4c1e9a2] 2 files changed, 14 insertions(+), 6 deletions(-)'] },
      { cmd: 'git push origin main', ms: 1210, out: ['a81f0d3..4c1e9a2  main -> main'] },
      { cmd: 'gh release create v0.9.4 --draft --generate-notes', ms: 960, out: ['draft created: releases/tag/v0.9.4  (23 commits, 4 contributors)'], tone: 'ok' },
    ],
    reply: 'Pushed two files to main and drafted v0.9.4 from twenty-three commits. It is still a draft, so nothing is public until you publish it.',
  },
];

// Anything typed into the demo that is not one of the errands above.
export function freeform(text) {
  return {
    id: 'freeform',
    heard: text,
    plan: 'route utterance, pick a tool subset',
    steps: [
      { cmd: `nexus route --utterance ${JSON.stringify(text.slice(0, 48))}`, ms: 212, out: ['offered 3 of 21 tools: chat, open_app, web_search'] },
    ],
    reply: 'In the app I would act on that. This demo only replays the four errands above, so pick one of those to watch the whole pipeline run.',
  };
}
