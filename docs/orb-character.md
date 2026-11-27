# The Orb character (Rive) - brief

The Orb is SponsorJobs' assistant: today a violet ball with a face drawn in SVG/CSS
(`ui/templates/orb.html`, `shell-electron/orb_view.js`). This brief is for rebuilding it as a
**Rive** character with a real range of motion, built through Rive's official MCP from Claude
Code (Rive desktop app open; `claude mcp add --transport http rive http://127.0.0.1:9791/mcp`).
The current SVG Orb stays as the fallback until the Rive file ships.

## Look
- A round violet body (#7957FC, the app's `--sj-pointer` token), soft inner glow, simple face:
  two big oval eyes with pupils, expressive brows, a small mouth. Readable at 56 px.
- Small hands that appear only when it holds or uses something (no permanent arms).
- Props: a **pen tucked behind its "ear"** (a little nub on the side of the head), **serious
  reading glasses**, a **sheet of paper**, a tiny **magnifying glass**.
- Flat vector style, two or three tones, no gradients except the glow (Rive refuses gradients
  outside a fill/stroke). Hex colours only.

## Personality
Helpful, a bit playful, never in the way. Clippy's lesson: people loved the face and hated
being interrupted. So: expressive when spoken to or when something happens; quiet otherwise.
Funny faces are short (under ~1.5 s) and never during a person's typing.

## States (Rive State Machine "Orb")
Inputs the app sends (view model / state machine inputs):
- `mode` (number enum): 0 idle, 1 thinking, 2 tailoring, 3 filling, 4 needsYou, 5 celebrate,
  6 sleeping, 7 offline, 8 talking
- `look_x`, `look_y` (numbers -1..1): where the eyes look (the pointer, the field, the person)
- `blink` (trigger), `wink` (trigger), `react` (trigger + `reaction` number: 0 surprised,
  1 proud, 2 oops, 3 curious, 4 thumbs-up)
- `askYesNo` (boolean): pose for a yes/no speech bubble
- `reducedMotion` (boolean): faces and glow only, no big moves

| State | What it does |
|---|---|
| idle | slow breathing glow, occasional blink and a glance around; every ~40 s a tiny fidget |
| thinking | eyes up, three dots orbiting, a finger tapping its "chin" |
| tailoring | **pulls the pen from behind its ear, puts on the serious glasses, holds the paper in one hand and writes with the other**, eyes following the pen; small nods; ends by holding the page up, pleased |
| filling | glasses on, leans toward the form, eyes track `look_x/look_y` |
| needsYou | eyes wide, brows up, a small wave, points toward the field |
| celebrate | a hop, arms up, sparkle burst; bigger version for milestones |
| sleeping | eyes closed, a slow "z" (quiet hours) |
| offline | greyed, eyes half closed |
| talking | mouth moves while the bubble text appears |
| askYesNo | leans in, brows up, one hand open toward the bubble |
| funny faces (react) | surprised (pop eyes), proud (chin up, smirk), oops (squint, sweat drop), curious (head tilt, one brow), thumbs-up |

Transitions are blended (no hard cuts). Every state can return to idle.

## The yes/no bubble
Quick questions appear as a Clippy-style speech bubble beside the Orb: one short line plus
**Yes** / **No** (or two short labels). The bubble is the app's HTML (not part of the Rive
file); the Rive character only poses (`askYesNo`).

## Files and budget
- One `.riv` file, one artboard "Orb" 256x256, the state machine "Orb".
- Target under ~150 KB.
- Runtime: `@rive-app/canvas` (MIT) in the Orb's own view, driven from the existing Orb states.
- Export: the free Rive plan plays a splash screen on exports; shipping needs the Cadet plan
  (about $9/seat/month, confirm on rive.app/pricing; owner approves before paying).

## Later
The sign-in / first-run screen can show the character in motion on the left half (like the
Rive desktop app's login), reusing the same file.
