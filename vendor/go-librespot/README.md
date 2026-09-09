# The go-librespot pipe patch

StreaMuse reads Spotify audio as PCM from a Windows named pipe. go-librespot supports exactly that
on every platform except Windows, where `output/driver-pipe-stub.go` returns
`"pipe output is not supported on Windows"`; its WASAPI backend is the only other option and plays
to the system default device, which is neither ours to take over nor routable to us.

The Unix implementation only needs `os.OpenFile`. The `O_NONBLOCK` dance around it is FIFO
semantics that a Windows named pipe does not have, so the Windows version is the same function
without it.

## Producing the binary

`.github/workflows/go-librespot.yml` does this and publishes the result - the exe plus the DLLs it
links against - as `go-librespot-win-x64.zip` under a `go-librespot-{LIBRESPOT_REF}` tag, which is
where `deps.GO_LIBRESPOT_URL` downloads it from. Nothing triggers that workflow on an ordinary push,
so run it by hand after changing the patch or `LIBRESPOT_REF`; `deps.GO_LIBRESPOT_REF` has to name
the same upstream release, since it is what builds the URL.

It is upstream's own Windows job - `release.yml` in `devgianlu/go-librespot`, MSYS2 `MINGW64` with
`mingw-w64-x86_64-{gcc,pkg-config,libogg,libvorbis,flac,mpg123}` - with two steps inserted:
`driver-pipe-windows.go` copied to `output/`, and `output/driver-pipe-stub.go` deleted, because
both define `newPipeOutput`. Keep it in step with theirs.

`go-librespot.exe` alone is not enough - see CLAUDE.md's Spotify section for the DLLs it also needs
and why, which is why the published asset is an archive of the whole set rather than the exe.

Nothing has to be installed by hand: the app downloads that archive into
`%LOCALAPPDATA%\StreaMuse\bin` when the exe is not already there. To test a local build instead, put
it in that folder and it wins - `DependencyManager.go_librespot` resolves live, so a running app
picks it up with no restart and no download.

Open the change upstream. Once a go-librespot release carries it, point `GO_LIBRESPOT_URL` at their
asset, drop this workflow and delete this directory.
