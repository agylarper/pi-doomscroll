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

## What it stores

Videos, covers and the feed index go in `~/.cache/doomscroll` (`$XDG_CACHE_HOME/doomscroll` if set). Frames pass through a folder in `/tmp`, and commands go through a Unix socket in `/tmp`. It fetches only public TikTok pages and media through `yt-dlp`. It has no telemetry.

## Develop

```bash
npm test
```

`pi/index.ts` is the Pi extension. `pi/viewer.mjs` is the standalone split viewer. `pi/frame.mjs` turns RGB frames into PNG. `pi/frame-writer.mjs` drops stale frames when the terminal lags. `bin/doomscrolld.py` is the player: feed, downloads, decoding and audio.

## Disclaimer

This is an unofficial fan project. It isn't affiliated with TikTok or ByteDance. The videos belong to their creators.

## License

[MIT](LICENSE)
