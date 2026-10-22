// What the activity log streams after the real commit history: a replay of
// the kinds of events one E.V. session writes. The shapes come from the code
// (tool calls, the wake and echo checks, the guard), the values are examples.
import REPO from './repo.json';

export const POOLS = {
  tool: [
    ['open_app app="firefox"  launched in 0.4 s', 'ok'],
    ['file_manager organize ~/Downloads  41 files', 'ok'],
    ['media_control action=volume_down  65% -> 55%', 'ok'],
    ['web_search query="lofi beats" engine=youtube', 'ok'],
    ['app_control focus "Visual Studio Code"  window tier', 'ok'],
    ['take_screenshot question="what does that say"  95 KB', 'ok'],
    ['browser_task  amazon.in  3 planner calls, 0 frames', 'ok'],
    ['remember_fact key=browser value="Firefox"', 'ok'],
    ['manage_todo add "call the dentist"', 'ok'],
  ],
  voice: [
    ['wake matched "Eve" -> E.V.', 'info'],
    ['end of speech after 600 ms (full sentence)', 'info'],
    ['stt avg_logprob -0.31  accepted', 'ok'],
    ['stt avg_logprob -1.12  doubtful, told the model', 'warn'],
    ['barge-in: user above echo level, reply stopped', 'info'],
    ['self-echo dropped: 6 of 7 words were its own', 'warn'],
    ['engaged window open, no name needed', 'info'],
  ],
  guard: [
    ['Confirm? terminal_command "git push origin main"', 'warn'],
    ['refused /etc/hosts: outside FILE_ROOTS', 'warn'],
    ['blocked: curl ... | sh never runs', 'warn'],
    ['redacted 1 key-shaped value from a file read', 'ok'],
    ['fenced 2.1 KB of page text as untrusted', 'ok'],
    ['audit: 1 line appended to audit.jsonl', 'ok'],
  ],
  test: REPO.testFiles.slice(0, 18).map((t) => [`${t.file}  ${t.tests} passed`, 'ok']),
};
