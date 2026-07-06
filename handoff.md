# Handoff: Light Mode Toggle

## Goal
Add a light/dark theme toggle matching cove-download-manager: sun/moon button in the titlebar control cluster, immediately left of Minimize. Dark mode shows a sun (click = go light), light mode shows a moon (click = go dark). Theme persists across restarts. Light palette token values mirror cove-download-manager's `_LIGHT` theme (which itself mirrors cove-image-upscaler's CSS custom properties) so the Cove apps share visual identity.

## Changed files
- `renderer/index.html` - only file changed

## Implementation summary
- `:root` gains new CSS variables for every previously hard-coded dark-only color (window edge, scrollbar thumbs, hover backgrounds, progress track, toggle pill, titlebar tint, log console bg, badge/thumbnail gradients, panel/popup/badge shadows, app background gradient, danger text/border/soft, info blue).
- New `:root[data-theme="light"]` block overrides the full palette with values mapped from cove-download-manager `theme.py` `_LIGHT`.
- All hard-coded dark-only colors in the CSS and inline JSX styles were converted to the new variables (body border, scrollbars, resize grip, titlebar gradient, brand badge, ctrl button hovers, panel shadow, progress bar track, danger button/pill, toggle pill, log console, dropdown shadow/hover, link row hover, downloading state blue, empty-state thumbnails).
- `WindowChrome` gains `theme` / `onToggleTheme` props; `ctrlBtn` helper gains a `tip` (tooltip/aria-label) parameter now used by all four buttons. New `IconSun` / `IconMoon` icons (same SVG paths as cove-image-upscaler's `ThemeToggle`).
- Theme state lives in `CoveDownloader`, persisted to `localStorage["cove:theme"]` (the Cove web pattern from cove-image-upscaler; synchronous read so no flash). A small inline `<script>` in `<head>` applies the saved theme before first paint.
- Dark mode remains the default and is visually unchanged (all dark variable values are the previous literals).

## Verification run
- JSX compile check: extracted the `text/babel` script and ran it through `@babel/core` + `@babel/preset-react` -> "JSX compiles OK"
- `timeout 12 npx electron . --enable-logging` -> app launched, ran 12s, no renderer/JS errors (only system fontconfig warnings)
- `git diff --check` -> run before staging

## Manual checks (for Sin)
| Check | Status |
|---|---|
| Toggle appears left of Minimize | NOT EXECUTED (needs visual check) |
| Sun in dark, moon in light, tooltips correct | NOT EXECUTED |
| Light mode readable everywhere (log, dropdowns, danger buttons, progress) | NOT EXECUTED |
| Theme persists across app restart | NOT EXECUTED |
| No dark flash on light-mode startup | NOT EXECUTED |

## Scope
Only `renderer/index.html`. No main.js/preload.js/backend changes (persistence is renderer-side localStorage).
