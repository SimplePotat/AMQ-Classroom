# Independent bug-test sweep, round two

Run: 12 November 2026. Baseline: `a3fc21a`. This adds new adversarial, model-based, and network-failure tests alongside the earlier suites.

## New coverage

- **16 API/JSON tests**, with more than 250 generated boundary cases: malformed and truncated JSON, excessive nesting, non-finite numbers, invalid Unicode in keys and values, wrong field types, invalid methods, GET/HEAD byte-range matrices, empty files, and very large decimal ranges. Accepted-case tests preserve valid Unicode, surrogate pairs, finite exponents, and exact JSON depth boundaries.
- **Six provider fault tests**: corrupt nested records and optional labels, invalid video URLs and numeric metadata, deep/non-finite JSON, and cancellation while streaming a provider response.
- **One seeded state-model test**: seeds 0–7 drive 64 matches and 640 rounds across all combinations of full/timed reveals and manual/automatic advancement. An independent ledger checks scores and histories after revisions, corrections, voids, pauses, restarts, spectators, roster changes, expired deadlines, rejected actions, and resets. No new scoring or lifecycle discrepancy was found.
- **Five browser fault scenarios**: response loss after a successful server commit; player offline drafts and host reconnection; a guess delayed until the next round; account changes in another tab; and a state response held open indefinitely. Uses real synthetic media and Chromium, with failures injected through Playwright routing.

## Findings and fixes

| Finding | Resulting behavior |
| --- | --- |
| Invalid Unicode and non-finite JSON were accepted, including escaped lone-surrogate guesses | Shared input/provider validation rejects these values before persistence; container depth is bounded at 32 |
| Older completed results could contain a surrogate that made CSV export return 400 | Export replaces invalid Unicode in cells with `�`, preserves formula escaping, and leaves saved Room/GameResult data unchanged |
| Huge valid byte-range decimals hit Python's integer conversion limit and returned 400 | Bound ranges to the media size before numeric conversion; return the appropriate slice or 416 |
| Media routes accepted mutation methods | Allow GET/HEAD; other authenticated methods receive 405 without ticking the room |
| Malformed optional provider labels, theme entries, or video metadata could abort selection | Skip unusable records, retain healthy videos, and use existing fallback labels for malformed optional text |
| Streamed provider responses did not check cancellation between chunks | Cancellation closes the response before consuming the rest of the body |
| A never-ending state request kept polling blocked | State requests have an eight-second abort deadline, allowing subsequent polls to recover |
| Logout in another tab left private profile fields open over sign-in | Identity changes clear the profile editor; shell/role changes close open dialogs |

Invalid Unicode, non-finite values, byte-range errors, media methods, corrupt provider records, stream cancellation, the blocked poll, the cross-tab editor, and the legacy CSV problem had failing reproductions before their fixes. Deep JSON was accepted on this Python runtime; the depth guard is defensive validation, not a claim that the decoder crashed here.

## Combined verification

| Suite | Result |
| --- | --- |
| Classroom server suite | 177 tests passed, including 23 new methods |
| Retained upstream selection/filter units | 16 tests passed with worker imports stubbed |
| Browser suites | Classroom smoke, playback regressions, new network faults, and legacy scoreboard checks passed |
| HTTP service test | 100 simulated LAN sessions passed concurrent registration, guessing, scoring, roster limits, privacy, real process restart recovery, and CSV export |
| Django checks, migration drift, compilation, JavaScript syntax, whitespace | Passed |

The standard `npm run test:browser` now includes the new network suite. Django discovers the new server tests automatically. The existing GitHub Actions workflow runs these commands on future changes; its server matrix covers Python 3.12 and 3.13.

## Reproduce the new tests

```sh
DJANGO_SETTINGS_MODULE=classroom.settings .venv/bin/python -m django test classroom.test_api_adversarial classroom.test_provider_faults classroom.test_state_model
node scripts/browser_network_faults.mjs
```

Use the [README verification commands](../README.md#development-and-verification) for the complete suites. Tests use temporary databases, synthetic media, simulated provider responses, and local servers. Existing user data was not changed.

## Confidence and limits

The covered scenarios have no remaining test failures. This does not establish that the build is bug free: the input space, scheduling, browser behavior, and external services cannot be exhausted by this suite. This run did not test live provider availability, Windows, Safari/Firefox, Docker/Redis deployment, or a physical 100-device Wi-Fi classroom. Local execution used Linux, Python 3.13, Node 24, Chromium, and FFmpeg; the CI matrix additionally exercises Python 3.12.
