# The go-librespot pipe patch

StreaMuse reads Spotify audio as PCM from a Windows named pipe. go-librespot supports exactly that
on every platform except Windows, where `output/driver-pipe-stub.go` returns
`"pipe output is not supported on Windows"`; its WASAPI backend is the only other option and plays
to the system default device, which is neither ours to take over nor routable to us.

The Unix implementation only needs `os.OpenFile`. The `O_NONBLOCK` dance around it is FIFO
semantics that a Windows named pipe does not have, so the Windows version is the same function
without it.

## Producing the binary

1. Fork `devgianlu/go-librespot` and branch from the `v0.9.0` tag.
2. Copy `driver-pipe-windows.go` here to `output/driver-pipe-windows.go`.
3. Delete `output/driver-pipe-stub.go` - both files define `newPipeOutput`.
4. Run the repository's own Windows build (`windows-2022`, MSYS2 `MINGW64` with
   `mingw-w64-x86_64-{gcc,pkg-config,libogg,libvorbis,flac,mpg123}`, then
   `go build -o go-librespot.exe -ldflags "-s -w" ./cmd/daemon`).
5. Attach `go-librespot_windows_amd64.tar.gz` to a release named `go-librespot-0.9.0-winpipe`
   on **this** repository. `GO_LIBRESPOT_URL` in `src/streamuse/deps.py` points at it.
6. Open the change upstream. Once a go-librespot release carries it, point `GO_LIBRESPOT_URL` at
   theirs and delete this directory.

Until that release exists the Spotify source is offered as unavailable and Apple Music is
unaffected.
