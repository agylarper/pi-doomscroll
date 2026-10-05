# Optional TikTok login

Guest remains the default. Personal playback must be explicitly selected; saving a login never switches the default.

Use an optional Playwright Chromium persistent profile under the local doomscroll config directory. Login happens in TikTok's browser UI; the extension never asks for passwords or OTPs. A dedicated login command opens the browser and the user closes it when finished. Personal feed uses that profile, reads visible For You video links, and scrolls on demand. Preserve discovery order and isolate seen history from guest mode. Existing yt-dlp downloads remain public-media downloads (no cookie export).

Provide CLI login, logout, and --personal viewer selection plus Pi command shortcuts. Logout refuses while the browser profile is in use. Guest must not load Playwright or touch the profile. Personal failure must report a generic actionable error rather than quietly presenting a guest mix as personalized content.

Limitations: TikTok DOM, CAPTCHA and access restrictions may break discovery; login is not a guarantee of mobile-app-identical recommendations. Browser feed discovery affects the account's browsing signals; terminal viewing/skip durations are not synced back to TikTok. Live authenticated testing needs the user's account and cannot be automated here.

Verification: tests for safe video URL normalization, ordered deduplication, demand-driven feed fetching and error state; run full existing suite and syntax checks. Manually login and verify guest and personal modes separately on user's machine.
