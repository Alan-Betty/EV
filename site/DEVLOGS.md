# stardance devlogs (after iteration 14)

---

## devlog 1: ev has a website now

vite + tailwind + plain js. no react, cause ev's whole thing is being lightweight and it'd be kinda hypocritical ngl

- nothing on it is made up. a script reads my git log before every build, so the commit list, the iteration number and the test count (950 btw) come straight from the repo
- the demo replays real ev errands with the real tool names. the amazon mouse one asks "Confirm?" before it touches the basket, same as the actual thing
- downloads are github zips pinned to the iteration commit since ev ships as source, plus a one liner to check which commit u got
- ctrl+k menu to jump around cause i'm lowk addicted to command palettes

works on phones too, tested it at like 5 screen sizes

---

## devlog 2: the face made it onto the site

the hero used to be a particle orb. it was mid. ev literally has a face so now the site uses that instead

- ported the qt renderer (`ev/face/render.py`) to a canvas and it reads the same `expressions.json` as the real face. change a mood in python and the site picks it up on the next build
- eyes follow ur cursor, it dozes off if u leave it alone for 15s and wakes back up when u move. click it and it gets startled
- big bug: it kept going "surprised" every few seconds and stopped following the mouse. turns out mouse events can be 1ms apart so a tiny wiggle read as 5000px/s 💀 now it measures over 100ms and only near the face
- glow is additive now so the eyes don't have that weird dark rim anymore

---

## devlog 3: less clutter + a manager page

the page had way too much going on fr. cut the face readouts, the demo stepper, extra stats and the giant verify box. it actually breathes now

- animations that mean something: headline rises in word by word, the face boots up with its eyes opening, sections fade in once, and the nav has a little pill that slides around. all of it switches off if u have reduced motion on
- made a site manager (`npm run admin`). edit any text, pick which commit stable points at, update the roadmap, all next to a live preview. saves as u type
- drag an installer onto a platform and it gets hashed on upload, so the page shows the real sha-256. no more copy pasting hashes by hand
- it only runs on localhost and never ships in the build, so nobody's editing my site but me

next up: actually putting it online lol
