# StreaMuse

Re-streams what Apple Music or Spotify is playing on this PC as an HLS (`.m3u8`) stream, and
publishes it worldwide through a Cloudflare tunnel. No OBS, no Docker.

It attaches directly to the music app's audio with WASAPI **process loopback**, reads now-playing
metadata from the Windows media transport controls, renders a cover-art video track, and muxes both
into 1-second mpegts HLS segments - the format VRChat's AVPro player handles reliably.

```
 ┌──────────────────────────── StreaMuse.exe ─────────────────────────────┐
 │  process loopback (WASAPI) ──> AudioPacer ──┐                          │
 │  media transport controls  ──> CoverFrames ─┴──> ffmpeg ──> hls/*.ts   │
 │                                                                        │
 │  :7788 loopback  control panel (WebView2)                              │
 │  :7789 loopback  /live/{key}/index.m3u8  <── cloudflared ──> internet  │
 └────────────────────────────────────────────────────────────────────────┘
```

## Running it

```powershell
dotnet run --project src/StreaMuse
```

On first launch it downloads `ffmpeg.exe` and `cloudflared.exe` into
`%LOCALAPPDATA%\StreaMuse\bin` (~150 MB, once). Progress shows in the Stream health log.

Then: start playing music → pick the source → **Start stream** → **Start tunnel** → copy the URL.

To publish automatically whenever you start streaming, tick *Start the tunnel automatically* in
Settings.

## What the panel controls

| | |
|---|---|
| **Audio source** | Apple Music / Spotify / External, plus a **Capture target** picker (External only) listing every process currently playing audio |
| **Stream** | Start/stop, live status, the public URL with a copy button, and tunnel start/stop |
| **Capture** | The attached process and a live level meter fed from the captured samples |
| **Stream health** | Encoder settings, uptime, dropped frames, and a rolling log from every component |
| **Settings** | Stream key, resolution, frame rate, bitrates, text overlay, tunnel mode, dependency status |

Encoder settings apply on the next start; source and target changes take effect immediately.

## The three sources

**Apple Music** and **Spotify** mean the dedicated desktop app. That app's process is the only
thing on Windows that identifies the source with certainty, so both options are offered *only*
while that process is running and are struck through otherwise. Choosing one and then closing the
app falls back to External - your stored preference is kept, so reinstalling restores it.

**External** is everything else. You pick the process and the panel states exactly what it attached
to. Capture covers that process and its children, so you get everything it plays - for a web player
that means the whole browser rather than one tab, since nothing in Windows reports which site a tab
is playing.

## How now-playing info works

Audio and metadata come from two unrelated places: audio is WASAPI process loopback on the chosen
process, while track, artist, album and artwork come from the Windows media transport controls - the
system behind the volume-key overlay, which browsers publish to via the web MediaSession API.

Because they are independent, several apps can be reporting a track while only one is captured.
Metadata is therefore used only when its session genuinely belongs to the captured process;
otherwise the panel says *no track info reported* rather than guessing.

## Security

**The tunnel only exposes the stream.** The control panel, its API, and the WebSocket listen on a
separate loopback port that cloudflared never sees. Everything on the public port other than
`/live/{key}/*.m3u8|.ts` returns 404 - verified, including path traversal attempts.

## Diagnostics

```powershell
StreaMuse.exe --probe              # every audio session and media session Windows can see
StreaMuse.exe --test-capture 20    # record 20s of the resolved source to a WAV and report drift
```

`--test-capture` is the fastest way to answer "why is my stream silent": it reports the peak level,
how much silence was filled, and how far the written audio drifted from wall clock.
