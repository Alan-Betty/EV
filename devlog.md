# E.V. can drive my screen now

This update was all about letting E.V. actually take over the computer, not just open apps.

**What's new**
- It can run a whole errand by itself. I said "find me a gaming mouse with an infinite scroll wheel and add it to my amazon cart" and it searched, picked one and added it.
- Web stuff goes through Playwright and reads the page instead of taking screenshots. Screenshots were eating my Groq rate limit in like 4 steps.
- There's a red frame around the screen plus a little status panel whenever E.V. is in control, so you can tell it's doing things and it's not a virus lol
- Kill switch: ctrl+alt+q, or just say "stop everything". It locks E.V. until you tell it to unlock.

**Bugs I hit**
- It added the same mouse to my cart 4 times. Now it remembers what it already clicked and won't do it twice.
- It got stuck on Amazon because "Search" matched 4 things on the page and it clicked a hidden one. Now if a step fails it reads the page and figures out a different way.

Next up: getting it to use Brave instead of its own Chromium.
