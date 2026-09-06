# CLAUDE.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this is

StreaMuse **is the speaker**. It advertises itself as an AirPlay device that Apple Music streams to,
and as a Spotify Connect device that the Spotify app hands playback to, then re-streams what arrives
as an HLS (`.m3u8`) stream published through a Cloudflare tunnel. One Python package hosts
everything: two aiohttp servers, the receivers, the encoder pipeline, and a pywebview window showing
the control panel. Windows-only by design.

Because audio, metadata and artwork all arrive over the same session, a track can never be
attributed to the wrong source - which is the whole reason for the receiver design over the WASAPI
process-loopback capture it replaced.

## Style

No AI slop. Write what a careful engineer on this codebase would write and nothing more: no defensive
scaffolding for cases that cannot happen, no abstraction with one caller, no options or hooks for
imagined future needs. Match the naming and idiom already around you instead of importing a house
style.

Comments are a last resort. Code needing one usually wants a better name or a smaller function first.
The ones that earn their place say *why*: a constraint that isn't visible locally, a workaround for
someone else's bug, an order that looks arbitrary but isn't. Never restate the line above, never
narrate a change, never leave commented-out code.

Anything that generalises past a single line - an invariant, a trap, why a fix looks strange - goes in
this file instead, so it's read once at the start rather than found by luck. Keep it short and
factual, and extend an existing section rather than adding one.

## Commands

```powershell
uv sync                                        # install
uv run streamuse                               # run

uv run streamuse --test-receiver apple 20      # run one receiver alone for 20s and report
uv run streamuse --test-receiver spotify 20

uv run pyinstaller streamuse.spec --noconfirm  # dist/StreaMuse.exe
```

`.github/workflows/build.yml` produces that same one-file exe. `wwwroot` is data rather than code, so
the spec adds it explicitly and `paths.wwwroot()` resolves it identically from a checkout and from the
unpacked bundle; editing the panel needs no rebuild when running from source.

There is **no test project**. Verification is done by running the app and checking real behaviour.

**Never launch the app as a background job from an agent or IDE shell.** Doing so once took the whole
VS Code instance down with it when the process was reaped: the app died without running `_shutdown`
(its log ends mid-session with no teardown lines), and the editor went with it. The exact mechanism
was never pinned down - `jobs.py` is not it, since that only ever adopts our own children and they
were confirmed dead and cleaned up - so treat this as a rule rather than something to reason around.
Either have the human start it (`! uv run streamuse` from the Claude Code prompt puts its output in
the conversation), or run it in the foreground with an explicit timeout and stop it yourself before
the turn ends. Stopping the *stream* and the *tunnel* over the API is safe and unrelated.

Useful techniques used in practice:

- `curl http://127.0.0.1:7788/api/state` - full state as JSON; poll it in a loop to catch flapping.
- Console errors in the panel: `msedge --headless=new --virtual-time-budget=4000 --dump-dom
  http://127.0.0.1:7788/` and grep stderr for `Uncaught`.
- Screenshot the window with `PrintWindow(hwnd, hdc, 2)` - `PW_RENDERFULLCONTENT` is required to
  capture the web view, the capturing script must be DPI-aware, and the handle has to come from the
  process's `MainWindowHandle` (`FindWindow` by title does not find it).
- A synthetic RAOP sender is the fastest way to exercise the whole Apple path without Apple Music:
  drive the RTSP handshake, then push AES-encrypted ALAC packets over UDP. That is how the receiver
  was verified end to end.
- `--test-receiver` answers "why is the stream silent" fastest: it reports whether a sender ever
  connected, what it said, peak level, silence share and drift from wall clock, and writes a WAV.

## Architecture

```
AirPlayReceiver  mDNS _raop._tcp ─ RTSP :5100 ─ RTP udp 6100-6102 ─┐
SpotifyReceiver  go-librespot.exe ─ \\.\pipe\streamuse-spotify ────┤ only the selected one runs
                 + its HTTP API on loopback                        │
                                                                   ▼
                          track + artwork ──> StateHub ──> WebSocket ──> control panel
                          PCM s16le 44.1k ──> LevelMeter + AudioPacer ─┐
                          CoverFrameRenderer ────────────> VideoPacer ─┤ one shared Clock
                                                                       ▼
                    tcp://127.0.0.1:{audio,video} ──> ffmpeg ──> %LOCALAPPDATA%\StreaMuse\hls
                                                                       │
                                     :7789 public app <── cloudflared <┘
                                     :7788 control app <── pywebview window
```

`StateHub` is the single source of truth. Everything the UI shows arrives in one snapshot pushed
over the WebSocket; the panel is a pure view and only ever posts intents back (start/stop, settings,
transport). When adding UI data, put it in the snapshot rather than adding a poll endpoint.

**Both receivers deliver interleaved s16le at 44.1 kHz**, which is what `sources.SAMPLE_RATE`, the
pacer and ffmpeg's audio input are set to. The encoder still emits 48 kHz AAC, so the output contract
is unchanged; ffmpeg does the conversion. Do not resample in Python - the pacer counts frames of the
*source* rate against wall clock, and a second rate to keep honest buys nothing.

**Two ports, and this is a security boundary.** The control API, WebSocket, settings and log live on
the control port (7788) and must never appear on the other one. Only the public port (7789) is handed
to cloudflared, and everything it serves sits under `/live/{streamKey}/`, is GET or HEAD only, and is
one of: the HLS playlist and segments, the listener page's own files out of `wwwroot/listen/`, `now`
(current track as JSON) and `art`. Everything else 404s, including traversal attempts. They are two
separate aiohttp applications on two runners, so the boundary is structural rather than a guard.

The public surface must never serialize the state snapshot. That snapshot carries
`namedTunnelToken` - a Cloudflare credential - along with dependency paths that leak the Windows
username and 200 log lines. `web/public.py` therefore declares its own now-playing record and builds
it field by field, so a field added to the panel's state cannot become public by being adjacent to
one. Title, artist, album and cover are already rendered into the video, which is why those are the
ones it may carry - and only *while the video exists*. The tunnel's lifetime is independent of the
encoder's (separate buttons, plus `autoTunnel`), so with the stream stopped `now` answers a fixed
off-air record and `art` 404s; otherwise anyone holding the hostname could poll what the machine
plays locally. Do not move that gate into the page: `streamKey` defaults to a constant, so the URL is
not a secret either.

Nothing on the wire carries a display placeholder. `NowPlaying` holds `""` for a field the source did
not report, and each of the three views - the panel, the video, the listener page - supplies its own
text. A sentinel like `"Nothing playing"` reaching the browser makes a panel-only string into
something the listener page has to string-match, and renaming it there would silently show it as a
track title.

`web/public.is_safe_name` guards every public filename. Rejecting `/`, `\` and `..` is not enough on
Windows: a path join discards its first argument for a drive-relative name, so `C:seg.ts` would
resolve against drive C's current directory. The listener's asset lookup is additionally confined to
the `listen/` subtree and to `.html`/`.css`/`.js`, or the control panel's own `index.html` and
`app.js` - siblings in the same folder - would be reachable through the tunnel. `art` is the one
public body whose type is *guessed* - `content_type_of` sniffs bytes a third-party app sent us and
falls back to `application/octet-stream` - so it is sent `nosniff`; it is same-origin with the page
out there.

Both public handlers answer HEAD as well as GET. A page link gets pasted into chat apps whose
unfurlers probe with HEAD first, and a HEAD that 404s where GET returns 200 makes the link look dead.
`_send` keeps the two identical by always attaching the body; aiohttp sets `Content-Length` from it
and drops the body for HEAD itself. Never set `content_length` by hand there - aiohttp raises.

**Cloudflare overrides `Cache-Control` on `.css` and `.js`.** Measured through the named tunnel, a
`no-cache` on those came back to the client as `max-age=14400`, so a response header cannot be relied
on to retire an asset: after a rebuild, listeners would run the previous build's script against this
build's feed for up to four hours. The page therefore names its assets `listen.css?v={v}` /
`listen.js?v={v}`, `web/public.py` substitutes a hash of their bytes into the page as it serves it,
and the assets are sent `immutable`. Only the page itself is `no-cache`, and it is `.html`, which
Cloudflare leaves as `DYNAMIC`. Bust by URL here, never by header.

`ArtworkStore.version` is a 63-bit int, so it goes to the listener page as a **string**: through JSON
a number past 2^53 is rounded by `JSON.parse` and the `art?v=` that comes back is not the version the
host sent. That endpoint compares `v` against the current version and serves `immutable` only when
they agree, `no-store` when they do not - the cover can change between the poll that named a version
and the fetch for it, and caching those bytes under the old key pins the wrong cover for a year (the
panel has the same shape, but it is push-driven over loopback, so its window is milliseconds). The
comparison is only sound because `ArtworkStore.current` reads version and bytes under one lock. The
panel still receives the version as a number: nothing validates it there.

## Invariants that are easy to break

**Pacing (the reason the stream never stalls)**
- A receiver delivers *nothing* while the sender is paused. `AudioPacer` therefore writes exactly one
  second of frames per second of wall clock, filling silence on underrun, and `VideoPacer` emits a
  fixed frame rate from the same `Clock`. Both demuxers derive timestamps from data received, so this
  is also what keeps A/V in sync. Never make either pacer emit on source activity.
- **The ffmpeg input writers need a raised high-water mark.** ffmpeg consumes each input in bursts
  while it interleaves and fills a segment, so a writer left at asyncio's default 64 KiB blocks in
  `drain()` for seconds at a time. The pacer reads the clock rather than its own progress, so a
  blocked write comes out as a long burst followed by filled silence - measured, four seconds of
  silence and three seconds shed per eight seconds of stream. `WRITE_BUFFER_LIMIT` is the buffer the
  named pipes had before the move to loopback sockets.
- A pacer only returns when it can no longer write, so its exit has to end the session - that is what
  `_run_pacer` wraps both in. Silence is the *designed* output for a paused source, which means a dead
  pacer looks identical to a quiet one from the outside - nothing else will notice. Several paths can
  report one fault (both pacers see the broken socket, then the encoder exits); `_stop_core` is a
  no-op once the session is gone, so only the first does anything.
- Teardown kills the encoder first so a blocked write faults, then waits for the pacers before
  aborting the writers and closing the servers under them. The wait is bounded.
- The receiver is *not* part of the session. It runs whenever selected and `push_audio` drops what it
  delivers while no session exists, so selecting a source and starting a stream stay independent.

**AirPlay**
- Apple Music and iTunes for Windows speak **AirPlay 1 (RAOP) only** - confirmed by shairport-sync's
  maintainer. No HomeKit pairing, no FairPlay, no SRP. Do not advertise AirPlay 2 keys (`ft`) or an
  `_airplay._tcp` service: a sender that sees them tries AirPlay 2 first and then fails.
- The six MAC bytes in the mDNS service name and the six signed into `Apple-Response` must be the same
  bytes, and the A record must be the LAN IPv4 rather than loopback - the sender connects to that
  address and it is also what gets signed.
- `Apple-Challenge` is signed with PKCS#1 v1.5 and **no hash**. `cryptography` has no primitive for
  an unhashed signature, so `keys.sign_challenge` builds the padded block and does the modpow itself.
- The AES payload is decrypted with the IV reset **per packet**, not chained, and only
  `len & ~0xF` bytes are encrypted - the tail rides in the clear.
- A sender front-loads about two seconds on RECORD. Handing that straight to the pacer would trip its
  600 ms shed cap and lose the start of every play, so `RtpSession` plays packets out on their own RTP
  timestamps with a 250 ms lead and only then feeds the pacer.
- When a hole in the sequence has nothing behind it, the sender stopped rather than dropped a packet:
  park the cursor and resume wherever it speaks again. Filling silence there instead would emit
  forever. A hole with later packets waiting is a real loss and does get silence, so the timeline
  stays honest.
- ffmpeg's ALAC decoder needs the **36-byte `alac` atom**, not the bare 24-byte body the SDP fmtp
  describes. `alac.magic_cookie` rebuilds it, and its output is byte-identical to what ffmpeg writes
  for its own ALAC files - which is how it was verified.
- The TXT record offers uncompressed audio as well (`cn=0,1`), so `PcmDecoder` has to exist; a sender
  that takes it would otherwise crash the session on an ALAC decoder it never announced.
- A failed ANNOUNCE must release the session. Otherwise the connection stays the owner and the SETUP
  that follows runs against parameters that were rejected.
- Metadata arrives as DMAP over SET_PARAMETER. The Apple apps have been seen packing `artist — album`
  into the artist field with the album empty; that split only fires when the album is genuinely empty,
  because the result is burned into the outgoing video, and it logs when it does so it can be deleted
  once the behaviour is confirmed either way.

**Spotify**
- go-librespot's Windows build **stubs out the pipe backend**, and its WASAPI backend plays to the
  system default device with no way to select another. `vendor/go-librespot/` holds the one-file patch
  that implements the pipe there and the steps to build it; until that binary exists the Spotify
  source reports itself unavailable and Apple Music is unaffected.
- The named pipe instance must exist **before** the daemon starts, because go-librespot is the client
  and its open fails outright when nothing is listening.
- go-librespot closes the pipe on stop and on playback moving to another device, and reopens it on the
  next play. The reader is therefore a loop that survives any number of connect cycles; the pacer
  fills the gaps and never learns anything happened. Never close the pipe while streaming should
  continue - a write error makes the daemon emit `stopped` and stay stopped until the user presses
  play.
- `external_volume: true` keeps the broadcast at full scale; Spotify's slider is the listener's
  business, not ours. The AirPlay side ignores its `volume:` messages for the same reason.
- Password login is gone from Spotify. Credentials arrive by the desktop app handing off over
  zeroconf, and are persisted so later runs need no re-pick.

**Playback Device**
- This source identifies no app at all - it's WASAPI loopback on a Windows playback device the user
  picks, for routing something with no receiver of its own (a virtual cable, or an app's own output
  device) into the stream. No title, artist, artwork or transport control exist at the device level,
  so `track()` always returns an empty `TrackState` - never a placeholder like "Live audio", per the
  rule below that nothing on the wire carries one.
- Loopback taps the audio engine's already-mixed stream, so DRM is a non-issue here: whatever plays
  through Windows' normal audio stack is already decrypted PCM by the time it reaches this tap. This
  is also why it works for apps a receiver protocol never could (see `--test-receiver device`).
- A loopback device delivers **nothing at all**, not silence, while its render endpoint is idle -
  unlike a real hardware output, which keeps rendering a mixed silence. `stream.read()` on an idle
  virtual cable can go long stretches without producing a frame. This is not a bug to route around:
  the pacer's own silence-fill already covers exactly this case, the same as a paused AirPlay/Spotify
  sender.
- **The one place in the app that resamples in Python.** Every other source already delivers 44.1kHz
  s16le - that guarantee comes from the wire protocol itself (RAOP, go-librespot), which is exactly
  what "never resample, the pacer counts frames of the source rate against wall clock" is protecting.
  A loopback device carries no equivalent guarantee: it runs at whatever the audio engine actually
  negotiated (commonly 48kHz), and unlike a real capture device, loopback isn't reliably able to
  renegotiate to an arbitrary format on request - confirmed by testing a real device on this machine,
  which reported 48kHz regardless. `sources/device/receiver.py` captures at the device's own native
  rate/channel count and converts to 44.1kHz s16le stereo itself (`_Resampler`, a persistent
  linear-interpolation resampler that carries its fractional phase across `stream.read()` calls - a
  fresh interpolation per chunk would click at every chunk boundary) before anything downstream ever
  sees it, so the pacer's own assumption still holds everywhere else.
- **PyAudioWPatch**, not raw `ctypes`, despite `jobs.py` already establishing a no-`comtypes`
  convention elsewhere in this codebase. That precedent is two flat `WinDLL` calls with a struct;
  WASAPI loopback needs real COM interfaces (`IMMDeviceEnumerator`, `IAudioClient`,
  `IAudioCaptureClient`) with vtables and `QueryInterface`, which is a different order of complexity
  hand-rolled `ctypes` was never exercised against. PyAudioWPatch is a PortAudio fork built
  specifically for this and ships a `cp314-win_amd64` wheel.
- A device is persisted by **name**, not index - WASAPI device indices shift when devices are
  plugged/unplugged, and PyAudioWPatch's device info exposes no more stable identifier than the
  name, so this is the same tradeoff `receiverName`/`spotifyConnectDeviceName` already accept.
- **PortAudio snapshots the device list when it initialises**, so a device plugged in after launch is
  invisible to a long-lived instance forever. Measured on this machine: 75 ms to create an instance
  against 0.04 ms to walk its snapshot, and `available`/`reason` are polled about twice a second - so
  `DeviceReceiver` keeps one instance and recreates it only when the configured name is missing from
  it, throttled to `RESCAN_INTERVAL` and never while a stream is open on it. `list_devices()` (the
  settings dropdown) always uses a throwaway instance, so the list offered is never the stale one.
- Changing the configured device while "device" is already the selected source does not restart
  capture - consistent with existing behavior (`save_settings` only calls `sources.select()` when
  `settings.source` itself changes), not something new to solve here.

**DJ mixer - Rave mode and the learned library (`dj/library.py`, `dj/harvester.py`, `dj/key.py`)**
- Radio DJ (the default) only ever mixes in what's requested. Rave DJ additionally picks its own
  next track from a learned library once nothing's queued, instead of falling back to the captured
  app - but a real request queued after an autonomous pick still plays first (`PendingTrack.
  autonomous`, checked in `_start_next`'s READY-entry search). The pick itself only ever happens from
  `_evaluate_resume()`, the same place Radio DJ already decides whether to resume the captured app -
  Rave mode's whole design is "resuming would end the set," not a separate code path bolted on.
- The library needs no Apple Music API, no Spotify Web API, and no OAuth - both would have worked but
  needed a paid Apple Developer account or an OAuth app respectively, and a much simpler path exists
  by reusing capability that already ships here: `harvester.harvest_live_source` walks forward through
  whatever's already playing by sending the existing "next" transport command and reading whatever
  metadata arrives after it. Neither DACP nor go-librespot confirms a skip actually changed anything
  - both are fire-and-forget HTTP acks - so this polls `receiver.track()` after each skip and diffs
  against the previous title/artist, with a timeout standing in for "end of queue" (or a repeat-one
  loop) since neither protocol says so directly. Stops on: the starting track reappearing (a full
  loop), any track repeating, the timeout elapsing with no change, or a hard track-count ceiling.
- This is real playback: every skip actually advances Apple Music/Spotify, and `push_audio` would
  relay it to a running public stream. `push_audio` already drops everything while no stream session
  exists, so harvesting before going live needs no new gating code - the DJ window's "Learn" button
  just says to do it first, rather than this module enforcing it.
- `harvester.harvest_youtube_playlist` reads a playlist URL directly with the same `--flat-playlist`
  technique `fetch.py`'s search dropdown already uses - no API key, no OAuth, and unlike the live-
  source harvest it hands back a `video_id` per track immediately, skipping the search-resolution step
  live-harvested (title, artist) pairs need.
- **Pre-analysis fetches a ~30 second clip (`YtDlpFetcher.fetch_clip`, `--download-sections`), never a
  whole track.** Most candidates in a learned library never get played - fetching them in full to find
  out their key/tempo would be bandwidth spent on tracks nothing ever hears. A full fetch only happens
  if a track is actually picked, the same "fetch when needed" principle a real request already follows.
- **Analysis concurrency is bounded and tunable (`djLibraryConcurrency`), never unbounded.** yt-dlp
  downloading is already noted elsewhere in this file as ToS-adjacent; a hundred simultaneous requests
  reads as automated abuse to YouTube's own side and risks the whole DJ feature getting rate-limited,
  not just the library-building pass. `TrackLibrary.analyze_pending` bounds itself with an
  `asyncio.Semaphore`, matching the async-native style `fetch.py` already uses rather than raw threads.
- Key detection (`dj/key.py`) is a chroma profile against the standard Krumhansl-Schmuckler major/minor
  templates, expressed as a Camelot wheel position - that notation is what actually determines mixing
  compatibility (adjacent numbers, or the same number's other letter), not the raw key name. Verified
  against synthetic chords (exact key recovered, correct relative-minor Camelot pairing) but not yet
  against real, ambiguous material the way `beatgrid.py`'s own confidence threshold was calibrated
  against real tracks - key detection genuinely can be wrong on atonal or percussion-heavy material,
  and `pick_next` should be re-measured against real picks before trusting it blindly.
- **Nothing in `dj/` is touched from a receiver thread, so none of it takes a lock.** `mix()` runs on
  the pacer's tick, fetching/analysis/harvesting are loop-scheduled tasks, and the web handlers and
  the frame renderer are on the loop too - every entry point is the one thread. Adding a call from a
  receiver thread (the obvious candidate being "feed the live audio from `push_audio`") puts the lock
  back and is not what the rolling grid wants anyway - see the next point.
- **The live beat grid is fed from `mix()`, not from `push_audio`.** The anchor it publishes is
  compared against `_stream_frames`, so the buffer it is read from has to be on that same timeline:
  `push_audio` sees audio before the pacer's ~200 ms jitter reserve, which would leave every drop a
  fraction of a beat early. `_remember_live` therefore appends the very chunk being written, and
  `_refresh_live_grid` reads the playhead together with the window, before `analyze()` takes any time.
- **A cued transition starts in the future, and the record on air has to keep playing until it does.**
  `_start_frame` is up to a phrase (2-6 beats) ahead of the block that scheduled it, so `mix()` plays
  the departing deck over `[0, active_start)` rather than falling through to the live source - which
  is paused by then, so the fall-through was measured as 2.5 s of digital silence before every
  deck-to-deck handover.
- **The captured app is resumed during a track's own outro, not when the deck retires.** The envelope
  ramps live audio back in over the closing beats; `mix()` therefore drives `_set_source_paused` from
  what the envelope actually wants each block, and a resume at retirement would crossfade into a
  source that is still paused.
- A request that fails to fetch, or that audition rejects, leaves the queue and is marked unplayable
  in the library. Both matter: a terminal entry left in the queue is a row nothing ever retires and a
  count `_phase_text` keeps reporting, and in Rave mode the replacement picked for the failure is the
  same track again - `pick_next` is deterministic and nothing else about its inputs has changed.

**Serialization and background tasks**
- Never put a non-finite `float` into anything serialized. `state.dumps` passes `allow_nan=False`, so
  a mistake raises here instead of emitting JSON a browser silently rejects (see `LevelMeter.read`,
  which returns `None` rather than `-inf`).
- The hub is mutated from receiver threads as well as the loop, so broadcasts are scheduled with
  `call_soon_threadsafe` and each client has its own queue - a mutation never blocks on a slow socket.
- Detached tasks must log their exceptions. Anything swallowed here is invisible and presents as a
  frozen UI.
- Logging must never throw. The Windows console cannot encode every track title, so the print is
  guarded; prefer plain quotes over typographic ones in log and status strings.
- Nothing in `app._shutdown` may throw. It runs after the window has gone and there is no handler
  above it. Each step is attempted independently, because skipping the rest would leave ffmpeg,
  cloudflared or go-librespot running and the stream publicly live.
- Every child process is adopted into the job object in `jobs.py`, whose handle must stay open for the
  life of the app - closing it is what kills them. That is the only cover for an End task or a crash,
  where no teardown of ours runs; measured, a killed app otherwise leaves cloudflared serving the
  tunnel indefinitely.

**Encoder**
- `-analyzeduration 0` with a small `-probesize` is load-bearing. ffmpeg's default 5 s probe does not
  drain the audio input; the paced writer fills the buffer, blocks, and sheds seconds of audio. The
  video input's probesize must still admit one whole JPEG.
- The HLS muxer reports `bitrate=N/A`, so the delivered bitrate is measured from segment file sizes in
  `hls.measure_bitrate_kbps`.
- Output targets AVPro/VRChat: muxed mpegts, 1 s segments, one keyframe per second, `main` profile,
  limited colour range. Separated audio/video tracks are a known VRChat failure mode. JPEG input is
  full-range and will otherwise leak out as `yuvj420p`. libx264 warns that 1280x720 exceeds the level
  3.1 limits it is given; that is the tuned combination and the warning is expected.
- The cover renderer caches the composed ground (blurred backdrop plus art) per artwork version and
  redraws only the text over it. The blur is most of the frame cost - 77 ms against 6 ms for a text
  redraw - and only the progress and track fields change between frames.

## Control panel (`wwwroot/`)

`styles.css` is the Industry design system, vendored **verbatim** from a Claude Design export - do
not edit it. Put every override in `app.css`. Its "blueprint" pass at the end of the file overrides
earlier rules, which causes two traps worth knowing:

- `.card, .dialog { background: transparent }` makes a modal see-through; the settings dialog paints
  its own ground.
- `.field > label` (the field caption rule) outranks `.radio`, so a checkbox nested in a `.field`
  loses its flex layout and its dot collapses to its borders.

`app.js` binds declaratively: every `[data-bind="x"]` element receives `view.x`. A binding value may
carry a `style` that is either a declaration *string* or a property object - `apply()` handles both,
because passing a string to `Object.assign` throws and silently aborts the remaining bindings.

**Theming.** The system ships one light palette, so dark mode restates its tokens in `app.css`. The
accent ramps are reversed there - `--color-accent-100` is the darkest step - because every pairing in
the system reads one end as a ground and the other as ink (`.tag-accent`, the status tags in
`buildView`), and only reversing keeps those legible. The neutral ramp is left alone: its only uses
are the modal scrim and the inactive dots, which want the same value either way. A theme of `Auto`
sets no `data-theme` at all, so the `prefers-color-scheme` block paints it - which is also what makes
the first frame right before `app.js` has read the setting, and why that block is the token list a
second time.

The window is an ordinary framed window, so the page owns none of the chrome. `ui.ground` paints what
shows before the first paint, resolving `Auto` against the registry's `AppsUseLightTheme`. The page no
longer posts anything to the host, and the body scrolls rather than growing the window when the
details drawer opens.

## Not yet verified

The **Spotify** path has never run end to end: it needs the patched go-librespot binary described in
`vendor/go-librespot/README.md`, and no release carries it yet. Everything up to that binary - the
config, the process wrapper, the named pipe reader, the API client - is written and the pipe reader
is verified against synthetic writers, including reconnect cycles.

The **AirPlay** path is verified end to end against a synthetic RAOP sender that performs the real
handshake and streams real AES-encrypted ALAC: challenge signing, key unwrap, SETUP, RECORD, DMAP
metadata, JPEG artwork, progress, and decoded PCM all check out. It has **not** yet been driven by
Apple Music itself; the service is confirmed discoverable over mDNS on the development machine, but
whether Apple Music for Windows lists it, and whether it advertises `_dacp._tcp` so the transport
buttons work, are unconfirmed. The panel keeps working without DACP - only the transport buttons go
quiet.

The **Playback Device** source is verified piecewise, not against real content end to end. Confirmed
on the development machine: device/loopback enumeration finds a real virtual cable, `start`/`stop`/
`available`/`connected` behave correctly against it via `--test-receiver device`, and - checked in
isolation with a synthetic tone through `_Resampler` directly, not through a live capture - the
resample carries exact frame counts (no drift) and no chunk-boundary discontinuity beyond what a
smooth waveform's own slew rate accounts for. What is **not** yet confirmed: the exact output pitch
of real audio captured live and resampled end to end - an attempt to verify this by also generating a
test tone through the same virtual cable's playback side hit unrelated WASAPI shared-mode output
timing issues in the test harness itself (a near-silent, wrong-frequency capture even before the
resampler saw the audio), which point at the harness rather than `_Resampler` given the isolated test
already confirmed it exactly preserves frequency, but this was not run to ground. Route a real app's
output to a real device and listen to the resulting stream before trusting pitch on real content.

**The DJ mixer's request path is verified end to end.** With the Playback Device source selected and
the stream running, a real request was fetched, auditioned, beat-read and mixed, and the published
HLS segments carried it (peak 0.96, RMS 0.23, no silence). The mix path itself is verified by driving
`DjMixer.mix()` with synthetic click tracks: the live anchor lands on the source's beat grid and stays
there as the stream lengthens, a deck-to-deck handover leaves no silent gap, the captured app is
paused and resumed with seconds of lead before the hand-back, a permanently failing Rave pick is tried
once rather than looped on, and the rewritten `Biquad` matches the direct-form-I original to 1e-12.

**Rave mode's autonomous picking is still only verified against a stubbed library**, and the harvester
against a mocked receiver: key detection recovers the exact key (and correct relative-major/minor
Camelot pairing) on synthetic chords, `pick_next` prefers an adjacent key and close tempo and excludes
recent and unplayable tracks, and `harvest_live_source`'s stop conditions (the starting track
reappearing, any repeat, a timeout with no change) behave against mocked timing. **Not yet run against
an actual playing Apple Music or Spotify session** - the mocked timing may not match how quickly real
metadata arrives after a real "next", so re-verify `SKIP_TIMEOUT`/`SKIP_POLL_INTERVAL` against real
DACP/go-librespot before trusting the harvester's stop conditions on a real playlist.

The pipeline, both web surfaces, the tunnel, the frozen exe and the panel have been verified by
running them.
