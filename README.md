# Doomscroll for Pi

A TikTok-style video feed for the [Pi](https://pi.dev) coding agent. It plays real video, decoded with ffmpeg and drawn with the kitty graphics protocol, with sound.

## Requirements

- Ghostty or kitty (kitty graphics protocol)
- macOS or Linux
- `python3` (3.8+), `ffmpeg`, `ffprobe` and `yt-dlp` on your `PATH`

```bash
# Debian / Ubuntu
sudo apt install ffmpeg python3 pipx && pipx install yt-dlp
```

Keep `yt-dlp` up to date (`pipx upgrade yt-dlp`), because TikTok changes often.

## Run

```bash
pi --extension ./pi/index.ts
# or install as a package
pi install ./
```

Split-screen mode: run `/doomscroll` in Pi, press Ghostty's `Ctrl+Shift+O` to split right, then run this in the right pane:

```bash
node pi/viewer.mjs
```

Pi stays on the left. You have to make the split yourself, because Ghostty doesn't let the Pi process open one.

Overlay mode: `/doomscroll overlay` opens the player in an overlay on the right of Pi. It plays while the agent works and pauses when the agent settles.

Keys: `j`/`k` next/previous, `p` pause, `m` mute, `o` open in browser, `q` or Escape close.

## Optional TikTok login (experimental)

**Guest is always the default.** Login never changes `npm run viewer`, `/doomscroll`, or the guest overlay. Guest needs no browser or Playwright.

For the optional personal split viewer, install Playwright into a virtual environment and activate it in the terminal running Pi/the viewer:

```bash
python3 -m venv ~/.local/share/doomscroll-browser
source ~/.local/share/doomscroll-browser/bin/activate
python3 -m pip install playwright
python3 -m playwright install chromium
# Linux may additionally require: python3 -m playwright install-deps chromium
npm run login
```

Log in directly on TikTok in the opened browser, including any OTP/CAPTCHA. Close that browser when finished; the session stays local. Then explicitly choose:

```bash
npm run viewer:personal
# Back to the main, no-login mode:
npm run viewer
# Close personal/login browsers before removing the local session:
npm run logout
```

Pi shortcuts: `/doomscroll login`, `/doomscroll personal` (split instructions), `/doomscroll logout`. With a package install, use these shortcuts and activate the same environment before starting Pi.

Personal mode uses a separate browser profile to read **visible video links from the logged-in For You page**, preserving their discovery order. The discovery browser stays open while the personal viewer runs; its media playback is disabled to avoid double audio/video. Downloads still use public URLs, with no cookies exported to yt-dlp. TikTok can require CAPTCHA, expire the session, or change its page structure; this is experimental and needs testing with your account. It does **not** promise the exact same feed as the phone app or sync terminal watch/skip durations back to TikTok. Failures do not silently switch to guest.

## What it stores

Videos, covers and the feed index go in `~/.cache/doomscroll` (`$XDG_CACHE_HOME/doomscroll` if set). Frames pass through a folder in `/tmp`, and commands go through a Unix socket in `/tmp`. Guest fetches only public TikTok pages and media through `yt-dlp`. Optional personal mode also reads the authenticated For You page in its browser. The sensitive browser session is stored under `~/.config/doomscroll/tiktok` (`$XDG_CONFIG_HOME/doomscroll/tiktok` if set), outside the repository, with a private profile directory. Treat this profile like a password; do not share it or commit it. Logout deletes that local profile, not TikTok's server-side sessions or cached public videos. It has no telemetry.

## Develop

```bash
npm test
```

`pi/index.ts` is the Pi extension. `pi/viewer.mjs` is the standalone split viewer. `pi/frame.mjs` encodes RGB as async compressed Kitty payloads for the split viewer, or PNG for the overlay. `pi/frame-writer.mjs` drops stale frames when the terminal lags. `bin/doomscrolld.py` is the player: feed, downloads, decoding and audio.

## Disclaimer

This is an unofficial fan project. It isn't affiliated with TikTok or ByteDance. The videos belong to their creators.

## License

[MIT](LICENSE)
