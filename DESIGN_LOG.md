# Design log

Styling only. No elements, data fields, charts, interactions, API calls or state logic were added, removed, renamed or reordered.
The only markup additions are the legend (`.key`, explicitly allowed by the brief), `title` tooltips, and `data-*` attributes used as CSS hooks.

## Iter 1: design tokens + font setup
- Centralised colour, spacing (4px scale), radius, depth, motion, type scale into `:root` tokens; light theme overrides in `[data-theme="light"]`.
- Added three concept colour families (`--occ`, `--evi`, `--act`) each with soft/line variants. success/warning/danger stay reserved for system status.
- Fonts: Space Grotesk (display + labels), IBM Plex Sans (body/UI), IBM Plex Mono (numbers, tabular). Embedded as base64 woff2 so they work offline at the venue. Replaced the Google Fonts link.
- All palette pairs checked against WCAG AA (min 4.5:1) on dark and light surfaces.

## Iter 2: typography applied
- Every hard-coded font size moved onto the type scale (11/12/13/14/15/17/20/26/36 + hero + number size).
- Display face on hero state, headings, tabs, brand, table headers, eyebrow labels (600 weight, tracked caps). Numbers: Plex Mono 500, tabular-nums.
- Chart/axis text 10px -> 11px for legibility.

## Iter 3: occupancy / evidence / activity separation
- Occupancy (violet): solid pill when occupied, hollow ring when vacant, dashed ring when uncertain. Used on the hero marker + "Occupancy · the conclusion" pill, confirm timers, timeline state band, room tint, event dots, table state badges.
- Evidence (sky): outlined chips with a sensor icon on "Occupancy evidence" and "Sound level" rows, evidence track + sound bars, evidence line in the chart, floor dots in the room drawing.
- Activity (pink): bars glyph, activity sparkline, activity bars in the chart, mic rings in the room drawing.
- Added a three-line legend at the top of the readout and tooltips defining each term. Renamed the group header "Occupancy · from sound" to "From the microphone" because it held all three concepts. Rows are separated by coloured left rails and extra spacing.
- No colour is shared between the three concepts or with success/warning/danger.

## Iter 4: layout rhythm, depth, chart gutter
- Section, row and block spacing moved onto the 4px scale (`--s-*`). Group eyebrows lifted to `--fg-2` for hierarchy.
- Depth only where something floats: toasts, modal and the story band get a soft lift. The page itself stays flat, with hairline rules and no boxes (you asked for no soft-cornered boxes).
- Chart left gutter widened (46 -> 58px) so the "EVIDENCE" axis label no longer runs under the bars. Activity bars softened to 40% so they do not overpower the evidence line.

## Iter 5: numbers, tables, charts
- Tabular numerals on tables, timestamps, status and hover readout. Table numbers medium weight, row padding on the scale, units styled as quiet suffixes, tick marks easier to see.
- Legend uses the display face and now includes Occupancy.

## Iter 6: states
- Pressed states for buttons, consistent focus ring token on inputs, visible focus-visible outlines on tabs, segmented controls and toast close.
- Row hover tints with the row's own concept colour. Dashed empty-events rule, balanced headings (no orphan words), reduced-motion safety net, loading state dims the legend and badges.
- Checked: dark, light, mobile (390px), pencil mode, Customise modal, empty state, demo story. No console errors. CSV, shift report, chart window buttons and hover readout all still work.

## Final audit
- Same element ids before and after (none removed or renamed). Only additions: the legend block, tooltips, `data-*` hooks.
- Script changes are colour choices and `data-state` / `data-kind` / `data-occ` attributes only (41 lines), plus the chart gutter constant.
- Not done because it would change elements: rename the "Occupancy evidence" label (it says "occupancy" but is evidence), wrap each concept's rows in real section containers, colour the thermal readings as an evidence family.

## Fix: chart data spilling over the axis labels
- The evidence line, activity bars and temperature lines include a few seconds of data from before the chart window starts, so they were drawn left of the plot area and over the axis labels (worst in pencil mode). Drawing is now clipped to the plot area.

## Iteration 8: calmer palette, three languages

- **Palette:** the saturated violet, sky and pink were replaced with muted tones (dusty indigo for occupancy, sand for evidence, dusty rose for activity) and calmer success/warning/danger colours. Shapes and the legend still keep occupancy, evidence and activity apart, so the colour families did not have to stay loud.
- **Languages:** English, Hindi (हिन्दी) and Punjabi (ਪੰਜਾਬੀ). The EN / हि / ਪੰ switch in the header changes the page instantly and tells the backend, so phone alerts, rule-based advice, shift reports and AI replies use the same language.
- **How:** the English text is the lookup key (`tr()`), static markup is translated by a walker that remembers the English original, and dynamic strings call `tr()` directly. Letter-spacing is switched off and line-height raised for Devanagari and Gurmukhi; Noto Sans fonts are embedded.
- **Checked:** desktop and mobile in all three languages, Customise dialog, demo story, shift report; an audit script confirms no `tr()` key is missing from the dictionary.
- **Open:** a native speaker should proofread the Hindi and Punjabi text before the demo.
