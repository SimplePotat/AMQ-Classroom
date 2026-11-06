# Classroom implementation

`classroom` is a separate Django application inside the upstream fork. `run_classroom.py` selects `classroom.settings`, applies migrations, recovers interrupted state, discovers the LAN URL, and starts one Waitress process with 16 threads. The original `manage.py`, `quiz`, and `anime_quiz` application remain available with security fixes and independent settings/routes.

## Components

- `models.py`: durable profiles, one room document, API caches, and completed game records.
- `engine.py`: authoritative transitions, normalization, scores, deadlines, and public serialization.
- `library.py`: bounded background playlist preparation, MAL pagination, AnimeThemes lookup, caches, local catalog validation.
- `views.py`: session authentication, CSRF-protected JSON endpoints, host authorization, range-capable media, QR and CSV generation.
- `security.py`: IP/explicit-host request boundary and expiring host grants tied to the current host key.
- `static/app.js`: one-second polling, persistent video element, phone answer forms, countdown interpolation and reconnection.
- `static/style.css` and `scene.svg`: responsive layouts, whiteboard mode and original artwork. No external frontend dependencies.

```mermaid
stateDiagram-v2
  [*] --> Lobby
  Lobby --> Loading: host prepares
  Loading --> Ready: playlist built
  Loading --> Lobby: provider error / restart
  Ready --> Buffering: host starts
  Buffering --> Guess: host audio is playing
  Guess --> Reveal: server deadline / host reveal
  Reveal --> Buffering: next song
  Reveal --> Finished: final song
  Guess --> Buffering: skip without score
  Finished --> Lobby: new quiz
```

Skipping a final song also finishes the game. Pause freezes either timed phase. Reset cancels a playlist job through its generation identifier. A restarted timed phase is paused before any expired deadline can advance.

## Integrity boundaries

Each read or mutation of room state holds a process-wide reentrant lock and an immediate SQLite transaction. The server ticks its clock before validating actions. State and score changes commit together; duplicate reveal/next requests cannot reapply points. Concurrent finalization has a multi-threaded test. Multiple server processes require replacing this coordination model and are deliberately unsupported here.

Anonymous guests receive only sign-in/join information. Signed-in players receive public room information, broad title suggestions, and their own guess. They receive neither current-song metadata nor the media URL before reveal. MAL usernames are omitted from the public roster and preparation diagnostics are host-only. Only an authenticated host can load the current media endpoint. Round identifiers reject stale guesses and stale playback confirmations. Title suggestions do not identify the current song.

The host is trusted: it controls playback and may correct answers. The game does not hide answers from the host or attempt to prevent recognition using outside tools. Local media paths must resolve inside the media directory, including after symlink resolution. Range requests are bounded to file size. Profile strings are HTML-escaped. Authentication uses Django hashing, session/CSRF rotation, and mutually exclusive player/host identities. Host grants expire after 12 hours and contain a fingerprint of the current host key for revocation. CSV formula-like cells are escaped; completed-game exports use the saved result snapshot, independent of later roster edits.

All routes validate the HTTP Host header. Wildcard configuration only permits literal IPs/localhost alongside explicitly allowed names, preventing arbitrary-domain DNS rebinding. Waitress and application middleware enforce 16 KiB request bodies; the server also limits header size and idle connections. Authentication and player mutations have per-IP rate limits. Untrusted forwarded headers are cleared. Provider JSON is streamed with a 4 MiB decoded limit, exact HTTPS provider hosts, request timeouts, and no redirects. MAL imports and combined title/anime sets have finite limits.

## Persistence and tradeoffs

SQLite records survive restarts; ninety-day player browser sessions and passcodes allow profile reuse. Browser-only unsent drafts survive pause and temporary connection loss and are cleared on account changes/sign-out. Only acknowledged guesses survive a page refresh. Loading jobs run in a daemon thread and are restartable, not durable background workers. POSIX data directory, database and secret permissions are restricted at startup.

Public MAL list scraping and AnimeThemes availability are external dependencies for online mode. Individual unavailable MAL lists are skipped with a host warning, while global rate limits abort preparation. Offline mode removes those dependencies. Metadata caches expire after seven/thirty days. The starter refresh script is a maintainer operation, not required at startup.

There is one classroom per server. Multiple classes can use different ports and data directories. There is no public matchmaking, messaging, email recovery, or cloud service dependency.
