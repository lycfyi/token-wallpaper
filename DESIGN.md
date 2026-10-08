# Design notes

Choices that change what the wallpaper looks like, and why. Newest last.

## Layout

Two rows of five KPI cards, then Daily Trend (stacked bars, last 30 days) beside Daily Activity
(weekday × week heatmap, 26 weeks), then Tools and Models donuts by tokens. It is laid out at
1600×900 CSS px and zoomed into the safe area of each screen; the menu bar and the Dock (side and
size read from `com.apple.dock`) are left clear.

## A display, not an app

Nothing may look clickable: no pills, segmented toggles, buttons, help icons or links. Panel
headers carry a plain gray caption for the range or unit ("Last 30 days", "By tokens").

## Readability over style

The first version was 1984-Macintosh pixel art (bitmap Chicago, dithered grays). It was hard to
read on a real desktop: 9 px labels, patterns instead of tones, a dark mode that inverted the
windows. A round of more stylized, illustrative ideas was also dropped. The wallpaper is a
conventional analytics dashboard instead, because people glance at it between windows.

## Theme

Zinc neutrals. Dark: `#09090b` page, `#18181b` cards. Light: `#f4f4f5` page, white cards. The theme
follows the macOS appearance at each update. Two accents only on the cards: green for cost, blue for
active days. Chart series: blue, green, amber, red, violet, pink, then gray for "Other".
Numbers are SF Mono (embedded from the system font file); labels use the system font.

## Numbers

- Deltas compare the last 30 days with the 30 days before. The "Today" cards compare today with the
  30-day daily average.
- ccusage daily reports carry no hours, sessions, messages or projects, so the heatmap is per day
  and there are no time or session cards.
- History is kept locally and a day never shrinks, because agents delete old session logs.
