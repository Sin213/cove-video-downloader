# Handoff: Search Tab + Collapsible Log

## Goal
Add a polished "Search" tab alongside "Video Links" in the left panel, and make the Log panel collapsible.

## Changed files
- `renderer/index.html` — tabs, search UI, collapsible log, new icons/styles/state
- `preload.js` — expose `search.run(query)` via contextBridge
- `main.js` — IPC handler `cove:search:run` → `sendCommand({cmd:"search", params:{query}})`
- `python/backend.py` — `_run_search()` helper + `elif cmd == "search"` dispatch branch

## Feature summary
- Video Links and Search are tab buttons in the existing panel header area
- Video Links tab: all existing URL paste/input/list/summary behavior preserved
- Search tab: input + Search button → calls `yt-dlp "ytsearch10:<q>" --flat-playlist --dump-json`
- Results appear as thumbnail rows; clicking selects and shows a detail card
- "Add to queue" on selected card calls existing `addUrls(result.webpageUrl)`
- Log panel header is clickable to collapse/expand; Clear button still works independently
- Search runs in a daemon thread, does not touch the `_busy` download lock

## Verification run
- `python3 -m py_compile python/backend.py` → PASS
- `git diff --check` → PASS
- `npm start` manual smoke → NOT EXECUTED (no display server in this environment)

## Manual checks
| Check | Status |
|---|---|
| App launches | NOT EXECUTED |
| Video Links tab is default | NOT EXECUTED |
| Existing URL add/paste/clear/queue flow works | NOT EXECUTED |
| Existing download flow works | NOT EXECUTED |
| Search tab hides URL paste UI | NOT EXECUTED |
| Search tab can search YouTube | NOT EXECUTED |
| Results show title + thumbnail | NOT EXECUTED |
| Clicking result shows selected card | NOT EXECUTED |
| Add to queue adds URL to existing queue | NOT EXECUTED |
| Selected result downloadable via global controls | NOT EXECUTED |
| Log collapses and expands | NOT EXECUTED |
| Log Clear works without toggling collapse | NOT EXECUTED |
| Collapsed log does not reserve body height | NOT EXECUTED |

Reason NOT EXECUTED: no display server / headless environment.
