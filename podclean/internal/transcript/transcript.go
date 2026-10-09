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
	"sort"
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

// A Piece is one transcription reply, still timed from its own beginning, and where in
// the episode that beginning is.
type Piece struct {
	start float64
	cues  []Cue
	words []Word
	hole  *timeline.Span // set when this reply is about a hole, sent again on its own
}

// Filling marks this reply as the one about hole, a stretch the first replies left empty.
func (p Piece) Filling(hole timeline.Span) Piece {
	p.hole = &hole
	return p
}

var errNotVerbose = errors.New("not a verbose_json transcription")

// Parse reads one reply to the audio that begins start seconds into the episode. Both
// granularities are required: the segments become the numbered cues the model answers
// about, and the words are what a cut is placed on, so a reply carrying only segments is
// no use and is rejected rather than half used.
func Parse(body []byte, start float64) (Piece, error) {
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
		return Piece{}, fmt.Errorf("%w: %v", errNotVerbose, err)
	}
	if len(reply.Segments) == 0 || len(reply.Words) == 0 {
		return Piece{}, fmt.Errorf("%w: %d segments, %d words",
			errNotVerbose, len(reply.Segments), len(reply.Words))
	}
	p := Piece{start: start}
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
//
// A reply about a hole lands inside another reply's stretch, so everything is put in
// time order before it is numbered. Around a hole the first reply often parked the words
// it lost, all with no duration, at the far end; once the hole has its own reply those
// would be the same words twice, and a quote found twice is refused, so they are dropped.
//
// seconds is how long the audio is, and it is the only bound on the last cue below.
func Join(pieces []Piece, seconds float64) *Transcript {
	var holes []timeline.Span
	for _, p := range pieces {
		if p.hole != nil {
			holes = append(holes, *p.hole)
		}
	}
	t := &Transcript{}
	var words []Word
	for _, p := range pieces {
		at := p.start
		for _, c := range p.cues {
			c.Start += at
			c.End += at
			c.Words = nil
			t.Cues = append(t.Cues, c)
		}
		for _, w := range p.words {
			w.Start += at
			w.End += at
			if p.hole == nil && w.End == w.Start && inside(w.Start, holes) {
				continue
			}
			words = append(words, w)
		}
	}
	sort.SliceStable(t.Cues, func(i, j int) bool { return t.Cues[i].Start < t.Cues[j].Start })
	sort.SliceStable(words, func(i, j int) bool { return words[i].Start < words[j].Start })
	for i := range t.Cues {
		t.Cues[i].Index = i + 1
	}
	t.attach(words)
	t.closeLastCue(seconds)
	return t
}

// inside is whether at lies in one of the spans, both ends included: a parked word sits
// exactly on the far end of its hole.
func inside(at float64, spans []timeline.Span) bool {
	for _, s := range spans {
		if at >= s.Start && at <= s.End {
			return true
		}
	}
	return false
}

// Holes are the stretches of at least least seconds that no cue and no timed word speaks
// for, from the start of the audio to seconds. A word with no duration is not a timing
// (see closeLastCue) and does not fill one.
func (t *Transcript) Holes(least, seconds float64) []timeline.Span {
	var said []timeline.Span
	for _, c := range t.Cues {
		said = append(said, timeline.Span{Start: c.Start, End: c.End})
		for _, w := range c.Words {
			if w.End > w.Start {
				said = append(said, timeline.Span{Start: w.Start, End: w.End})
			}
		}
	}
	var holes []timeline.Span
	at := 0.0
	for _, s := range timeline.Merge(said) {
		if s.Start-at >= least {
			holes = append(holes, timeline.Span{Start: at, End: s.Start})
		}
		if s.End > at {
			at = s.End
		}
	}
	if seconds-at >= least {
		holes = append(holes, timeline.Span{Start: at, End: seconds})
	}
	return holes
}

// attach hangs every word off the last cue that had started by the word's own start.
func (t *Transcript) attach(words []Word) {
	if len(t.Cues) == 0 {
		return
	}
	at := 0
	for _, w := range words {
		for at+1 < len(t.Cues) && t.Cues[at+1].Start <= w.Start {
			at++
		}
		t.Cues[at].Words = append(t.Cues[at].Words, w)
	}
}

// closeLastCue lets the final cue cover the words that landed in it, up to the length of
// the audio -- and does that for the final cue only.
//
// A transcription reply does not agree with itself. Its segments stop while the words it
// timed inside them keep going, and at the end of a file everything after the last segment
// still belongs to the last cue that had started. A cut ends where its last cue ends, so a
// post-roll described that way would be cut short of its own words and most of it left
// in: that is how the advertising at the end of two real episodes survived, reported at
// 0.95 and at 1.0 confidence.
//
// The same disagreement anywhere else in the file means something entirely different, and
// following it there would remove programme. When segments stop short in the middle, the
// aligner has dropped a stretch of speech and parked the words that follow at the far end
// of the hole. Measured in the one real reply this project keeps: ninety-four cues of two
// hundred are stamped past their own end, four by more than ten seconds, the worst by
// twenty-eight -- and the audio inside that gap is programme, unmentioned by any cue,
// which is why nothing would have shown that it had gone. So every other cue ends exactly
// where the transcriber said it does, and a cut that ends on one stops there.
//
// A word that the aligner gave no duration at all is not a timing and is never followed.
// That is what the four catastrophic cues above are made of -- 7 of 7, 11 of 11, 39 of 39
// and 33 of 34 of their words have an end equal to their start, a cluster parked at the
// far end of the hole rather than spread across it. A word with no length is the aligner
// saying it does not know where the word is, and an edge this server cannot trust leaves
// the advertising in. Ordinary words are milliseconds long; an end exactly equal to a
// start is not a short word.
//
// The bound is the audio itself, which is the one number here no word timing can move. If
// it is not known, the cue does not grow: a missing bound must not resolve towards cutting.
func (t *Transcript) closeLastCue(seconds float64) {
	if len(t.Cues) == 0 || seconds <= 0 {
		return
	}
	last := &t.Cues[len(t.Cues)-1]
	end := last.End
	for _, w := range last.Words {
		if w.End > w.Start && w.End > end {
			end = w.End
		}
	}
	if end > seconds {
		end = seconds
	}
	if end > last.End {
		last.End = end
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
