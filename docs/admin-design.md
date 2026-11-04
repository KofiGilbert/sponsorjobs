# Admin dashboard design rules

How admin.sponsorjobs.ai must look and why. Read this before changing anything the admin shows.
What the pages do (data, actions, deploy) is in `docs/admin-dashboard.md`. The code is
`worker/src/admin_ui.ts` (look and components), `admin_pages.ts` (pages), `admin_icons.ts` (icons).

## 1. The source of truth is the Figma file

The design is ByeWind's free **Dashboard UI Kit** (SnowUI), Figma Community, licence CC BY 4.0:
<https://www.figma.com/community/file/1210542873091115123>. The credit line in the page footer is
required by the licence; keep it.

- **Copy it exactly.** Same layout, block sizes, fonts, colours, charts, headings and controls.
  "Exactly" was the owner's explicit requirement after two rounds of corrections. Do not draw your
  own version of a chart, do not rename a heading to make it "mean more", do not drop a control.
- **Read the file, not a picture of it.** The values below came from the file itself (frames
  `549:8646` light and `54869:11584` dark) through Figma's plugin API. Section 7 says how.
- **Where the kit shows data we do not have, keep the element and give it a real job.** Example:
  "Traffic by Location" became "Users by Plan"; the kit's Favorites tab became starred pages.
- **Every control works.** A button that does nothing is not allowed. With no JavaScript, controls
  are links (`/prefs?...` sets a cookie and returns) or forms. The one script is the ⌘K shortcut,
  allowed by its hash in the CSP.

## 2. Measurements (1440 wide frame)

| Element | Value |
|---|---|
| Left sidebar | 212 wide, padding 16, right border 1px at 10% |
| Header | 68 high, padding 20 28, bottom border 1px at 10% |
| Right sidebar | 280 wide, padding 16, left border 1px at 10% |
| Content | padding 28; grid of 4 equal columns, gap 28 |
| Page title row | 28 high: title Semibold 14 (padding 4 8) left, a small control right; the grid starts 16 below |
| KPI card | 202x112, radius 20, padding 24, gap 8; fills alternate #edeefc / #e6f1fd (same in dark, black text) |
| Block | radius 20, padding 24, gap 16; #f9f9fa (dark: white at 4%); title Semibold 14 |
| Sidebar row | 36 high, padding 8, radius 12, rows 4 apart; active row #000 at 4% (dark white at 10%) |
| Sidebar row parts | slot 16 (dot or chevron), gap 4, icon 20, gap 8, text (text at x 72) |
| Group label | 28 high, padding 4 12, Regular 14 at 40% |
| Favorites / Recently tabs | 12/16, 40% (selected) and 20%, padding 4 12 |
| Search | 160x28, radius 16, #000 at 4%, a ⌘K key at the right |
| Header icon button | 24x24, padding 4, gap 8 |
| Table | no rules; header 40 high (Regular 12/16 at 40%); rows 52 high, padding 12 16, odd rows #000 at 2% |
| Right sidebar list | title Regular 14 (padding 8 4); item padding 8, gap 8; tile or avatar 24 |

## 3. Type and colour

- **Font:** Inter (Google Fonts), weights 400 and 600 only.
- **Styles:** Regular 14/20 (body), Regular 12/16 (labels, axes, small text), Semibold 14/20
  (titles), Semibold 24/36 (KPI numbers).
- **Text:** #000 at 100% (main), 40% (secondary), 20% (faint). Lines are #000 at 10%.
- **Dark mode:** background #333. Text is #fff at 100% / 40% / 20%, lines #fff at 15%.
- **Chart palette, in this order:** #a0bce8, #6be6d3, #000 (dark: #fff at 40%), #33d1ff, #b899eb,
  #71dd8c.
- **Status:** a 6px dot plus a word (green #71dd8c, yellow #ffc555, red #ff4747). Never coloured
  text, never filled pills.
- **Icons:** Phosphor (MIT). Duotone 20 in the sidebar, Duotone 16 in the header, Regular 16
  elsewhere. Fill for the active Overview icon. The duotone shading is **8%** (the kit's value, not
  Phosphor's 20%).

## 4. The kit's blocks and how we use them

| Kit block | Its exact build | Ours |
|---|---|---|
| KPI cards (4) | label Regular 14, number Semibold 24, change "+11.01%" 12px with the kit's arrow | the range's numbers, each against the range before |
| "Total Users" chart (span 3) | tabs (Semibold for the open one, 40% for the others), a "\|" at 20%, legend dots; y labels in four 62-high rows (0 and three steps); month labels in equal columns; a monotone line fading 40% to 100% over a soft radial shading; the comparison line #a0bce8, dashed 2 4, round caps; 1.5px | Total Users / Revenue / AI Cost by month, this year and last year |
| "Traffic by Website" (span 1) | label 57 wide, gap 16, three 2px segments (100%, 40%, 10%) with gaps of 2; rows 43 apart | Top AI Users, E-mail Domains |
| "Traffic by Device" (span 2) | y labels in four 49-high rows; one bar per column, 28 wide, top radius 8, palette colours | Costs This Month, a person's AI Use |
| "Traffic by Location" (span 2) | a 120 ring (outer 60, inner 30), rounded gapped segments clockwise from the top (black, #33d1ff, #71dd8c, #a0bce8); legend rows 32 apart, 12px dots, share on the right | Users by Plan, Products |
| "Marketing & SEO" (span 4) | the device bars across the full width, #a0bce8 | Free AI per day |
| Notifications | a 24 tile, radius 8, #edeefc / #e6f1fd alternating, a black icon; a line and a time | what needs attention (titled "Notifications", exactly) |
| Activities | round 24 avatars joined by a 1px line | sign-ups, sign-ins, purchases, admin actions |
| Contacts | round 24 avatar and a name | recent e-mail sign-ins, masked |

## 5. Words

- **As few as the kit uses.** A block has a title and data, with no explanatory sentences.
  Explanations belong in this file and in `docs/admin-dashboard.md`, not on screen.
- **Titles in the kit's casing:** "Top AI Users", "Costs This Month", "Days Left".
- **Labels are one or two words.** A KPI value must fit 202 wide at 24px, so write "Job Hunt", not
  "Job Hunt Pass".
- **Headings the kit has are kept word for word.** For example "Notifications", "Activities",
  "Contacts", "Favorites", "Recently".
- **One action at a time.** Several forms are shown behind the kit's tabs (as on a person's page),
  never stacked.
- **Facts are a short list in the legend style** (12px label at 40%, value on the right), six rows
  at most. Longer detail goes in a table.
- **No em or en dashes. "Resume", not "CV".**

## 6. Privacy on screen

- **E-mails are masked** (`ka***@gmail.com`) everywhere: lists, the right sidebar, the "Recently"
  cookie. The application alias is masked too, because it carries the person's name.
- **"Show full e-mail" is a POST, and it is logged** in the audit log.
- **Never show** a token, a hash, a code, a forwarded mail's code or link, or anything a person
  wrote or uploaded.

## 7. How to check a change against the kit

1. **Open the kit file** in a signed-in Chrome. In this session that is `Kofi Gilbert's team`
   Drafts, "Dashboard UI Kit ...". The page exposes Figma's plugin API as `figma`, so no token is
   needed. Never create one.
2. **Read values** with `figma.getNodeByIdAsync(id)` and `findAll`. Read fills, strokes,
   `cornerRadius`, `layoutMode`, `itemSpacing`, padding, and `fontName` / `fontSize` /
   `lineHeight`. Tool output is cut off at about 1000 characters: build a compact string in a
   `window` variable and read it in slices.
3. **Get a reference picture.** `node.exportAsync({ format: "PNG" })`, shown full size in an
   overlay and screenshotted. Do not fetch to `127.0.0.1`: Chrome blocks it.
4. **Render ours** on seeded data at 1440x1024, light and dark (Playwright), and put the two side
   by side. Fix every difference you can name. Then show the owner the side-by-side page before
   asking to merge.
5. **Run the worker tests** (`npx vitest run`) and `npx tsc --noEmit -p .`.
