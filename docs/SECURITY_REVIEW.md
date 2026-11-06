# Security review — 6 November 2026

The review covered the classroom HTTP/API boundary, sessions and CSRF, authorization, game integrity, data exposure, rendering, filesystem access, provider requests, dependency advisories, launchers, container configuration and CI. It also inspected the retained upstream application, whose routes are separate from classroom mode. Three parallel code reviews were combined with manual reproduction, static analysis, dependency audits and regression tests.

## Findings and fixes

| Area | Confirmed issue | Fix |
| --- | --- | --- |
| LAN boundary | The wildcard Host allowlist accepted attacker-controlled domains, allowing a DNS-rebinding page to obtain its own CSRF token against the LAN server. | Validate every route, including assets. Permit IP literals/localhost by default and named hosts only when explicitly configured; mixed wildcard/name lists remain protected. |
| Host authorization | A boolean host session survived host-key rotation and inherited the 90-day player session lifetime. | Expiring 12-hour host grants tied to the current key. Replacing the key and restarting invalidates prior grants. Old boolean grants are rejected. |
| Identity switching | A session could retain host authority after signing in as a player. Unsent browser guesses could follow a different account on a shared device. | Exclusive host/player identities, rotated session and CSRF tokens, revoked prior session cookies, and draft clearing on logout/account changes. |
| Local data | Default filesystem permissions exposed the database, session records and player password hashes to other local accounts. | Owner-only POSIX data directory, secrets, database and sidecar permissions, including existing installations. |
| Privacy | Anonymous state disclosed the roster/game; public player data and loading messages disclosed other players' MAL usernames. | Minimal anonymous state, own-profile MAL access, and host-only loading diagnostics. |
| Request abuse | Large request limits and unrestricted player mutations allowed avoidable processing/storage pressure. A full-room registration race could leave a profile or changed identity after a failed join. | 16 KiB body/header bounds, shorter idle timeout, per-IP mutation limits, bounded profile inputs, and atomic registration/join before session changes. Existing auth throttling and CSRF protection remain enforced. |
| Results | Leaving or editing the roster after a game changed the host's exported final results. | Export the immutable completed-game snapshot. Existing CSV formula protection remains in place. |
| Playlist availability | One inaccessible MAL list aborted the entire group's preparation. Provider responses and aggregate lists were insufficiently bounded. | Isolate failed lists with host warnings; exact HTTPS metadata hosts, no redirects, 4 MiB decoded response cap, and finite list/title/candidate limits. Global rate limits still stop preparation. |
| Legacy rendering | Username alerts and cached lyrics were marked safe; temporary errors also interpolated unescaped text. Player names were inserted into HTML and the entire scoreboard was restored as executable HTML from local storage. | Django autoescaping with safe line breaks and escaped error formatting. Build scoreboards from trusted templates and bounded JSON using DOM value/text properties; discard old HTML storage. |
| Legacy defaults/providers | The retained app had a hard-coded development secret, debug enabled and wildcard hosts. MAL requests lacked timeouts and accepted usernames that changed URL structure. | Random fallback secret, explicit debug/host configuration, validated usernames/query encoding, finite pagination, provider timeouts and rejected redirects. |
| Dependencies | The installed pip 25.3 had known advisories. Declared Django and Requests floors allowed vulnerable releases. | Upgraded local pip to 26.3; require pip 26.2+ in new environments/CI/container builds, Django 5.2.19+, and Requests 2.33.0+. Existing environments have explicit upgrade instructions. |

The dependency floors follow the [Django 5.2.19 security release](https://docs.djangoproject.com/en/5.2/releases/5.2.19/) and the Requests advisories for [netrc credentials](https://github.com/psf/requests/security/advisories/GHSA-9hjg-9r4m-mvj7) and [temporary archive extraction](https://github.com/psf/requests/security/advisories/GHSA-gc5v-m9x4-r6x2). The latter utility is not used by this application; the updated floor excludes the affected version anyway.

## Verification

- **63 Django tests passed**, including 41 new security regressions covering the findings above, provider failure isolation, path/authorization boundaries, CSRF, rate limiting, registration races and private file permissions.
- **Classroom browser suite passed:** six registered profiles, host/phone/desktop sessions, audio/video, scoring, whiteboard, pause/reconnect, CSV, and cross-account draft isolation.
- **Legacy browser security suite passed:** malicious names remain text, prior saved HTML is discarded, JSON restores safely, and scores/new players still persist.
- `pip-audit` found **no known vulnerabilities in the installed classroom environment** after the upgrade. `npm audit` found **zero vulnerabilities** in the browser-test dependency tree. These results are point-in-time advisory checks, not proof that packages have no undiscovered flaws.
- Bandit and manual sink review were used to check Python and JavaScript paths. Remaining Bandit findings are calls on the upstream app's explicitly constructed `random.SystemRandom()` instance; these are quiz selection operations, not credential generation.
- CI now runs the new regressions, both browser suites and Python/npm dependency audits.

The standalone retained legacy deployment's Redis, queue, cloud integrations and complete transitive dependency environment were not launched. Its declared direct dependency minimums were checked against advisories, and the affected view/template/provider boundaries were tested with service stubs. Container startup was verified during the implementation; the security revision changes its package bootstrap and was not rebuilt during this review.

## Operational boundaries and upgrade

This remains a single-process application for a trusted LAN over HTTP. Traffic, passcodes and session cookies are not encrypted in transit. Use distinct classroom passcodes and a trusted network; do not expose this HTTP service publicly. Open registration is intentional, the host is privileged, and application-level limits do not prevent every denial-of-service attack. Windows relies on Windows folder permissions rather than POSIX mode bits.

Stop the server, back up the private data directory, pull the updated source and run the dependency upgrade commands in the [README](../README.md#updating-an-existing-installation). Restart to apply permission fixes and the new host-grant format, then sign the host in again. Player profiles, submitted answers and completed results remain available. Rotating a compromised host key requires replacing `CLASSROOM_HOST_KEY` (or the private `.host-key` file) and restarting. Do not distribute the data directory or publish its contents.
