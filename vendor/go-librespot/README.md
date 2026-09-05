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

The build is CGO (`libogg` forced static - see the comment above the build step for why - but
`libvorbis`, `libvorbisenc`, `libFLAC` and `libmpg123` stay dynamic), so `go-librespot.exe` alone
is not enough: it also needs `libmpg123-0.dll`, `libFLAC.dll`, `libvorbisenc-2.dll` and
`libvorbis-0.dll` from `/mingw64/bin` beside it, or Windows refuses to run it at all.

To get one for a source checkout, take it and those four DLLs out of a release exe (or its
`go-librespot-libs.zip` release asset) or run those steps by hand, and put them all in
`%LOCALAPPDATA%\StreaMuse\bin`. `DependencyManager.go_librespot` resolves the exe live, so a running
app picks it up with no restart; `deps.ensure_all` downloads the four DLLs on its own if the exe is
there but they are not.

Open the change upstream. Once a go-librespot release carries it, drop the `librespot` job for a
download of their asset and delete this directory.
