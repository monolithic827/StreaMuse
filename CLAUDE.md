# CLAUDE.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this is

StreaMuse re-streams whatever a Windows app is playing as an HLS (`.m3u8`) stream and publishes it
through a Cloudflare tunnel. One .NET 10 `WinExe` hosts everything: ASP.NET Core, a WinForms
frameless window, and a WebView2 showing the control panel. Windows-only by design.

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
dotnet build src/StreaMuse/StreaMuse.csproj    # build
dotnet run --project src/StreaMuse             # build + run

StreaMuse.exe --probe                          # dump every audio session and SMTC media session
StreaMuse.exe --test-capture [seconds]         # record the resolved source to WAV, report drift
```

DJ mixing (`src/StreaMuse/Dj/`) is part of this one project now, not a separate build - turn on
"Enable DJ mixing" in Settings and restart if the stream is already running.

`.github/workflows/build.yml` builds the release exe: self-contained and single-file, so the whole
app ships as one file that runs without a .NET install. `IncludeNativeLibrariesForSelfExtract` is
what pulls `libSkiaSharp.dll` and `WebView2Loader.dll` inside; they load from
`%TEMP%\.net\StreaMuse\` at runtime. `wwwroot` gets there a different way - the bundler takes
assemblies, not content - so it is an `EmbeddedResource` served through a
`ManifestEmbeddedFileProvider` set as the web root, and `StaticWebAssetsEnabled` is off so the asset
manifest does not ship either. Editing the panel needs a rebuild for the same reason.

There is **no test project**. Verification is done by running the app and checking real behaviour;
the two diagnostic modes above exist for that. Useful techniques used in practice:

- `curl http://127.0.0.1:7788/api/state` - full state as JSON; poll it in a loop to catch flapping.
- Console errors in the panel: `msedge --headless=new --virtual-time-budget=4000 --dump-dom
  http://127.0.0.1:7788/` and grep stderr for `Uncaught`.
- Screenshot the frameless window with `PrintWindow(hwnd, hdc, 2)` - `PW_RENDERFULLCONTENT` is
  required to capture WebView2 content, and the capturing script must be DPI-aware.
- `--test-capture` answers "why is the stream silent" fastest: it reports peak level, how much
  silence was filled, and drift from wall clock.

## Architecture

```
DiscoveryService (1 Hz) ──> StateHub ──> WebSocket ──> control panel
      │  audio sessions + SMTC metadata
      ▼
SourceResolver ──> capture pid
      │
StreamPipeline:  ProcessLoopbackCapture ──> AudioPacer ─┐
                 CoverFrameRenderer ──────> VideoPacer ─┤ (one shared Stopwatch)
                                                        ▼
                        \\.\pipe\streamuse-{audio,video} ──> ffmpeg ──> %LOCALAPPDATA%\StreaMuse\hls
                                                                              │
                                          :7789 HlsEndpoint <── cloudflared <──┘
```

`StateHub` is the single source of truth. Everything the UI shows arrives in one snapshot pushed
over the WebSocket; the panel is a pure view and only ever posts intents back (start/stop, settings).
When adding UI data, put it in the snapshot rather than adding a poll endpoint.

**Two Kestrel ports, and this is a security boundary.** The control API, WebSocket, settings and log
live on the control port (7788) and must never appear on the other one. Only the public port (7789)
is handed to cloudflared, and everything it serves sits under `/live/{streamKey}/`, is GET-only, and
is one of: the HLS playlist and segments, the listener page's own files out of `wwwroot/listen/`,
`now` (current track as JSON) and `art`. Everything else 404s, including traversal attempts.

The public surface must never serialize `StateSnapshot`. That snapshot carries `NamedTunnelToken` -
a Cloudflare credential - along with dependency paths that leak the Windows username, pids, process
names and 200 log lines, and `StateHub.AcceptSocketAsync` sends all of it to any socket that
connects. `ListenerEndpoint` therefore declares its own `PublicNowPlaying` record and builds it field
by field, so a field added to the panel's state cannot become public by being adjacent to one. Title,
artist, album and cover are already rendered into the video, which is why those are the ones it may
carry - and only *while the video exists*. The tunnel's lifetime is independent of the encoder's
(separate buttons, plus `AutoTunnel`), so with the stream stopped `now` answers a fixed off-air
record and `art` 404s; otherwise anyone holding the hostname could poll what the machine plays
locally. Do not move that gate into the page: `StreamKey` defaults to a constant, so the URL is not
a secret either.

Nothing on the wire carries a display placeholder. `NowPlaying` holds `""` for a field the source
did not report, and each of the three views - the panel, the video, the listener page - supplies its
own text. A sentinel like `"Nothing playing"` reaching the browser makes a panel-only string into
something the listener page has to string-match, and renaming it there would silently show it as a
track title.

Both public handlers share `HlsEndpoint.IsSafeName`, and it must stay shared. Rejecting `/`, `\` and
`..` is not enough on Windows: `Path.Combine` discards its first argument for a drive-relative name,
so `C:seg.ts` would resolve against drive C's current directory. The name must also not be rooted.
The listener's asset lookup is additionally confined to the `listen/` subtree and to
`.html`/`.css`/`.js`, or the control panel's own `index.html` and `app.js` - siblings in the same
embedded provider - would be reachable through the tunnel. `art` is the one public body whose type
is *guessed* - `ContentTypeOf` sniffs bytes a third-party app handed to SMTC and falls back to
`application/octet-stream` - so it is sent `nosniff`; it is same-origin with the page out there.

`ListenerEndpoint` is mapped before `MapPublicHls` because that one terminates for the public port
and never calls `next()`. It passes anything it does not own through to that 404.

Both public handlers answer HEAD as well as GET. A page link gets pasted into chat apps whose
unfurlers probe with HEAD first, and a HEAD that 404s where GET returns 200 makes the link look dead.
`ListenerEndpoint.SendAsync` is what keeps the two identical: it sets `ContentLength` from the body
either way and returns before writing it for HEAD. Widening the accepted methods does not widen the
surface - the route allowlist runs first - but re-check the 404 matrix under both methods if it
changes again.

**Cloudflare overrides `Cache-Control` on `.css` and `.js`.** Measured through the named tunnel, a
`no-cache` on those came back to the client as `max-age=14400`, so a response header cannot be relied
on to retire an asset: after a rebuild, listeners would run the previous build's script against this
build's feed for up to four hours. The page therefore names its assets `listen.css?v={v}` /
`listen.js?v={v}`, `ListenerEndpoint` substitutes a hash of their bytes into the page as it serves
it, and the assets are sent `immutable`. Only the page itself is `no-cache`, and it is `.html`, which
Cloudflare leaves as `DYNAMIC`. Bust by URL here, never by header.

`ArtworkStore.Version` is a 63-bit long, so it goes to the listener page as a **string**: through
JSON a number past 2^53 is rounded by `JSON.parse` and the `art?v=` that comes back is not the
version the host sent. That endpoint compares `v` against the current version and serves `immutable`
only when they agree, `no-store` when they do not - the cover can change between the poll that named
a version and the fetch for it, and caching those bytes under the old key pins the wrong cover for a
year (the panel has the same shape, but it is push-driven over loopback, so its window is
milliseconds). The comparison is only sound because `ArtworkStore.Current` reads version and bytes
under one lock. The panel still receives the version as a number: nothing validates it there.

## Invariants that are easy to break

**Build**
- The WebView2 package references its WPF assembly on every net5.0+ target, which wants
  `WindowsBase` 5.0.0.0. This is a WinForms app, so that is an unresolvable MSB3277 conflict against
  the runtime's 4.0.0.0 shim. `RemoveWebView2WpfReference` drops the reference; it has to be a
  target, because the package adds it from its own `.targets` after the project body. Don't fix it
  with `UseWpf`.
- `InvariantGlobalization` must stay off. WinForms handles `WM_INPUTLANGCHANGE` by resolving the new
  keyboard layout's LCID through `CultureInfo.GetCultureInfo`, which throws in invariant mode for
  anything but the invariant culture, so switching to a Hebrew layout while the window had focus
  killed the message loop with an unhandled exception. .NET uses Windows' own ICU, so nothing extra
  ships for it.

**Window chrome**
- The form is resizable only because `CreateParams` adds `WS_THICKFRAME` back to a `FormBorderStyle.
  None` window. A `WM_NCHITTEST` of our own cannot work: the WebView2 child window covers the whole
  client area and takes the mouse first. Keeping the real frame is also what keeps sizing, snapping
  and maximize on Windows' own code path, so do not strip it again with `WM_NCCALCSIZE`.
- The title bar's double-click is read from `mousedown`'s `detail`, not from a `dblclick` listener:
  the first click hands the mouse to the window manager's modal move loop, which consumes the
  mouse-up, so the page never sees the pair of clicks a `dblclick` needs.
- Windows maximizes a caption-less window to the whole monitor, not the work area, so it covers the
  taskbar. `WM_GETMINMAXINFO` restates the maximize rect against `Screen.WorkingArea`, inflated by
  the frame exactly as Windows inflates its own. Let the base handler run first - it owns the min
  track size that enforces `MinimumSize` - and rewrite only the two maximize fields.
- That frame is not accounted for by a `ClientSize` set in the constructor, and WinForms adjusts the
  height again during load. `MainWindow.OnLoad` therefore restates `ClientSize`, and `OnShown` reads
  the resulting outer size back as the minimum. Setting either earlier silently opens the page
  smaller than 1080x820.
- The minimum is captured from the opening size and stored with the DPI it was measured at, so it
  scales with the window rather than being recomputed through logical units - a round trip through
  those truncates and loses a pixel off the floor. It is also capped at what maximizing would give,
  or on a screen shorter than the window the bottom edge ends up permanently out of reach.
- The window's minimum height is `1080x820` plus the details pane while it is open, because the pane
  is laid out below everything else. Only the page can measure the pane, so it posts `detailsHeight`
  on every change and the host converts CSS pixels with `LogicalToDeviceUnits`. The window gives back
  height on close only up to what it grew by, or closing the pane resizes a window the user sized.

**Capture**
- `NAudio.Wasapi` must stay on a `3.0.0-preview` version. Process loopback (`WithProcessLoopback` +
  `BuildAsync`) does not exist in stable 22.x. Do not "upgrade" it.
- `WithFormat` is mandatory on that path: `GetMixFormat` returns `E_NOTIMPL` for process loopback,
  so there is no format to negotiate.
- NAudio hands over a span onto the WASAPI buffer valid only for the callback - copy before it
  leaves the capture thread.
- Process loopback taps the session *after* the audio engine has applied its per-app volume, so the
  Windows mixer's slider and mute reach the stream. Nothing on our side can undo it: a mute delivers
  zeros. Playing silently locally means routing the app to another output device, not muting it.
- `AudioSessionScanner` enumerates *every* active render endpoint, not just the default one, or an
  app routed to a second device (a virtual cable, say) drops out of election, the target list and
  the status text. Capture is endpoint-independent, so this is discovery only; one process can
  appear once per device it renders to, which is why callers group by root pid.

**Pacing (the reason the stream never stalls)**
- Process loopback delivers *nothing* while the source is paused. `AudioPacer` therefore writes
  exactly 48 000 frames per second of wall clock, filling silence on underrun, and `VideoPacer`
  emits a fixed frame rate from the same `Stopwatch`. Both demuxers derive timestamps from data
  received, so this is also what keeps A/V in sync. Never make either pacer emit on source activity.
- A pacer only returns when it can no longer write, so its exit has to end the session - that is
  what `RunPacerAsync` wraps both in. Silence is the *designed* output for a paused source, which
  means a dead pacer looks identical to a quiet one from the outside - nothing else will notice.
  Several paths can report one fault (both pacers see the broken pipe, then the encoder exits);
  `StopCoreAsync` is a no-op once the session is gone, so only the first does anything.
- Every teardown path in `StreamPipeline` holds `_gate`, including the one the encoder's `Exited`
  handler takes; `StopCoreAsync` is the ungated body for callers already holding it. Tearing down
  beside a running `StartAsync` leaves capture attached to a session that no longer exists.
- Teardown must `DrainSessionAsync` before disposing the pipes or the session's token source, or
  both outlive the pacers: a pipe disposed under a write, and a `Task.Delay` registering on a
  disposed source, each throw where nothing is watching. Kill the encoder first so a blocked write
  faults, and keep the drain bounded. Nothing a pacer calls back into during it may want `_gate` -
  the drain holds it - which is why `RunPacerAsync` and the encoder's `Exited` handler detach their
  stops.

**ffmpeg**
- Two named pipes, not stdin: stdin carries one stream, and keeping stdout free lets `-progress
  pipe:1` deliver structured stats while stderr stays for log lines.
- `-analyzeduration 0` with a small `-probesize` is load-bearing. ffmpeg's default 5 s probe does
  not drain the audio pipe; the paced writer fills the pipe buffer, blocks, and sheds seconds of
  audio.
- The HLS muxer reports `bitrate=N/A`, so the delivered bitrate is measured from segment file sizes
  in `HlsOutput.MeasureBitrateKbps`.
- Output targets AVPro/VRChat: muxed mpegts, 1 s segments, one keyframe per second, `main` profile,
  limited colour range. Separated audio/video tracks are a known VRChat failure mode. JPEG input is
  full-range and will otherwise leak out as `yuvj420p`.

**Source selection and metadata**
- Apple Music and Spotify mean the *desktop app* and are only offered when that process exists;
  everything else is `External`. Nothing in Windows reports which site a browser tab is playing, so
  never label a captured browser as a specific service.
- Apple Music plays through `AMPLibraryAgent.exe`, not the window's `AppleMusic.exe`. The agent is
  started by svchost, so it is not in the app's process tree either and `IncludeTargetProcessTree`
  never reaches it: capturing the process behind the window records pure silence. Availability still
  keys on `AppleMusic.exe` - the agent outlives the window - but the capture target must be the
  agent, which is why `AppleAudioProcessNames` is a separate list and lists it first.
- Apple Music does not fill `AlbumTitle` at all. It reports a space, em dash (U+2014), space - in
  *both* `Artist` and `AlbumArtist`, so there is no clean field to read and
  `SourceResolver.SplitAppleArtist` has to split the string. It splits on the first separator, since
  an album is likelier to contain one than an artist, and only for Apple app ids: no other app's
  format is known, and a guess here is burned into the outgoing video. Spotify fills both fields
  properly, and every other app is `External`, where the fields are whatever that app chose.
- `ManualProcessId` persists across runs while pids are recycled, so a manual pid that is not in
  the process snapshot resolves as *not detected* rather than as a target that fails at attach time.
- Auto target election prefers whichever candidate SMTC reports as *playing*. Do not elect on
  `MasterPeakValue`: it is an instantaneous sample, so every gap in the audio flips the target,
  which blinks the metadata and reattaches capture mid-stream. Loudness is a fallback only, and the
  incumbent is held through short silences. The app's own WebView2 is excluded from candidates.
- Metadata is only used when its SMTC session belongs to the captured process (`ProcessIdentity`).
  A wrong title is burned into the outgoing video, so "no track info reported" is the correct
  answer when nothing matches.
- Matching app id to process is not name comparison. Chromium forks run as `chrome.exe` but report
  e.g. `Helium.E2M2QCR2GB7Q3BNR56GK5FAO5Y`; the executable's product name and file description
  bridge the two.
- Discovery takes one `ProcessTree.Snapshot()` per poll and derives every process name and parent
  from it. `Process.GetProcessById` enumerates the whole process table on each call, so nothing on
  the poll path should open a `Process` except `ProcessIdentity`, which needs the start time and
  version resources.
- `ProcessIdentity` caches those lookups per *process instance* - pid plus start time - because
  Windows recycles pids, and an entry outliving its process would hand one app's metadata to
  whatever inherited the number. A lookup that fails is never cached, or one unreadable poll would
  blank that process's name for the rest of the run.
- `TimelineProperties.Position` is not a live clock - apps push it only on play/pause/seek, so it
  must be extrapolated from `LastUpdatedTime`.
- Every SMTC call runs on `MediaThread`. The session proxies are bound to the thread that created
  their manager, and an `await` inside a read - a thumbnail especially - resumes on a different pool
  thread, after which every remaining session throws `RPC_E_WRONG_THREAD` and is silently dropped
  for that poll. Retrying does not help: the retry runs on the same wrong thread. Do not move this
  work back onto the pool, and do not `await` SMTC anywhere else.
- Artwork does not arrive with the title. A track the app has never played is still being fetched,
  and until it lands the session reports *no thumbnail at all* rather than one that fails to read.
  It is chased for `ArtworkPolls` polls after a track change and taken as soon as it appears; the
  previous cover is dropped on the first empty read, or the video pairs the new title with the old
  album for as long as the fetch takes.
- `ArtworkStore.Version` is a hash of the bytes, not a counter, because it is also the cache key in
  `/api/art?v=`. That response is `immutable` for a year and WebView2's cache outlives the process,
  so a counter restarting at 1 each run served the panel the *previous* run's covers - while the
  video, which reads the bytes in-process, looked correct. Keep any cache key here content-derived.
  It is also forced odd, so it can never be 0 - the panel reads 0 as "no artwork".

**Serialization and background tasks**
- Never put a non-finite `double` into anything serialized. `System.Text.Json` refuses to write
  infinity, and because the telemetry loop is fire-and-forget the throw silently froze the meter and
  all stream statistics. Use `null` (see `LevelMeter.Read`).
- `ControlApi` serializes with `StateHub.Json` - the panel takes its first paint from `/api/state`
  and every update from the socket, so both must look the same.
- Detached tasks must log their exceptions. Anything swallowed here is invisible and presents as a
  frozen UI.
- Nothing in `Program.Shutdown` may throw. It runs after `Application.Run` returns, so the window is
  gone and there is no handler above it: a throw there is a crash dialog, not a message. It is also
  the *only* caller that surfaces a teardown fault at all - the same fault from the panel's stop
  button lands in an HTTP response nobody reads. Each step is attempted independently, because
  skipping the rest would leave ffmpeg or cloudflared running and the stream publicly live.
- `MainWindow` closes with `BeginInvoke(Close)`, never a bare `Close()`. The command arrives on a
  WebView2 message callback, and closing disposes the WebView2 that is mid-dispatch.
- Every child process is adopted into `ChildProcessJob`, whose handle must stay open for the life of
  the app - closing it is what kills them. That is the only cover for an End task or a crash, where
  no teardown of ours runs; measured, a killed app otherwise leaves cloudflared serving the tunnel
  indefinitely. ffmpeg happens to die on its own when its pipes break, but do not rely on it.

## The DJ window (`App/DjWindow.cs`, `wwwroot/dj.html`)

The decks are a second top-level window, not a dialog inside the panel, so both can be watched at once.
It is frameless for the same reason `MainWindow` is - the page draws its own title bar and posts
`minimize`/`close`/`drag` back - but carries none of the details-pane sizing, since nothing in it grows
the window.

- Both windows share one `CoreWebView2Environment`. `MainWindow` keeps the one it creates and hands it
  over; two environments over a single user data folder is a documented conflict, and sharing is what
  WebView2 expects for additional windows in a process.
- Closing hides rather than disposes (`OnFormClosing` cancels a user close), so reopening skips
  WebView2 startup and the page keeps its socket. The real teardown is `MainWindow.Dispose`.
- `dj.js` is deliberately separate from `app.js` rather than shared: `app.js` binds elements this page
  does not have, and would throw partway through `render` on the first snapshot. Both take the same
  snapshot from the same `/ws`; the DJ page reads only `state.dj`.
- The panel keeps no DJ state of its own. `buildDjView` reduces to whether a plugin is loaded, which is
  all the button that opens this window needs.
- To drive either window in a test, UI Automation reaches into WebView2 content: find the window by
  name, then elements by `AutomationId` (the DOM id). `InvokePattern` fires the page's own click
  handlers, and `ValuePattern.SetValue` fills inputs - that is how the request path in this window was
  verified end to end. Note a `PrintWindow` screenshot of a WebView2 window renders the page at logical
  size into a device-pixel bitmap, so on a scaled display the capture looks cropped when the window is
  fine.

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
second time. WebView2 follows the Windows app theme for that query with no host setting.

The window behind the page is not covered by any of it: `MainWindow.ApplyTheme` paints what shows
before the first paint and wherever WebView2 lags a resize. The page posts every change, so the host
only resolves the setting itself - `Auto` against the registry's `AppsUseLightTheme` - for the frames
before the page has loaded.

## DJ mixing (`src/StreaMuse/Dj/`)

Built into the main app - a plain `StreaMuse.Dj.DjAddon` instance constructed once in `Program.cs` and
handed around as a singleton (`StreamPipeline`, `CoverFrameRenderer`, `ControlApi` all take it as a
constructor parameter), not a plugin loaded from disk. It used to ship as a separately-built DLL
(`StreaMuse.DjAddon.dll`) loaded at runtime via `AssemblyLoadContext` into a `%LOCALAPPDATA%\StreaMuse\
plugins` folder, alongside a generic plugin-install mechanism (`POST /api/plugins/install`, a Settings
UI panel); both were removed once the DJ was the only thing that ever used them; `DjAddonEnabled` in
Settings is now the only on/off switch, checked live by `DjAddon.RequestAsync`/`StreamPipeline.
MixBlock` rather than by whether anything is loaded at all. The class still takes an `Initialize
(DjAddonContext)` call rather than doing setup in a constructor - a leftover from the plugin-contract
shape that was harmless to keep, since `DjAddonContext` (paths/settings/callbacks bundled once at
startup, `Media/DjAddonContext.cs`) is still a clean way to hand it what it needs from the rest of the
app without a direct dependency on `AppSettings` or `DiscoveryService`.

- The `/api/dj/*` routes are mapped unconditionally now, with no null-check needed - `dj` is always a
  live instance from the moment the app starts, so a request while mixing is disabled reaches
  `RequestAsync`, which itself returns `Accepted: false` with "DJ mixing is turned off in Settings"
  rather than the route 404ing.
- **`Mix` hangs off the audio pacer, never off the capture callback.** `StreamPipeline.MixBlock` is
  handed to `AudioPacer.RunAsync`, which applies it to each paced block just before writing it, so a
  DJ track advances on wall clock. Wiring it to `capture.SamplesAvailable` instead - the obvious place,
  and where this started - is silently broken by the pacing invariant above: process loopback delivers
  *nothing* while the source is quiet, so the deck froze exactly when the live source went silent,
  which is the one case it exists to cover. Measured: with capture pointed at a silent process the
  stream sat at -91 dB (digital silence) under the old wiring and carries the track at -25 dB under
  this one. `Mix` still must not block - fetching, decoding and retiming all happen ahead of time on
  background tasks, and it only does a per-sample gain blend against already-decoded PCM. A throw
  passes the block through unmixed rather than taking the stream down; with nothing queued it returns
  its input unchanged, so the addon stays invisible to the rest of the pipeline.
- Every request is fetched the same way, regardless of the active capture source: `yt-dlp`
  search-and-download (`ytsearch1:`), decoded through ffmpeg to raw interleaved float32 PCM. `--print`
  implies `--simulate` in yt-dlp unless `--no-simulate` is also passed - without it, `YtDlpFetcher`
  fetched the title and downloaded nothing at all, reporting success right up until no file existed
  on disk. There is deliberately no Spotify (or other streaming-service) integration for fetching
  audio: the addon only ever downloads and mixes in audio itself. It does pause/resume Apple Music or
  Spotify around a request through the OS media transport controls (see below) - that is playback
  *control*, not a streaming integration, and it never reaches into either app beyond that.
- Nothing reaches the stream unauditioned. `Mixing/TrackAudition.cs` is the pre-listen a DJ does in
  headphones: it rejects a download that is too short or effectively silent - a video with an empty
  audio track, a fetch that "succeeded" into noise - and finds where the music actually starts so the
  mix does not crossfade the live source out into an intro of near-silence. A rejected track is logged
  with the reason and never queued; the alternative is discovering it as dead air on the live stream.
- `BeatDetector` wraps **SoundTouch's `BpmDetect`** (`SoundTouch.Net`, a managed rewrite, so nothing
  native ships beside the plugin). Three detectors were hand-rolled before it - energy flux, a
  kick-band low-pass, then spectral flux over an FFT - and each had to be rebuilt once real music
  exposed it; the first two could not tell Nirvana from white noise. The library is the same idea done
  properly and, more importantly, exercised against far more material than a four-track test set. It
  also returns beat positions with a strength each, which is what a transition lands on.
- **Confidence is beat *spacing*, not beat strength** (`Regularity`). SoundTouch returns weak candidate
  beats when there is no beat present, and they are all similar, so any strength-relative test passes
  them: measured, white noise scored **100%**. Spacing separates cleanly - real beats arrive on a grid,
  spurious ones arrive whenever - and takes noise to 4%. Gaps are measured against the nearest whole
  multiple of the period so a missed beat does not count against the track.
- **The period is fitted to the beat positions, not taken from `GetBpm`** (`RefinePeriod`). The
  library's BPM figure is rounded enough to be ~0.2% out, which is nothing per beat and 17 ms across a
  sixteen-beat transition - the mix audibly drifts as it runs. A least-squares line through the beats
  takes that to 1.7 ms.
- A track's grid is read from its first `GridWindowSeconds`, not the whole file: averaged over four
  minutes, beat strength is diluted by intros and breakdowns (deadmau5 scored 3% across the file and
  63% on a window), and the opening is the part whose phase is actually entered on.
- The live grid is re-read every 2s from a rolling 8s window, fed by a `ConcurrentQueue` that `Mix`
  only enqueues into (O(1) on the audio path). It is the less trustworthy of the two - it tracks
  whatever someone else's app is playing, which may not be 4/4, may be ambient, may be mid
  tempo-change - so `TimeStretcher`'s `atempo` retime only runs when both sides clear the confidence
  gate. Below it the track plays at its native tempo with a fade, which is the honest answer.
- **The transition is a bass swap, not a crossfade** (`Mixing/DjTransition.cs`). Two tracks at half
  volume gives two basslines and two kicks at once, which is mud, and mid-fade everything sounds thin.
  Instead both decks stay at full level and the *low end* changes hands: the incoming track comes in
  high-passed (`Biquad`, 220 Hz) so it rides on top of the live low end, the bass swaps across on beat
  8 of 16, and only then does the live source walk out. The envelope is measured in beats, not seconds,
  because the swap has to land on a beat to sound deliberate. `SkipCurrent` moves the playhead into the
  closing transition rather than cutting, so a skip mixes out the same way the ending would.
- There are two decks. A request made while something is playing is **not** started on arrival - it
  waits for its cue, which `Mix` checks once per block: a transition's length before the current track
  runs out, so the blend finishes as that track ends. `StartNext(handover: true)` then moves the
  playing track to `_outgoing` and brings the new one in over it; `handover: false` (a fetch finishing,
  a track ending) only starts when the decks are empty. Skip takes the same handover path immediately,
  which is the difference between "next, now" and waiting for the cue. The envelope is the same either
  way - whatever is leaving plays the "live" role in `DjTransition`, which is the captured source only
  when no other track is on its way out. A second handover is refused while one is in progress; that
  would need a third deck and would land off the grid.
- A handover is scheduled on the *outgoing track's* own phrase (`NextPhraseOnDeck`), not on the live
  grid and not at the instant the cue fires - coming in on a 4-beat boundary of the record that is
  leaving is what makes it land musically instead of mid-bar.
- Beat *phase* matters as much as tempo: `BeatDetector.Grid` carries where the beats fall, and
  `StartNext` schedules the drop on a 4-beat boundary of whatever it is mixing over while seeking the
  track to its own first beat, so beat one of the mix is beat one of both.
- A track's confidence is measured *before* any retime and kept: `atempo` smears transients enough to
  roughly halve it (Stayin' Alive read 100%, then 18% after a x1.178 stretch), so re-gating on the
  post-retime figure rejects exactly the tracks that were just matched successfully. The re-read after
  retiming is only for the new phase and period.
- **Both** sides of a handover must clear the confidence gate, not just the incoming one. A track can
  carry a BPM figure while barely registering a beat at all - Never Gonna Give You Up read 114 BPM at
  16% - and matching to that is guesswork that lands worse than an honest fade. Live scheduling anchors on
  `LastBeatFrames`, never `PhaseFrames` - the latter is a best fit across the whole window, and
  extrapolating it forward multiplied the period error by every beat in the window, measured at ~50 ms
  out (audible as a flam). Anchoring on the most recent onset plus parabolic interpolation of the
  autocorrelation peak brought that to 2.5 ms, with ~11 ms of drift across a full 16-beat transition.
- With no usable live beat grid - a silent or beatless source - there is nothing to lock to and nothing
  to swap, so the transition degrades to an equal-power fade over `CrossfadeSeconds` and says so in the
  log. Do not "fix" this by forcing the beatmatched path: a swap against a grid that isn't there lands
  wrong, which sounds far worse than a fade.
- `SoftClip` on the summed output is not optional. Keeping both decks at full level is the point of a
  bass swap, and downloads routinely arrive at or above 0 dBFS already (two of the test tracks peaked
  at +2.6 and +1.8 dB), so their sum clips hard without it - measured at -0.1 dB peak on the stream
  before it was added.
## Captured-app control (pause/resume)

While Apple Music or Spotify is the source, `DjAddon` pauses it once a DJ track has fully taken over
(`Mix`'s envelope shows `LiveGain <= 0` - live audio is genuinely unused from that point until the
current track's own outro needs it again) and resumes it as soon as it knows nothing is queued behind
whatever is playing. The two triggers are asymmetric on purpose: pause as *late* as possible - only
once live audio is truly not needed - resume as *early* as possible, since resuming ahead of when it's
needed costs nothing (the unused live samples are just multiplied by zero in the envelope until the
outro wants them) where resuming late means dead air right when it does. `DiscoveryService.
TryPauseSourceAsync`/`TryResumeSourceAsync` gate this to Apple Music and Spotify only - the two sources
that mean a specific desktop app rather than an arbitrary picked process - and reuse the exact same
`ProcessIdentity.Matches` session lookup that metadata reads use, so a transport command can never land
on the wrong app's session. A source that declines the command, or isn't Apple/Spotify at all, is
silently a no-op (`false`), which is also what lets the addon call these unconditionally without
checking the source itself.

## Mix's hot-path cost (`DjAddon.Mix`)

`Mix` runs on the audio pacer's 20ms tick - `AudioPacer.WriteIntervalMs` - which is ~50 calls a second
for as long as any track is playing, not just during a transition. Three things that look like fine
detail matter here specifically because of that frequency:

- **The output array is reused (`_outputBuffer`), not allocated fresh per call.** `AudioPacer.RunAsync`
  copies `Mix`'s return value out before the next call (`Array.Copy(mixed, scratch, ...)`), so nothing
  ever holds a reference to a previous call's contents - reusing one buffer across calls is safe by
  construction, not just in practice. A fresh array 50 times a second, for as long as a stream runs, is
  exactly the kind of avoidable GC pressure a must-not-block thread should not carry.
- **The bass-smoothing coefficient is computed once, in `Initialize`, not recomputed with `Math.Exp`
  every call.** Both of its inputs (`BassSmoothingMs`, `SampleRate`) are constant for the addon's whole
  life, so recomputing it was never buying anything - the value is always the same.
- **`MaybePauseSource` checks `_sourcePaused` unlocked before taking the lock.** Once a track reaches
  its steady body - the majority of its runtime - this method is called on nearly every block for
  minutes at a stretch, and after the first call there is nothing left to do. Taking `_sync` every time
  anyway is the same class of cost `EvaluateResume` had before it was gated to only run when a deck
  actually retired (see below); the field is `volatile` so the unlocked read is never arbitrarily
  stale, and the locked check underneath is still what actually decides - this only skips the lock,
  never the decision.

Do not reverse any of these to "simplify" the code without re-measuring: none of the three change what
`Mix` computes, only how much unnecessary work happens getting there.

## DJ track in the video (`CoverFrameRenderer.CurrentSource`)

The video frame shows the DJ's own track - its title/artist/album and `CurrentArtwork()` - whenever
`dj.Snapshot().NowMixing` is not null, falling back to the regular SMTC `NowPlaying`/
`ArtworkStore` otherwise. This is not just cosmetic: once a DJ track fully takes over, `DjAddon` pauses
Apple Music/Spotify (see above), so their SMTC metadata is frozen for as long as it plays - without
this the video would keep showing a paused track's cover while a different song is audibly playing.
`EnsureArtworkDecoded` takes explicit bytes/version parameters rather than reading `ArtworkStore`
directly, since the decoded-bitmap cache now has two possible sources for what counts as "the current
art" and has to invalidate on either one changing. A "REQUESTED" pill renders above the title in the
DJ case - a viewer otherwise cannot tell a request from whatever the stream would normally be playing,
since dropped-in audio looks identical in the frame - positioned in the gap above where the title's
own glyphs start; the title itself is pushed down slightly for the DJ case, since at the normal
position its cap-height visibly touched the pill above it.

## Where a track is cued in from (`BeatDetector.FindMixIn`, `PendingTrack.MixInOffset`)

A track does not enter at frame 0 of its (audition-trimmed) audio, or at the very first beat
`BeatDetector` finds - it enters at the first beat that begins a run of `MixInWindowBeats` (8, two
bars) consecutive well-spaced beats. That is a DJ's actual cueing decision: find where the groove has
locked in, not wherever the file happens to start. `FindMixIn` scans the same beat list `Regularity`
already has, using the same on-grid tolerance, so this costs nothing extra to compute and is always an
exact beat on the track's own grid - never an arbitrary timestamp, which matters because
`NextPhraseOnDeck`'s phrase-boundary math anchors on it (`MixInOffset`) directly. Falls back to the
very first beat when no `MixInWindowBeats`-long steady run exists in the analysed window (an ambient or
rubato opening) - entering there is still better than frame 0, and `MixInFrames` is 0 by construction on
`BeatDetector`'s empty result, so an unreadable track still degrades to "start at the top."

This applies whether or not the drop itself ends up beatmatched: even a plain fade sounds far better
starting on a steady beat than on whatever happened to be playing at frame 0 - a sparse intro fill, a
vocal ad-lib, silence. Verified live: entering "Stayin' Alive" mid-transition landed the deck at
position 0.602s, matching the logged mix-in point exactly, not 0.

## Sound effects (`Sfx/SfxLibrary.cs`)

A folder (`%LOCALAPPDATA%\StreaMuse\dj-sfx`) the user drops audio files into; nothing to configure per
file, no manifest. `DjAddon.TryTriggerSfx` rolls the dice once per transition (a probability plus a
cooldown that must elapse first - see the constants at the top of `DjAddon.cs` - so it reads as an
occasional accent rather than wallpaper), and on a hit, picks and decodes a file in the background
(`AudioDecoder.DecodeAsync`, shared with `YtDlpFetcher` - the same "arbitrary file in, PCM at the mix's
rate out" need) and schedules it for the same beat-aligned frame the transition itself is scheduled on.
Because decoding runs in the background after the roll, it can lose the race against a short lead time;
a result that arrives more than `SfxLateToleranceBeats` late is dropped rather than played off the cue
it was picked for.

Playback is a third voice mixed additively into `Mix`'s per-sample loop, ahead of `SoftClip` -
deliberately: modern masters already sit at or above 0 dBFS (see the bass-swap section above), so
layering a full-level effect on top without going through the same limiter the two decks share would
risk exactly the clipping that limiter exists to prevent. `SfxGainAt` ramps a linear fade over
`SfxFadeSamples` at both the start and end of the one-shot, since it can begin or end at any sample
of its own waveform.

## Not yet verified

iTunes is unexercised - it matches the same Apple branch as Apple Music but is not installed on the
development machine. Apple Music and the Spotify desktop app have both been verified end to end
against real playback, including pause/resume around a DJ request. Everything else in the pipeline has
been verified the same way.

Window sizing has only been exercised on the development machine: one 2560x1440 monitor at 125% with
a bottom taskbar. The DPI and multi-monitor paths (`OnDpiChanged`, `Screen.FromControl`, a taskbar on
another edge) are written against the per-monitor APIs but unmeasured.

The DJ addon's request → fetch → audition → crossfade path has run end to end against a real stream,
measured against a *deliberately silent* capture target (`ManualProcessId` pointed at an idle process,
verified at -91 dB) so that any audio in the output could only be the DJ track: a request for "Darude
Sandstorm" was fetched, auditioned ("232s, peak -9.0 dB, skipped 2.0s of intro, 136 BPM"), and carried
the stream at -25 dB, with skip fading it back out to -73 dB. Picking a silent target matters - an
earlier run of the same test against an idle-looking Discord was measuring Discord's own audio at
-23 dB, not the mix, and would have "passed" even with the deck completely frozen. Plugin install was
exercised the same way, through the real endpoint: `.dll` and `.zip` both install and activate, a
`.txt` is refused.

The mixing DSP is verified two ways, and it needs both. A throwaway harness runs synthetic click
tracks with silence and white-noise controls, because that is the only way to get a *known* ground
truth for tempo and beat position. **Synthetic tests alone were badly misleading**: every one of them
passed while the detector could not tell Nirvana from white noise, so the harness also measures four
real decoded tracks (Daft Punk, Bee Gees, Nirvana, deadmau5) whole-file and in rolling 8s windows. It
is the real-music numbers that drove every calibration constant in `BeatDetector` and `DjAddon`. Any
future change to onset detection or confidence must be re-measured against real tracks, not clicks.

Between them these caught the real bugs, all now fixed: confidence metrics that measured loudness, then
autocorrelation peakiness, then beat strength - the last of which scored white noise 100%; octave
correction walking to the 200 BPM ceiling on real music; beat phase ~50 ms out; period drift across a
transition; whole-file confidence diluted by intros; post-retime confidence rejecting tracks that had
just been matched; and the summed output clipping.

Where the detector stands after the switch to SoundTouch, measured on the same set: 128 BPM click read
exactly, phase within 7 ms, 1.7 ms drift over a sixteen-beat transition, noise at 4% against a
real-music median of 71%. It reads Nirvana at half-time and finds no tempo at all in Strobe's ambient
opening - but scores both near zero, so they fall back to a fade rather than mixing wrong. **It does
not handle very fast material**: a 192 BPM click comes back as its 64 BPM subharmonic at 0% confidence.
That is a safe failure, not a silent one, but hardcore and drum-and-bass will fade rather than mix.

Verified live end to end, against a deliberately silent capture target so the DJ audio could be
isolated: two requests, the second beat-matched over the first (Stayin' Alive stretched x1.178 from
104 to 122 BPM and dropped on the beat over Around The World) without waiting for it to finish.

Captured-app pause/resume is now verified against a real, playing Spotify session, and that testing
found a real bug: `Crossfader.EqualPowerGains(1.0)` returned a live gain of ~6.12e-17, not 0 -
`Math.Cos(Math.PI/2)` cannot land on an exact zero, since pi/2 has no exact binary representation, and
that residue survived the cast to float. `Mix` decides "live audio is genuinely unused, safe to pause"
by checking `LiveGain <= 0f`, so a tiny positive residue meant that check was never true through the
non-beatmatched fade path - pause silently never fired, for as long as a DJ track played, regardless of
duration. It looked exactly like "doesn't pause" because the residue rounds away to "0.000" in any log
or display, which is what let it pass an earlier round of testing undetected: a value can look correct
at three decimal places and still fail an exact comparison a few lines away. The beatmatched envelope
in `DjTransition` was unaffected only because it returns a literal `0`, not a trig result - that was
luck, not a property of the design, and does not generalise to a future envelope shape. Fixed by
special-casing the endpoints in `EqualPowerGains` rather than trusting `Cos`/`Sin` to reach them - see
the comment there. Re-verified end to end afterward: a DJ track paused a real, playing Spotify session
(confirmed via Spotify's own reported SMTC state going from `Playing` to `Paused`), and Skip - emptying
the queue - resumed it from the exact position it had been paused at, not from the start.

The sound-effect folder's mechanics - scan, random pick avoiding an immediate repeat, background
decode, beat-aligned scheduling, the additive mix ahead of the limiter - were verified with a synthetic
tone (band-isolated frequency analysis on a live segment, well above a control band), since no real
effect files exist yet. Real effects will differ in one way that matters: a synthesized tone has no
natural attack transient to preserve, so `SfxFadeSamples`' 5ms fade-in is untested against something
with a sharp onset (an air horn stab, a scratch) where a fade that slow might be audible as softening
the hit.

What is still unverified: a **live capture source with a real beat**. Every live run has used a silent
target, so the live-side rolling grid has never actually driven a transition - only track-to-track
handover has. The parts most likely to disappoint there are live-side beat tracking through whatever a
capture happens to contain, and whether a 16-beat blend holds alignment when the other side's tempo is
human rather than exact.
