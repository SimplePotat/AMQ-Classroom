# Repository bug-test sweep

Run: 12 November 2026. Baseline: `b078bcd`, followed by the fixes and regressions in this change.

## Results

| Check | Result |
| --- | --- |
| Classroom server suite | 154 tests passed, including 17 new regressions |
| Retained upstream selection/filter units | 16 tests passed; worker imports stubbed, real selection/filter logic exercised |
| Classroom browser smoke suite | Passed with six profiles, host/phone/desktop sessions, synthetic audio and decoded video |
| New browser regressions | Passed with native video endings and deliberately interrupted requests |
| Legacy browser suite | Passed: safe stored scoreboard data, names, persistence, and malformed storage |
| Live HTTP service test | Passed with 100 simulated LAN sessions and an actual server process restart |
| Django system checks and migration drift | Passed; no missing model migrations |
| Python compilation, JavaScript parsing, shell parsing, diff whitespace | Passed |
| Installed Python dependency audit | 10 packages checked; no known advisories returned |
| npm audit and pip dependency consistency | No known npm vulnerabilities; no broken Python requirements |

Local runtime: Linux, Python 3.13.11, Node 24.22.1, Playwright 1.62.1/Chromium, FFmpeg-generated synthetic media. Classroom dependencies: Django 5.2.19, Waitress 3.0.2, Requests 2.34.2, qrcode 8.2.

## Bugs fixed

| Trigger | Previous behavior | Fix |
| --- | --- | --- |
| One anime has many themes | Raw theme counts stopped imports before enough distinct shows were available | Pool limits count distinct shows |
| An openings.moe show has many versions | Versions consumed the selection limit before later shows were considered | Resolve one eligible song per show before moving on |
| Openings-first pool also queries AnimeThemes for covered shows | Shuffling could discard the preferred provider, and preparation made unnecessary requests | Query AnimeThemes only for uncovered MAL IDs |
| Selected openings-first provider is unavailable | The whole preparation aborted | Continue with AnimeThemes; rate limits still stop preparation |
| One MAL anime has different provider titles | The same show could enter the queue twice | Deduplicate MAL pools by MAL ID |
| openings.moe details or fallback encounters rate limiting | Errors could be swallowed while preparation continued | Propagate rate limiting through lookup and video fallback paths |
| A media URL has an invalid port | Availability probing raised an exception | Treat the URL as unavailable |
| Removed player edits their profile after the roster fills | API returned 409 but profile changes remained saved | Save profile and join roster within one transaction |
| MAL status settings contain JSON booleans or floats | Values such as `true` and `1.0` were accepted as integer status codes | Validate exact value types |
| A host action arrives after its round ends | It could pause, reveal, skip, or modify the next round | UI includes round IDs; server rejects supplied stale IDs |
| Initial playback-start POST is interrupted | Audio played while the room remained stuck buffering | Retry from polling after actual playback begins |
| Volume preference is invalid or browser storage is blocked | Video setup could throw before event listeners were installed | Validate volume and tolerate unavailable storage |

## Additional coverage

The browser regressions cover full reveals restarting at zero, avoiding repeated rewinds, overriding the timer, pause/resume, host refresh, real video endings, interrupted ending notifications, automatic/manual advancement, stale endings, host command round IDs, phone-sized settings, native input validation, corrected resubmission, and invalid/zero/unavailable volume storage.

The HTTP service test uses normal HTTP registration and CSRF/session cookies for 100 clients with distinct loopback source addresses. It checks concurrent ready/guess/poll requests, the 101st-player rejection without an orphan profile, answer/media privacy, byte-range media responses, stale guesses, host-only controls, exact scores, saved sessions and answers after process restart, voided scores, a single persisted result, and 200 CSV rows.

All tests use isolated temporary databases and synthetic media. Existing runtime data and profiles were not changed. Provider responses are simulated; real music is not downloaded.

## Reproduce

```sh
DJANGO_SETTINGS_MODULE=classroom.settings .venv/bin/python -m django test classroom
.venv/bin/python scripts/test_legacy_units.py
.venv/bin/python scripts/service_stress.py
npm run test:browser
node scripts/test_legacy_browser.mjs
```

The browser commands require the installed Playwright Chromium shell. `PLAYWRIGHT_BROWSERS_PATH`, `PYTHON`, and `QA_OUTPUT_DIR` may select existing local runtimes and output directories. The new units, browser regressions, and HTTP test are included in the classroom CI workflow.

## Limits

This sweep does not establish an absence of bugs. It did not run live MAL/AnimeThemes/openings.moe integration, the retained Redis/worker deployment, Docker builds, Windows, Firefox, Safari, or a physical 100-device Wi-Fi classroom. Local tests used Python 3.13; CI is configured for 3.12 and 3.13. Dependency audit results cover the versions checked and the advisory data returned at this run.
