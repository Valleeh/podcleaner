package mp3

import (
	"bytes"
	"embed"
)

// cueFiles is the tone, encoded once per format an episode comes in, by cue/make.sh: pure
// frames, no tag, no Xing frame, no bit reservoir -- so none of them reaches back into the
// publisher's frame before it.
//
//go:embed cue/*.mp3
var cueFiles embed.FS

// A Cue is the tone put in where advertising was cut. The owner asked for it on 2026-10-17
// to hear where every cut is, and so to check by ear that only advertising went.
//
// The zero Cue is no tone: an episode in a format there is no cue for is cut as it always
// was, rather than served frames its decoder may stumble on.
type Cue struct {
	frames  []byte
	seconds float64
}

// Seconds is how long the tone plays.
func (c Cue) Seconds() float64 { return c.seconds }

// Format is the episode's format: the one most of its frames are in, and the bitrate most
// of those say. Not the first frame's -- a spot stitched in at the front can be encoded
// differently from the programme it is gone from once it is cut.
func (f *File) Format() Format {
	counts := map[Format]int{}
	for _, fr := range f.frames {
		if h, ok := header(f.data[fr.Offset:]); ok {
			counts[h.format]++
		}
	}
	shape := func(x Format) Format { x.BitrateIndex = 0; return x }
	shapes := map[Format]int{}
	for format, n := range counts {
		shapes[shape(format)] += n
	}
	var best Format
	for format, n := range counts {
		if s, b := shapes[shape(format)], shapes[shape(best)]; s > b || s == b && n > counts[best] {
			best = format
		}
	}
	return best
}

// Cue is the tone in this episode's format -- same MPEG version, sample rate and channels --
// or none.
func (f *File) Cue() Cue {
	episode := f.Format()
	entries, _ := cueFiles.ReadDir("cue")
	for _, e := range entries {
		data, err := cueFiles.ReadFile("cue/" + e.Name())
		if err != nil {
			continue
		}
		frames := walk(data)
		if len(frames) == 0 {
			continue
		}
		h, _ := header(data[frames[0].Offset:])
		if h.format.Version == episode.Version && h.format.Rate == episode.Rate &&
			h.format.Mono == episode.Mono {
			return cueAt(data, frames, episode.BitrateIndex)
		}
	}
	return Cue{}
}

// cueAt is the cue's frames saying the episode's bitrate instead of their own, so a
// constant-bitrate episode stays one and a player that reckons time from bytes is not
// thrown by the tone. A frame is never squeezed below its own bitrate; an episode lower
// than the cue gets it as it is.
//
// The room the wider frame makes is filled with 0xFF, never with zeros. The cue skips it
// as ancillary data, but the publisher's frame after the cue does not: nearly every one
// begins its audio up to 511 bytes back, in the bit reservoir -- which is now the cue's
// last bytes. Read as Huffman code, zeros are the largest values there are, and on 96
// kbps episodes 40 to 70 % of joins cracked up to 20 dB over full scale; 0xFF reads as
// silence at the strongest attenuation, and cracked less often than the plain cut did
// (measured 2026-10-17, review of the cue). Nor can 0xFF pass for a frame header: its
// bitrate index would be 15, which is no bitrate.
func cueAt(data []byte, frames []Frame, bitrateIndex byte) Cue {
	var out []byte
	var seconds float64
	for _, fr := range frames {
		frame := append([]byte(nil), data[fr.Offset:fr.Offset+fr.Length]...)
		seconds += fr.Seconds
		wider := append([]byte(nil), frame[:4]...)
		wider[2] = wider[2]&0x0d | bitrateIndex<<4 // the episode's bitrate, no padding
		if h, ok := header(wider); ok && h.length > len(frame) {
			frame = append(frame, bytes.Repeat([]byte{0xff}, h.length-len(frame))...)
			copy(frame, wider)
		}
		out = append(out, frame...)
	}
	return Cue{frames: out, seconds: seconds}
}
