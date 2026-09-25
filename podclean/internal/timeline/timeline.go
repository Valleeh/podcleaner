// Package timeline is the arithmetic the served audio and both sidecars share.
//
// One episode has two timelines: the publisher's, which every time the transcriber and
// the model speak of is on, and the served one, which is the publisher's with the cut
// intervals taken out. Everything that has to move between them moves the same way --
// earlier by exactly the audio removed before it -- and everything that fell inside a
// removed interval is dropped rather than moved to the join. That is one rule, so it
// lives in one place and the chapter marks and the transcript cannot disagree about it.
package timeline

import "sort"

// A Span is a half-open stretch of the publisher's audio: [Start, End).
type Span struct {
	Start float64
	End   float64
}

func (s Span) Seconds() float64 { return s.End - s.Start }

// Merge sorts spans and joins the ones that touch or overlap.
func Merge(spans []Span) []Span {
	if len(spans) == 0 {
		return nil
	}
	in := append([]Span(nil), spans...)
	sort.Slice(in, func(i, j int) bool { return in[i].Start < in[j].Start })
	out := []Span{in[0]}
	for _, s := range in[1:] {
		last := &out[len(out)-1]
		if s.Start <= last.End {
			if s.End > last.End {
				last.End = s.End
			}
			continue
		}
		out = append(out, s)
	}
	return out
}

// A Timeline is the served audio expressed as what was taken out of the publisher's.
type Timeline struct {
	Removed []Span
}

// Total is how much audio is gone.
func (t Timeline) Total() float64 {
	var sum float64
	for _, s := range t.Removed {
		sum += s.Seconds()
	}
	return sum
}

// Holds reports whether this moment was removed, and so has no place on the served
// timeline at all.
func (t Timeline) Holds(at float64) bool {
	for _, s := range t.Removed {
		if at >= s.Start && at < s.End {
			return true
		}
	}
	return false
}

// Touches reports whether any part of this stretch was removed. A transcript line that
// touches a cut is dropped whole rather than trimmed: trimming would leave a line
// claiming words that are not in the file.
func (t Timeline) Touches(s Span) bool {
	for _, r := range t.Removed {
		if s.Start < r.End && s.End > r.Start {
			return true
		}
	}
	return false
}

// At is this moment on the served timeline: earlier by exactly the audio removed before
// it.
func (t Timeline) At(at float64) float64 {
	moved := at
	for _, s := range t.Removed {
		if s.Start >= at {
			break
		}
		if at >= s.End {
			moved -= s.Seconds()
		} else {
			moved -= at - s.Start
		}
	}
	return moved
}

// A Chapter is one titled mark, on whichever timeline it was placed on.
type Chapter struct {
	At    float64
	Title string
}

// Chapters is the marks as they fall on the served audio: a mark whose audio was removed
// is dropped rather than moved to the join, and the rest move by exactly what was removed
// before them.
//
// Dropped, because moving it would stand an advertiser's name over the programme that
// follows the cut. The cost is that the stretch after a cut carries the title of the
// chapter before it -- a title written for audio that is still there.
func (t Timeline) Chapters(chapters []Chapter) []Chapter {
	var out []Chapter
	for _, c := range chapters {
		if t.Holds(c.At) {
			continue
		}
		out = append(out, Chapter{At: t.At(c.At), Title: c.Title})
	}
	return out
}
