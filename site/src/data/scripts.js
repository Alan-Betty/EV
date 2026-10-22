// Simulator scripts. Each replays one errand through E.V.'s real tools, with
// the arguments those tools really take: what was heard, the plan, an optional
// spoken confirmation, the tool calls, and the spoken reply.
// `ms` on a step is the duration shown next to it once it finishes.

export const SCRIPTS = [
  {
    id: 'error',
    label: 'Read the error on screen & hand it to Claude Code',
    heard: 'E.V., what does that error say? Get Claude Code to fix it.',
    plan: 'take_screenshot to read the error, then dev_workflow to start Claude Code with it',
    steps: [
      { cmd: 'take_screenshot question="what does the error say"', ms: 1840, out: ['frame 1280px  95 KB  sent to Gemini vision', 'TypeError: cannot unpack non-iterable NoneType  (router.py:218)'] },
      { cmd: 'dev_workflow directory="~/GitHub/api" start_claude=true prompt="Fix the TypeError at router.py:218"', ms: 2310, out: ['VS Code focused, typing into its integrated terminal', 'claude started with the opening prompt'], tone: 'ok' },
    ],
    reply: "It's a TypeError on line 218 of router.py. route() returned None. Claude Code's open in your terminal with the fix request.",
  },
  {
    id: 'mouse',
    label: 'Find a gaming mouse under ₹5,000 & add it to my basket',
    heard: 'Find me a gaming mouse under five thousand with an infinite scroll wheel and put it in my Amazon basket.',
    plan: 'agent_task, routed to the browser: search, sort, pick, add once, check the basket',
    confirm: 'That adds something to your Amazon basket. Confirm?',
    steps: [
      { cmd: 'agent_task task="gaming mouse under 5000, infinite scroll, add to basket"', ms: 610, out: ['route: web  amazon.in  DOM planner, no screenshots'] },
      { cmd: 'web_agent fill 3 = "gaming mouse infinite scroll"', ms: 1420, out: ['48 results, 45 elements numbered'] },
      { cmd: 'web_agent select 14 = "Price: Low to High"', ms: 1180, out: ['[12] link "Logitech G502 X Lightspeed"  ₹4,295'] },
      { cmd: 'web_agent click 13 "Add to basket"', ms: 960, out: ['basket 0 -> 1', 'ALREADY DONE: Add to basket on /dp/B0C8'] },
      { cmd: 'web_agent look "is the mouse in the basket?"', ms: 2040, out: ['yes: Logitech G502 X Lightspeed, qty 1'], tone: 'ok' },
    ],
    reply: "Done. The Logitech G502 X at four thousand two ninety-five is in your basket, infinite scroll wheel and all. I didn't check out.",
  },
  {
    id: 'downloads',
    label: 'Tidy up my Downloads folder',
    heard: 'Tidy up my downloads folder.',
    plan: 'file_manager organize, inside FILE_ROOTS, files only, nothing deleted',
    steps: [
      { cmd: 'file_manager action=organize path="~/Downloads"', ms: 1290, out: ['41 files sorted into 7 folders', 'Images 18  Documents 9  Archives 6  Installers 4  Audio 2  Video 1  Other 1'], tone: 'ok' },
    ],
    reply: 'Forty-one files, seven folders. Downloads is civilised again.',
  },
  {
    id: 'system',
    label: 'Run a quick system check (RAM, swap, CPU)',
    heard: 'How is my system doing? Memory, swap and CPU.',
    plan: 'terminal_command, read-only commands, classified SAFE so no confirmation',
    steps: [
      { cmd: 'terminal_command command="free -h"', ms: 12, out: ['Mem:   31Gi total   11Gi used   18Gi available', 'Swap:  8.0Gi total   0B used'] },
      { cmd: 'terminal_command command="cat /proc/pressure/cpu"', ms: 9, out: ['some avg10=1.42 avg60=0.97 avg300=0.88'] },
      { cmd: 'terminal_command command="ps -eo comm,%cpu,rss --sort=-%cpu | head -4"', ms: 31, out: ['chromium   18.2   1.4G', 'code        6.9   812M', 'python3     1.1   118M'], tone: 'ok' },
    ],
    reply: "All healthy. Eighteen gigs free, swap untouched, CPU barely working. Chromium's the heaviest thing running. I'm the 118 megabytes at the bottom.",
  },
];

