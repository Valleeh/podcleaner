// Package mp3 is every byte-level thing done to an episode: walking it into frames,
// splitting it for the transcriber, and cutting it.
//
// Nothing here re-encodes and nothing shells out. A cut is whole frames left out, so the
// bytes served are the publisher's own and a replay is byte-identical by construction
// rather than by caching -- and the server needs no ffmpeg, which is why it fits on the
// host at all.
package mp3

import (
	"bytes"
	"errors"
	"fmt"

	"podclean/internal/timeline"
)

// minimumSeconds is how much audio a download has to parse as before this server will
// believe it is an episode.
//
// A publisher's server that answers a download with a block page -- 200, its own length,
// calling itself audio/mpeg -- once had that page published as an episode for ever,
// because nothing re-examines an episode that already has one. A blocklist of what a
// reply calls itself cannot be completed, so what is trusted is the bytes: a page of HTML
// makes a frame or two out of whatever bytes happen to look like a sync word, never
// seconds of them.
const minimumSeconds = 3.0

// A Frame is one MPEG audio frame where it lies in the file.
type Frame struct {
	Offset  int
	Length  int
	Seconds float64
}

// A File is a downloaded episode, walked into frames.
type File struct {
	data   []byte
	frames []Frame
	starts []float64 // each frame's start on the publisher's timeline
}

// Parse walks the bytes into frames, or says why they are not an episode.
func Parse(data []byte) (*File, error) {
	frames := walk(data)
	f := &File{data: data, frames: frames}
	f.starts = make([]float64, len(frames))
	var at float64
	for i, fr := range frames {
		f.starts[i] = at
		at += fr.Seconds
	}
	if at < minimumSeconds {
		return nil, fmt.Errorf("%w: %d bytes parse as %.2f s of audio", ErrNotAudio, len(data), at)
	}
	return f, nil
}

// ErrNotAudio is what the publisher sent failing to be an episode at all.
var ErrNotAudio = errors.New("not audio")

// Seconds is the episode's length: the sum of its frame durations, not anything a header
// claims. A container's own duration field disagrees with the decoder by many seconds on
// real publisher files.
func (f *File) Seconds() float64 {
	if len(f.frames) == 0 {
		return 0
	}
	last := len(f.frames) - 1
	return f.starts[last] + f.frames[last].Seconds
}

// A Piece is a run of whole frames and where it begins in the episode.
type Piece struct {
	Data  []byte
	Start float64
}

// Pieces splits the episode into runs of whole frames of at most maxBytes each.
//
// A full episode is several times what a transcription endpoint accepts. Splitting on
// frame boundaries means every piece is a playable MP3 in its own right, so the endpoint
// needs to know nothing about the split; putting the answers back on one timeline is this
// server's arithmetic, done from Start. No overlap is added between pieces: a word
// garbled at a boundary can only make a quote unfindable, which leaves an advertisement
// in, and that is the direction this project errs in.
func (f *File) Pieces(maxBytes int) []Piece {
	var pieces []Piece
	from := 0
	for from < len(f.frames) {
		to, size := from, 0
		for to < len(f.frames) && (to == from || size+f.frames[to].Length <= maxBytes) {
			size += f.frames[to].Length
			to++
		}
		pieces = append(pieces, Piece{Data: f.span(from, to), Start: f.starts[from]})
		from = to
	}
	return pieces
}

// Cut is the episode with every frame whose *start* lies in a removed span left out.
//
// Its start, half open at the end, so a cut lands on a frame edge -- 26 ms at 44.1 kHz --
// and every byte that comes back is one the publisher sent.
func (f *File) Cut(tl timeline.Timeline) []byte {
	out := make([]byte, 0, len(f.data))
	for i, fr := range f.frames {
		if tl.Holds(f.starts[i]) {
			continue
		}
		out = append(out, f.data[fr.Offset:fr.Offset+fr.Length]...)
	}
	return out
}

func (f *File) span(from, to int) []byte {
	if from >= to {
		return nil
	}
	first, last := f.frames[from], f.frames[to-1]
	return f.data[first.Offset : last.Offset+last.Length]
}

// walk finds the frames, skipping whatever is not one.
func walk(data []byte) []Frame {
	at := skipID3(data)
	var frames []Frame
	for at+4 <= len(data) {
		h, ok := header(data[at:])
		if !ok || !followed(data, at+h.length) {
			at++
			continue
		}
		frames = append(frames, Frame{Offset: at, Length: h.length, Seconds: h.seconds})
		at += h.length
	}
	return dropInfoFrame(data, frames)
}

// followed is the only defence against a sync word that is really audio data: a frame is
// trusted when what it predicts as the next frame parses as a header too, or when it runs
// to the end of the file.
func followed(data []byte, next int) bool {
	if next >= len(data) {
		return next == len(data)
	}
	_, ok := header(data[next:])
	return ok
}

// dropInfoFrame drops a leading Xing or Info frame. It is a header pretending to be
// audio, and its counts describe the file before the cut -- so leaving it in would make
// every player disagree with the bytes about how long the episode is.
func dropInfoFrame(data []byte, frames []Frame) []Frame {
	if len(frames) == 0 {
		return frames
	}
	first := frames[0]
	body := data[first.Offset : first.Offset+first.Length]
	if bytes.Contains(body, []byte("Xing")) || bytes.Contains(body, []byte("Info")) {
		return frames[1:]
	}
	return frames
}

// skipID3 steps over a leading ID3v2 tag by its syncsafe size, plus the footer when the
// flag for one is set.
func skipID3(data []byte) int {
	if len(data) < 10 || !bytes.HasPrefix(data, []byte("ID3")) {
		return 0
	}
	size := syncsafe(data[6:10])
	at := 10 + size
	if data[5]&0x10 != 0 {
		at += 10
	}
	if at > len(data) {
		return len(data)
	}
	return at
}

func syncsafe(b []byte) int {
	n := 0
	for _, c := range b {
		n = n<<7 | int(c&0x7f)
	}
	return n
}

type frameHeader struct {
	length  int
	seconds float64
}

var (
	bitratesV1 = [16]int{0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0}
	bitratesV2 = [16]int{0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0}
	ratesV1    = [4]int{44100, 48000, 32000, 0}
	ratesV2    = [4]int{22050, 24000, 16000, 0}
	ratesV25   = [4]int{11025, 12000, 8000, 0}
)

// header reads one MPEG 1, 2 or 2.5 Layer III frame header.
func header(b []byte) (frameHeader, bool) {
	if len(b) < 4 || b[0] != 0xff || b[1]&0xe0 != 0xe0 {
		return frameHeader{}, false
	}
	version := b[1] >> 3 & 0x03 // 0 = 2.5, 1 = reserved, 2 = 2, 3 = 1
	layer := b[1] >> 1 & 0x03   // 1 = Layer III
	if version == 1 || layer != 1 {
		return frameHeader{}, false
	}
	bitrateIndex := b[2] >> 4 & 0x0f
	rateIndex := b[2] >> 2 & 0x03
	padding := int(b[2] >> 1 & 0x01)

	var bitrate, rate, samples int
	if version == 3 {
		bitrate, rate, samples = bitratesV1[bitrateIndex]*1000, ratesV1[rateIndex], 1152
	} else {
		bitrate, samples = bitratesV2[bitrateIndex]*1000, 576
		if version == 2 {
			rate = ratesV2[rateIndex]
		} else {
			rate = ratesV25[rateIndex]
		}
	}
	if bitrate == 0 || rate == 0 {
		return frameHeader{}, false
	}
	length := samples/8*bitrate/rate + padding
	if length < 4 {
		return frameHeader{}, false
	}
	return frameHeader{length: length, seconds: float64(samples) / float64(rate)}, true
}
