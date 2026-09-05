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
- `app._prepare` starts the receiver **before** `deps.ensure_all`. The receiver needs none of the
  downloads, and behind ~135 MB of them a first launch offers Apple Music no speaker to pick for
  minutes - which reads as the app being broken. ffmpeg and cloudflared are wanted later, by the
  stream and tunnel buttons, and both report their own absence.

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
- **A decoded plane is longer than the samples it holds.** ffmpeg allocates audio buffers with
  alignment padding, so `bytes(frame.planes[0])` returns 128 bytes more than
  `samples * 4` and every one of those bytes is zero. Sending the whole buffer appends silence to
  every packet: 0.8 % on the 4096-frame packets a test file produces, and **9 % on the 352-frame
  packets AirPlay actually sends**, which is both an audible buzz at the packet rate and a 9 %
  overrun the pacer then sheds continuously. Always cut a plane to `frame.samples`. The receiver's
  output is bit-exact against a reference decode, and that comparison is the test that catches this.
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
- **`go-librespot.exe` is resolved, never downloaded.** No release carries the patch, so a URL for it
  is a URL that 404s - which it did, on every launch, as a red error in the log of everyone using
  Apple Music. `DependencyManager.go_librespot` is a property over `resolve`, which also means a
  binary built and dropped into `BIN_DIR` needs no restart. Restore a download only against an asset
  that exists.
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

The pipeline, both web surfaces, the tunnel, the frozen exe and the panel have been verified by
running them.
