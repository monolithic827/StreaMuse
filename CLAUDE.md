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
                 CoverFrameRenderer ──────> VideoPacer ─┤ (one shared StreamClock)
                                                        ▼
                        \\.\pipe\streamuse-{audio,video} ──> ffmpeg ──> %LOCALAPPDATA%\StreaMuse\hls
                                                                              │
                                          :7789 HlsEndpoint <── cloudflared <──┘
```

`StateHub` is the single source of truth. Everything the UI shows arrives in one snapshot pushed
over the WebSocket; the panel is a pure view and only ever posts intents back (start/stop, settings).
When adding UI data, put it in the snapshot rather than adding a poll endpoint.

**Two Kestrel ports, and this is a security boundary.** The control API, WebSocket and UI live on
the control port (7788). Only the public port (7789) is handed to cloudflared, and it serves
*nothing* but `/live/{streamKey}/*.m3u8|.ts` - everything else 404s, including traversal attempts.
Never widen what the public port serves, and never move an API onto it. Rejecting `/`, `\` and `..`
is not enough on Windows: `Path.Combine` discards its first argument for a drive-relative name, so
`C:seg.ts` would resolve against drive C's current directory. The name must also not be rooted.

## Invariants that are easy to break

**Build**
- The WebView2 package references its WPF assembly on every net5.0+ target, which wants
  `WindowsBase` 5.0.0.0. This is a WinForms app, so that is an unresolvable MSB3277 conflict against
  the runtime's 4.0.0.0 shim. `RemoveWebView2WpfReference` drops the reference; it has to be a
  target, because the package adds it from its own `.targets` after the project body. Don't fix it
  with `UseWpf`.

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
- `AudioSessionScanner` enumerates only the *default* render endpoint, so an app routed to a second
  device (a virtual cable, say) is invisible to it and drops out of election and the target list.

**Pacing (the reason the stream never stalls)**
- Process loopback delivers *nothing* while the source is paused. `AudioPacer` therefore writes
  exactly 48 000 frames per second of wall clock, filling silence on underrun, and `VideoPacer`
  emits a fixed frame rate from the same `StreamClock`. Both demuxers derive timestamps from data
  received, so this is also what keeps A/V in sync. Never make either pacer emit on source activity.
- A pacer only returns when it can no longer write, so its exit has to end the session. Silence is
  the *designed* output for a paused source, which means a dead pacer looks identical to a quiet one
  from the outside - nothing else will notice.
- Every teardown path in `StreamPipeline` holds `_gate`, including the one the encoder's `Exited`
  handler takes; `StopCoreAsync` is the ungated body for callers already holding it. Tearing down
  beside a running `StartAsync` leaves capture attached to a session that no longer exists.
- Teardown must `DrainSessionAsync` before disposing the pipes or the session's token source, or
  both outlive the pacers: a pipe disposed under a write, and a `Task.Delay` registering on a
  disposed source, each throw where nothing is watching. Kill the encoder first so a blocked write
  faults, and keep the drain bounded. Nothing a pacer calls back into during it may want `_gate` -
  the drain holds it - which is why `RunVideoAsync` and the encoder's `Exited` handler detach their
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
- `ProcessIdentity` caches those lookups per *process instance* - pid plus start time - because
  Windows recycles pids, and an entry outliving its process would hand one app's metadata to
  whatever inherited the number. A lookup that fails is never cached, or one unreadable poll would
  blank that process's name for the rest of the run.
- `TimelineProperties.Position` is not a live clock - apps push it only on play/pause/seek, so it
  must be extrapolated from `LastUpdatedTime`.
- `ArtworkStore.Version` is a hash of the bytes, not a counter, because it is also the cache key in
  `/api/art?v=`. That response is `immutable` for a year and WebView2's cache outlives the process,
  so a counter restarting at 1 each run served the panel the *previous* run's covers - while the
  video, which reads the bytes in-process, looked correct. Keep any cache key here content-derived.

**Serialization and background tasks**
- Never put a non-finite `double` into anything serialized. `System.Text.Json` refuses to write
  infinity, and because the telemetry loop is fire-and-forget the throw silently froze the meter and
  all stream statistics. Use `null` (see `LevelMeter.Read`).
- `StateHub` and `ControlApi` must keep matching `JsonSerializerOptions` - the panel takes its first
  paint from `/api/state` and every update from the socket, so enums have to look the same on both.
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

## Not yet verified

The Apple Music and Spotify **desktop app** branches have never run - neither app is installed on
the development machine, so those paths are written but unexercised. Everything else in the pipeline
has been verified end to end against real playback.

Window sizing has only been exercised on the development machine: one 2560x1440 monitor at 125% with
a bottom taskbar. The DPI and multi-monitor paths (`OnDpiChanged`, `Screen.FromControl`, a taskbar on
another edge) are written against the per-monitor APIs but unmeasured.
