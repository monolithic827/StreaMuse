//go:build windows

package output

import (
	"fmt"
	"os"
	"sync"
)

func newPipeOutput(opts *NewOutputOptions) (out *pipeOutput, err error) {
	out = &pipeOutput{
		reader:         opts.Reader,
		volume:         opts.InitialVolume,
		err:            make(chan error, 2),
		externalVolume: opts.ExternalVolume,
		volumeUpdate:   opts.VolumeUpdate,
	}

	out.cond = sync.NewCond(&out.lock)

	out.transform, err = newPipeTransform(opts.OutputPipeFormat)
	if err != nil {
		return nil, err
	}

	// A named pipe client is just CreateFile on \.\pipe\<name>, which is what os.OpenFile does
	// here. The Unix side opens the FIFO non-blocking to find out whether a reader is attached;
	// on Windows the open itself fails when no server instance is listening, so there is nothing
	// to probe and OutputPipeWaitForReader has no meaning.
	out.file, err = os.OpenFile(opts.OutputPipe, os.O_WRONLY, 0)
	if err != nil {
		return nil, fmt.Errorf("failed to open named pipe: %w", err)
	}

	go out.outputLoop()

	return out, nil
}
