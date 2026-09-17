// Package transcript is the episode as cues and words, and everything read off them.
//
// A cue is one transcription segment. The model is shown cues and answers about cues; a
// cut is placed on the words inside them. The two are kept together here because the
// numbering that joins them is load-bearing: a break is placed by looking its cues up by
// index, so a gap in the numbering refuses a segment rather than moving it.
package transcript

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"podclean/internal/timeline"
)

// A Word is one spoken word with its own timing. What a cut is actually placed on.
type Word struct {
	Text  string
	Start float64
	End   float64
}

// A Cue is one transcription segment, numbered from 1 as the model is shown it.
type Cue struct {
	Index int
	Start float64
	End   float64
	Text  string
	Words []Word
}

// A Transcript is the whole episode, on the publisher's timeline.
type Transcript struct {
	Cues []Cue
}

// A Piece is one transcription reply, still timed from its own beginning.
type Piece struct {
	cues  []Cue
	words []Word
}

var ErrNotVerbose = errors.New("not a verbose_json transcription")

// Parse reads one reply. Both granularities are required: the segments become the
// numbered cues the model answers about, and the words are what a cut is placed on, so a
// reply carrying only segments is no use and is rejected rather than half used.
func Parse(body []byte) (Piece, error) {
	var reply struct {
		Segments []struct {
			Start float64 `json:"start"`
			End   float64 `json:"end"`
			Text  string  `json:"text"`
		} `json:"segments"`
		Words []struct {
			Word  string  `json:"word"`
			Start float64 `json:"start"`
			End   float64 `json:"end"`
		} `json:"words"`
	}
	if err := json.Unmarshal(body, &reply); err != nil {
		return Piece{}, fmt.Errorf("%w: %v", ErrNotVerbose, err)
	}
	if len(reply.Segments) == 0 || len(reply.Words) == 0 {
		return Piece{}, fmt.Errorf("%w: %d segments, %d words",
			ErrNotVerbose, len(reply.Segments), len(reply.Words))
	}
	p := Piece{}
	for _, s := range reply.Segments {
		p.cues = append(p.cues, Cue{Start: s.Start, End: s.End, Text: strings.TrimSpace(s.Text)})
	}
	for _, w := range reply.Words {
		p.words = append(p.words, Word{Text: strings.TrimSpace(w.Word), Start: w.Start, End: w.End})
	}
	return p, nil
}

// Join puts the pieces back on one timeline: each shifted by where its audio began, and
// the cues renumbered continuously from 1 across all of them.
//
// The renumbering is what the model is shown, so it is also what the model answers about.
// Numbering each piece from 1 again would give several cues the same number and place
// every break after the first one in the wrong episode entirely.
func Join(pieces []Piece, starts []float64) *Transcript {
	t := &Transcript{}
	var words []Word
	for i, p := range pieces {
		at := starts[i]
		for _, c := range p.cues {
			c.Start += at
			c.End += at
			c.Index = len(t.Cues) + 1
			c.Words = nil
			t.Cues = append(t.Cues, c)
		}
		for _, w := range p.words {
			w.Start += at
			w.End += at
			words = append(words, w)
		}
	}
	t.attach(words)
	return t
}

// attach hangs every word off the last cue that had started by the word's own start.
func (t *Transcript) attach(words []Word) {
	at := 0
	for _, w := range words {
		for at+1 < len(t.Cues) && t.Cues[at+1].Start <= w.Start {
			at++
		}
		if len(t.Cues) == 0 {
			return
		}
		t.Cues[at].Words = append(t.Cues[at].Words, w)
	}
}

// Cue is the cue with this number, if the transcript has one.
func (t *Transcript) Cue(index int) (Cue, bool) {
	if index < 1 || index > len(t.Cues) {
		return Cue{}, false
	}
	return t.Cues[index-1], true
}

// Words is every word in a run of cues, in order.
func (t *Transcript) Words(from, to int) []Word {
	var out []Word
	for i := from; i <= to; i++ {
		if c, ok := t.Cue(i); ok {
			out = append(out, c.Words...)
		}
	}
	return out
}

// Render is the transcript as the model is shown it: one line per cue, the cue's own
// number in brackets and its start as a clock, so that an answer can name a cue and
// nothing else.
func (t *Transcript) Render() string {
	var b strings.Builder
	for _, c := range t.Cues {
		fmt.Fprintf(&b, "[%d] %d:%02d %s\n", c.Index, int(c.Start)/60, int(c.Start)%60, c.Text)
	}
	return b.String()
}

// VTT is the transcript of the audio that was served: every cue that touched removed
// audio dropped whole, the rest moved by exactly what was removed before them.
//
// Dropped whole rather than trimmed, which leaves a few seconds either side of a join
// with audio and no text. Trimming would leave a line claiming words that are not in the
// file, and a listener reading along cannot tell that from a mistranscription.
func (t *Transcript) VTT(tl timeline.Timeline) string {
	var b strings.Builder
	b.WriteString("WEBVTT\n")
	n := 0
	for _, c := range t.Cues {
		if tl.Touches(timeline.Span{Start: c.Start, End: c.End}) {
			continue
		}
		n++
		fmt.Fprintf(&b, "\n%d\n%s --> %s\n%s\n", n, stamp(tl.At(c.Start)), stamp(tl.At(c.End)), c.Text)
	}
	return b.String()
}

func stamp(at float64) string {
	if at < 0 {
		at = 0
	}
	ms := int(at*1000 + 0.5)
	return fmt.Sprintf("%02d:%02d:%02d.%03d", ms/3600000, ms/60000%60, ms/1000%60, ms%1000)
}
