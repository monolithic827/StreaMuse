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

## DJ addon (optional)

StreaMuse ships as the plain re-streamer; extra features arrive as plugins you install yourself. The
DJ plugin lets you request a song from the control panel and have it mixed into the live stream -
fetched, auditioned, tempo-matched to what's currently playing, and crossfaded in and back out.

To install any plugin: **Settings → Plugins → Install plugin**, and pick a `.dll` or a `.zip`. If
nothing is loaded yet it activates immediately; otherwise restart StreaMuse. Installed plugins are
listed there with which one is active.

For the DJ plugin specifically: build `src/StreaMuse.DjAddon/StreaMuse.DjAddon.csproj`, then install
**both** `StreaMuse.DjAddon.dll` and `SoundTouch.Net.dll` from its output folder - zip them together
and install the zip in one go - and tick **Enable DJ mixing** in Settings. A **DJ** button
appears in the top bar and opens the decks in **their own window** - request box, what's playing, the
queue and skip - so you can keep an eye on the stream and the decks at the same time.

Ask for a song while one is playing and it waits in the queue, then comes in over the closing bars of
the current track - beat-matched to it, on a phrase boundary - so the blend finishes as that track
ends, the way a set runs. **Skip to next** brings it in immediately instead; it still mixes rather than
cuts, and mixes back to your live audio if nothing else is queued.

A plugin runs as part of StreaMuse with the same access to your machine that StreaMuse has - there is
no sandbox. Only install plugins you trust.

Every request is fetched the same way regardless of your source: the addon searches YouTube with
`yt-dlp` and downloads the best-match audio on the fly. **This is against YouTube's Terms of
Service** - it's the only practical way to turn a free-text request into audio, but using it is your
call and your responsibility, the same posture this project already takes toward auto-downloading
ffmpeg/cloudflared, just spelled out here because this one fetches copyrighted media. There is no
Spotify or other streaming-service integration - the addon only ever downloads and mixes in audio
itself, it never controls another app's playback.

Like a DJ cueing a record in headphones, the plugin listens to a track before the stream ever hears
it: it checks the download is actually playable, refuses silent or too-short ones outright, and finds
where the music really starts so the mix doesn't fade the live source out into a silent intro. What it
found gets logged - *"auditioned: 232s, peak -9.0 dB, skipped 2.0s of intro, 136 BPM"*.

The transition is a real one, not a volume fade. Both tracks stay at full level and the *bass* changes
hands: the requested track comes in with its low end cut so it sits on top of what's playing, the bass
swaps across on the beat halfway through, and only then does the old track leave. It drops on a beat,
having matched the tempo first.

Beat detection is [SoundTouch](https://www.surina.net/soundtouch/) (LGPL-2.1, via the managed
`SoundTouch.Net` package) rather than something home-grown, so it's a properly exercised detector. It's
still best-effort: it reads the beat from the fetched track and from whatever's already live - an
unknown capture that might not even be 4/4 - and when it can't find a beat it trusts, it says so and
does a plain fade instead of forcing a swap that would land wrong. Very fast material (180 BPM and up)
is a known gap: it reads the half- or third-time and declines, so hardcore and drum-and-bass will fade
rather than mix.

A track also doesn't just start playing from the top. It's cued in from wherever its groove actually
locks into a steady beat - skipping past a sparse or free-tempo intro - the same thing a DJ does by ear
before dropping a record in.

The requested track plays on its own clock, so it keeps going even when the app you're capturing is
silent - which is the usual case when you just want to play a request into a quiet stream.

**With Apple Music or Spotify as the source**, the plugin pauses the app once a request has fully
taken over - it isn't contributing any audio at that point anyway - and resumes it as soon as it knows
nothing else is queued, so the app is playing again in time for its own outro. This has only been
checked for the safe case (every other source correctly leaves the app alone); the actual pause/resume
commands haven't been exercised against a real Spotify or Apple Music session.

**Sound effects**: drop audio files into `%LOCALAPPDATA%\StreaMuse\dj-sfx\` and, with **Sound effects**
ticked on in Settings, the plugin occasionally picks one at random and mixes it in right on the beat of
a transition - an accent, not a constant thing. Nothing to name or configure per file; it just picks
from whatever's in the folder. There's nothing in that folder by default - it only plays what you put
there yourself.

## Diagnostics

```powershell
StreaMuse.exe --probe              # every audio session and media session Windows can see
StreaMuse.exe --test-capture 20    # record 20s of the resolved source to a WAV and report drift
```

`--test-capture` is the fastest way to answer "why is my stream silent": it reports the peak level,
how much silence was filled, and how far the written audio drifted from wall clock.
