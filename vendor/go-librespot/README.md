# The go-librespot pipe patch

StreaMuse reads Spotify audio as PCM from a Windows named pipe. go-librespot supports exactly that
on every platform except Windows, where `output/driver-pipe-stub.go` returns
`"pipe output is not supported on Windows"`; its WASAPI backend is the only other option and plays
to the system default device, which is neither ours to take over nor routable to us.

The Unix implementation only needs `os.OpenFile`. The `O_NONBLOCK` dance around it is FIFO
semantics that a Windows named pipe does not have, so the Windows version is the same function
without it.

## Producing the binary

The `librespot` job in `.github/workflows/build.yml` does this on every release build, and the
result is shipped inside `StreaMuse.exe`. It is upstream's own Windows job - `release.yml` in
`devgianlu/go-librespot`, MSYS2 `MINGW64` with
`mingw-w64-x86_64-{gcc,pkg-config,libogg,libvorbis,flac,mpg123}` - with two steps inserted:
`driver-pipe-windows.go` copied to `output/`, and `output/driver-pipe-stub.go` deleted, because
both define `newPipeOutput`. Keep it in step with theirs; `LIBRESPOT_REF` pins the tag it patches.

To get one for a source checkout, take it out of a release exe or run those steps by hand and put
`go-librespot.exe` in `%LOCALAPPDATA%\StreaMuse\bin`. `DependencyManager.go_librespot` resolves it
live, so a running app picks it up.

Open the change upstream. Once a go-librespot release carries it, drop the `librespot` job for a
download of their asset and delete this directory.
