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
carry.

Both public handlers share `HlsEndpoint.IsSafeName`, and it must stay shared. Rejecting `/`, `\` and
`..` is not enough on Windows: `Path.Combine` discards its first argument for a drive-relative name,
so `C:seg.ts` would resolve against drive C's current directory. The name must also not be rooted.
The listener's asset lookup is additionally confined to the `listen/` subtree and to
`.html`/`.css`/`.js`, or the control panel's own `index.html` and `app.js` - siblings in the same
embedded provider - would be reachable through the tunnel.

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

`ArtworkStore.Version` is a 63-bit long and reaches the page through JSON, where `JSON.parse` rounds
anything past 2^53 - so the `art?v=` the browser requests is not the version the host sent. It is
only ever an opaque cache key and change-detector, and two hashes would have to agree in their top 53
bits to stall a cover swap, so this is left alone deliberately. Anything that starts *validating*
`v` server-side has to send it as a string first.

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

## Not yet verified

The Spotify **desktop app** branch has never run - it is not installed on the development machine, so
that path is written but unexercised, as is iTunes, which the Apple branch also matches. Apple Music
itself has been verified end to end against real playback on the Store build
(`AppleInc.AppleMusicWin`). Everything else in the pipeline has been verified the same way.

Window sizing has only been exercised on the development machine: one 2560x1440 monitor at 125% with
a bottom taskbar. The DPI and multi-monitor paths (`OnDpiChanged`, `Screen.FromControl`, a taskbar on
another edge) are written against the per-monitor APIs but unmeasured.
