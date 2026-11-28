Changelog
Unreleased — November 23, 2026
Media Requests
- Added Seerr integration (Overseerr/Jellyseerr) as an alternative to VODUM's built-in Sonarr/Radarr request engine, with per-library configuration, account matching, availability indicators, season and 4K options, and request tracking.
- Added a configurable recent-requests limit in the user portal.
- Improved Sonarr/Radarr request reliability: unavailable instances no longer block eligible destinations, and requests can fall back to the next available instance according to configured priorities. Uncertain submissions are never automatically repeated.
- Improved error reporting for failed media requests.
Collections & Lists
- Introduced a Lists management interface for movies and TV shows, including custom lists, media search, activation, deletion safeguards, and links to collections and libraries.
- Added a Media List collection type, allowing collections to be generated from a selected VODUM or custom list.
- Moved Franchise collections to locally managed lists instead of relying on downloaded public lists, with support for mixed movie/TV content and explicit list associations.
- Expanded the built-in franchise catalog to 32 lists, including Stargate, Harry Potter, Middle-earth, Jurassic Park, James Bond, Doctor Who, Dragon Ball, and more.
- Added dedicated franchise artwork and improved poster caching and metadata lookup, including IMDb ID fallback and identifier-based search.
- Improved franchise synchronization and recovery from collection-name conflicts without overwriting unrelated collections.
- Added contextual warnings when a collection requires missing TMDb or MyAnimeList credentials.
Monitoring & Performance
- Moved historical Monitoring statistics and additional filtered views to background precomputation, reducing heavy work during page requests while keeping live indicators current.
- Improved Monitoring diagnostics and worker time limits for Plex/Jellyfin data collection.
- Optimized user searches, collection status polling, user profile loading, log filtering, and merge suggestions to reduce unnecessary database work.
- Added CPU and RAM indicators to server cards on the dashboard.
Security & Reliability
- Fixed trusted reverse-proxy header handling under Waitress, preserving VODUM's proxy validation and HTTPS/client-address detection.
- Improved Plex session termination verification to avoid treating malformed or incomplete responses as successful termination.
Fork only (r3surrection/VODUM, not part of upstream)
- Dashboard API: read-only JSON API under /api/dashboard/v1 (health, users, users/<id>, expiring, requests, servers, tasks, stats) plus POST users/<id>/gift, using the shared gift logic. Details: docs/dashboard-api.md.
- Dashboard API monitoring endpoints: read-only /api/dashboard/v1/monitoring/ overview, live, activity, heatmap, top, locations, usage-risk, events and artwork, with previous-period comparison, sparklines, partial-day flags, week buckets (KW) from six months, a 7x24 heatmap in Europe/Berlin, and city-level locations. No IP or mail address is ever returned. Details: docs/dashboard-api.md.
- Dashboard API admin page with an on/off switch and database-backed API keys (stored as hashes, shown once); the VODUM_DASHBOARD_API_KEY environment variable still works.
- GHCR image workflow for the fork/main deploy branch.
- Seerr series without a TVDB id: the portal now asks Seerr's Sonarr lookup for the TVDB series, takes a single match over automatically and otherwise shows a picker (poster, title, year). The choice is sent as tvdbId, so the request reaches Sonarr. Without any match the member gets a clear message and nothing is requested.
- Seerr requests with a lost answer are no longer left as "done" without an id: VODUM finds the member's existing Seerr request (same user and TMDB id) and attaches its id, so the status shows in the portal; a retry attaches instead of requesting twice. New texts in all languages.
- Seerr requests are no longer sent when Seerr has no default Sonarr/Radarr for the requested tier (HD or 4K): Seerr would only log a warning and leave the request "approved" forever. The member gets a clear message instead.
These changes are currently listed as unreleased. Production deployment and validation are not implied.