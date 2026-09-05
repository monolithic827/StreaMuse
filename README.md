# StreaMuse

Turns this PC into a speaker that Apple Music and Spotify can play to, and re-streams whatever they
send as an HLS (`.m3u8`) stream published worldwide through a Cloudflare tunnel. No OBS, no Docker.

It appears in Apple Music's AirPlay menu as a speaker and in Spotify's device list as a Connect
device. The audio, the track info and the cover art all arrive over that one connection, so the
now-playing info can never belong to some other app. It renders a cover-art video track and muxes
both into 1-second mpegts HLS segments - the format VRChat's AVPro player handles reliably.

```
 ┌──────────────────────────── StreaMuse ─────────────────────────────────┐
 │  Apple Music ──AirPlay──> RAOP receiver  ─┐                            │
 │  Spotify     ──Connect──> go-librespot   ─┴──> ffmpeg ──> hls/*.ts     │
 │                                                                        │
 │  :7788 loopback  control panel                                         │
 │  :7789 loopback  /live/{key}/index.m3u8  <── cloudflared ──> internet  │
 └────────────────────────────────────────────────────────────────────────┘
```

## Running it

Download `StreaMuse.exe` from the [latest release](../../releases/latest) and run it. It carries
ffmpeg, cloudflared and go-librespot inside it, so there is nothing to install, nothing to download
on first launch and nothing that needs the internet until you publish a stream.

Then:

1. Pick **Apple Music** or **Spotify** in the panel.
2. In Apple Music, open the AirPlay menu and choose **StreaMuse**. In Spotify, open the device list
   and choose **StreaMuse**. (Rename it under Settings → Receiver name.)
3. **Start stream**, then **Start tunnel**, and copy the URL.

To publish automatically whenever you start streaming, tick *Start the tunnel automatically* in
Settings.

Because your music now plays *to* StreaMuse rather than out of your speakers, you hear it through the
stream. Nothing else on the machine is captured or muted.

## What the panel controls

| | |
|---|---|
| **Audio source** | Apple Music or Spotify, and whether a sender is connected right now |
| **Now playing** | Cover, track, progress, and play/pause/next/previous sent back to the app |
| **Stream** | Start/stop, live status, the public URL with a copy button, and tunnel start/stop |
| **Receiver** | Which client is connected and a live level meter fed from the received audio |
| **Stream health** | Encoder settings, uptime, dropped frames, and a rolling log from every component |
| **Settings** | Receiver name, stream key, resolution, frame rate, bitrates, text overlay, tunnel mode |

Encoder settings apply on the next start; the source can be switched at any time.

## Requirements

- Windows 11, Apple Music from the Microsoft Store and/or the Spotify desktop app.
- Spotify Connect needs **Spotify Premium**.
- The first time a sender connects from another device, Windows Firewall will ask to allow StreaMuse
  on the private network. Everything on this same PC works without it.

## Security

**The tunnel only exposes the stream.** The control panel, its API and the WebSocket listen on a
separate loopback port that cloudflared never sees. Everything on the public port other than
`/live/{key}/` - the playlist, the segments, the listener page, and its now-playing feed - returns
404, verified including path traversal attempts. With the stream stopped the public feed reports
nothing at all, even while the tunnel is still up.

## Diagnostics

```powershell
uv run streamuse --test-receiver apple 20     # listen for 20s and report what arrived
uv run streamuse --test-receiver spotify 20
```

This is the fastest way to answer "why is my stream silent": it says whether a sender ever connected,
what track it reported, the peak level, how much of the audio was silence, and how far it drifted
from wall clock - and writes what it heard to a WAV.

## Building

```powershell
uv run pyinstaller streamuse.spec --noconfirm   # dist/StreaMuse.exe
```

CI builds the same one-file exe on every push and attaches it to a release on a `v*` tag. It stages
the three tools into `vendor/bin` first and fails the build if any is missing, so a release can
never ship without them. go-librespot's own Windows build cannot write audio to a pipe, so CI builds
it from upstream with the one-file patch in `vendor/go-librespot/`.

Running from source instead (`uv run streamuse`) downloads ffmpeg and cloudflared into
`%LOCALAPPDATA%\StreaMuse\bin` on first launch, ~200 MB, once - the AirPlay speaker is advertised
before the download starts, so Apple Music can pick it straight away. go-librespot is not
downloaded; build it per `vendor/go-librespot/README.md` or take the one out of a release exe.
Without it Spotify shows as unavailable and Apple Music works normally.
