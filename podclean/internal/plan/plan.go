// Package plan turns what the model said, once classify has read it, into what may be
// removed, and is where the one rule is enforced: no second of programme is ever removed.
//
// Nothing here talks to anyone. It is given a transcript and an answer and it returns
// intervals, so every judgement it makes can be read in one file -- which matters more
// here than anywhere else in this server, because a mistake in it is the one mistake a
// listener cannot detect and cannot undo.
//
// Every rule below fails in the same direction. A break that cannot be placed exactly is
// left in; a plan that is not believable is dropped entire; a start that cannot be found
// is moved later, never earlier. An advertisement that survives is an annoyance. A
// sentence that disappears is gone.
package plan

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"unicode"

	"podclean/internal/classify"
	"podclean/internal/timeline"
	"podclean/internal/transcript"
)

// margin is how much of a break stays audible at each of its ends: a cut begins this far
// after the break's first word and ends this far before its last cue does.
//
// It is where an error of a word at either edge lands. The classifier may quote a word
// early, the transcriber may stamp one late, a cue may run a breath past the
// advertisement it holds; all are ordinary, and all are harmless as long as the error is
// smaller than the margin. A word of advertising left in is an annoyance where a word of
// programme removed is a loss, so the margin is spent on the side of leaving advertising
// in. A break shorter than two of them is not cut at all.
const margin = 1.5

// minConfidence is the model's own estimate, below which its segment is ignored.
//
// Confidence here means both things at once -- that the run is promotional and that its
// boundaries are right -- so a low number is as often doubt about the edges as about the
// category, and an uncertain edge is exactly what must not be cut on.
const minConfidence = 0.5

// longestBreak is the longest single cut that is believable. Past it, the whole plan is
// dropped rather than the one segment: a model that has claimed ten minutes as one
// advertisement has misread the episode, and its other segments were read the same way.
const longestBreak = 600

// mostOfAnEpisode is the share of an episode that may be removed before the plan stops
// being believable as a whole. No real show is a fifth advertising by time; a plan that
// says so is describing a different episode.
const mostOfAnEpisode = 0.2

// cuttable is the categories that are ever removed.
//
// self_promo is deliberately absent: cutting it once removed 83.7 s of interview from an
// episode, on 2026-09-09, and it was withdrawn the same day. A show's own live dates and
// its own outro run straight out of the programme with no seam to find. credits has never
// been cut.
var cuttable = map[string]bool{"sponsor_read": true, "host_endorsement": true, "cross_promo": true}

// A Plan is what will be done to one episode.
type Plan struct {
	State    string          // cut, clean or refused
	Cuts     []timeline.Span // on the publisher's timeline, sorted and merged
	Refusals []string        // why each candidate that was not cut was not cut
}

// Timeline is this plan as the arithmetic both sidecars are moved by.
func (p Plan) Timeline() timeline.Timeline { return timeline.Timeline{Removed: p.Cuts} }

// Build decides what may be removed from this episode, and nothing else.
func Build(t *transcript.Transcript, segments []classify.Segment, seconds float64) Plan {
	p := Plan{State: "clean"}
	candidates := 0
	for _, s := range segments {
		if !cuttable[s.Category] || s.Confidence < minConfidence {
			continue
		}
		candidates++
		span, err := place(t, s)
		if err != nil {
			p.Refusals = append(p.Refusals, fmt.Sprintf("cues %d-%d: %v", s.StartCue, s.EndCue, err))
			continue
		}
		p.Cuts = append(p.Cuts, span)
	}
	p.Cuts = timeline.Merge(p.Cuts)

	if why := implausible(p.Cuts, seconds); why != "" {
		// The whole plan, not the one segment. The judgement is about the reading, and
		// everything in it came from the same reading.
		return Plan{State: "refused", Refusals: append(p.Refusals, why)}
	}
	switch {
	case len(p.Cuts) > 0:
		p.State = "cut"
	case candidates > 0:
		p.State = "refused"
	}
	return p
}

// implausible says why a plan as a whole cannot be believed, or "".
func implausible(cuts []timeline.Span, seconds float64) string {
	var total float64
	for _, c := range cuts {
		if c.Seconds() > longestBreak {
			return fmt.Sprintf("a single break of %.0f s is longer than %d s", c.Seconds(), longestBreak)
		}
		total += c.Seconds()
	}
	if seconds > 0 && total > mostOfAnEpisode*seconds {
		return fmt.Sprintf("%.0f s of %.0f s is more than a %.0f%% of the episode",
			total, seconds, mostOfAnEpisode*100)
	}
	return ""
}

// place turns one reported segment into the interval that will actually be removed.
//
// The start is the word the model quoted, found among the words of the cues it named; the
// end is where the cue holding the break's quoted last words ends. The two edges are
// guarded differently because they fail differently. A transcriber's segment does not
// begin where an advertisement does, so the cue that opens a break usually opens with the
// host still talking, and a cut from the cue's start would take that sentence -- the
// start has to be placed on the break's own first word. The cue that closes a break ends
// with it, so the end takes a cue's own bound -- but which cue is not the model's count:
// on a transcript with nothing wrong in it, a break named one cue too far took seven
// seconds of programme and one named fifteen too far took forty-five. The last words pick
// the cue, and the cut never ends after the last cue named.
func place(t *transcript.Transcript, s classify.Segment) (timeline.Span, error) {
	// Both indices must exist and bound a complete range. A clamped or invented index is
	// refused outright rather than narrowed: the cut runs to the end of the last cue
	// named, and clamping an index past the transcript to the last cue there is would run
	// it to the end of the episode, programme and all.
	if _, ok := t.Cue(s.StartCue); !ok {
		return timeline.Span{}, fmt.Errorf("cue %d is not in the transcript", s.StartCue)
	}
	last, ok := t.Cue(s.EndCue)
	if !ok {
		return timeline.Span{}, fmt.Errorf("cue %d is not in the transcript", s.EndCue)
	}
	if s.EndCue < s.StartCue {
		return timeline.Span{}, fmt.Errorf("cue %d ends before cue %d begins", s.EndCue, s.StartCue)
	}

	opens, _ := t.Cue(s.StartCue)
	start, err := opening(t.Words(s.StartCue, s.EndCue), s.FirstWords, opens)
	if err != nil {
		return timeline.Span{}, fmt.Errorf("first_words %q: %w", s.FirstWords, err)
	}
	end, err := ending(t, s, last)
	if err != nil {
		return timeline.Span{}, fmt.Errorf("last_words %q: %w", s.LastWords, err)
	}

	// A first word stamped past the last cue's end is the transcriber parking words at
	// the far end of a hole it dropped, and is not a place to cut from.
	if end <= start {
		return timeline.Span{}, fmt.Errorf("the break ends at %.2f s before it starts at %.2f s", end, start)
	}
	if end-start <= 2*margin {
		return timeline.Span{}, fmt.Errorf("%.2f s to %.2f s is shorter than two margins", start, end)
	}
	return timeline.Span{Start: start + margin, End: end - margin}, nil
}

// maxTokens is how much of a quote is matched on, and minTokens how little it may be
// worn down to before the break is given up on.
//
// Six is enough to be unique in an episode and short enough to survive a model that
// quoted one word more than it should have; below three a phrase stops naming a place in
// the transcript and starts naming a turn of speech that occurs all over it.
const (
	maxTokens = 6
	minTokens = 3
)

var errNotFound = errors.New("not in the cues the segment names")

// opening finds where the quoted first words are spoken, and returns the moment the break
// begins.
//
// Every retry drops the quote's first word, which moves the start later. So a quote the
// model got slightly wrong costs seconds of advertising left in and can never cost a
// second of programme.
//
// A quote found in more than one place starts at the last of them, for the same reason:
// every place is inside the cues the model named, and the last removes the least. A spot
// played twice back to back and named as one break opens with the same words twice --
// the last in one episode did, and refusing it left forty seconds of it in.
//
// The one exception is the whole quote found at the very opening of the cue the model
// named first: then the model named the cue the break opens, and that is where it starts.
// A spot played twice and named from its first playing is cut from its first word; with
// a quote worn down to a few words the exception never applies, because a short phrase
// at a cue's opening is as likely a turn of speech as a break.
//
// A place whose first word has no duration is not a place: it is a word the transcriber
// parked at the far end of a stretch it dropped (see transcript.closeLastCue), and the
// last copy of a quote being one of those started a cut twenty seconds into its break.
func opening(words []transcript.Word, quote string, opens transcript.Cue) (float64, error) {
	tokens := normalise(quote)
	if len(tokens) < minTokens {
		return 0, fmt.Errorf("%w: %d words is too few to place an edge on", errNotFound, len(tokens))
	}
	if len(tokens) > maxTokens {
		tokens = tokens[:maxTokens]
	}
	spoken, said := spokenWords(words)
	first, _ := spokenWords(opens.Words)
	whole := true
	for len(tokens) >= minTokens {
		var timed []transcript.Word
		for _, i := range matches(said, tokens) {
			if spoken[i].End > spoken[i].Start {
				timed = append(timed, spoken[i])
			}
		}
		for _, w := range timed {
			if whole && len(first) > 0 && w == first[0] {
				return w.Start, nil
			}
		}
		if len(timed) > 0 {
			return timed[len(timed)-1].Start, nil
		}
		tokens, whole = tokens[1:], false
	}
	return 0, errNotFound
}

// Letters a quoted end must have: endLetters to name a place among all the cues a segment
// names, confirmLetters to confirm that its last cue is the one the break ends in. The
// model often quotes the end in two words -- "slash purpose.", "right here." -- which are
// too few to tell one cue from another and plenty to say whether they are in this one.
const (
	endLetters     = 12
	confirmLetters = 6
)

// ending is where the break ends: the end of the last cue named when the quoted last
// words are in it, otherwise the end of the named cue that holds the last place they are
// found, and an error when they are found nowhere -- a cut whose end is only the model's
// count is the cut that took forty-five seconds of programme.
//
// Every retry drops the quote's last word, which moves the end earlier; and every cue
// searched is one the model named, so the cut never ends after the last of them.
func ending(t *transcript.Transcript, s classify.Segment, last transcript.Cue) (float64, error) {
	tokens := normalise(s.LastWords)
	if quote := strings.Join(tokens, ""); len(quote) >= confirmLetters {
		if _, said := spokenWords(last.Words); strings.Contains(strings.Join(said, ""), quote) {
			return last.End, nil
		}
	}
	if len(tokens) > maxTokens {
		tokens = tokens[len(tokens)-maxTokens:]
	}
	var cues []transcript.Cue
	var said []string
	for i := s.StartCue; i <= s.EndCue; i++ {
		c, _ := t.Cue(i)
		_, words := spokenWords(c.Words)
		for _, w := range words {
			cues, said = append(cues, c), append(said, w)
		}
	}
	for quote := strings.Join(tokens, ""); len(quote) >= endLetters; quote = strings.Join(tokens, "") {
		if found := matches(said, tokens); len(found) > 0 {
			// the cue of the quote's own last word, which may be the next one
			k, run := found[len(found)-1], ""
			for ; len(run) < len(quote); k++ {
				run += said[k]
			}
			return cues[k-1].End, nil
		}
		tokens = tokens[:len(tokens)-1]
	}
	return 0, errNotFound
}

// spokenWords is the words that carry any letters at all, and their comparable form.
// Punctuation on its own is not a word and must not break a phrase in half.
func spokenWords(words []transcript.Word) ([]transcript.Word, []string) {
	var spoken []transcript.Word
	var said []string
	for _, w := range words {
		if bare := fold(w.Text); bare != "" {
			spoken = append(spoken, w)
			said = append(said, bare)
		}
	}
	return spoken, said
}

// matches is every word the quote starts on: its letters, run together, are the letters
// of the words from there, run together, ending on a word boundary.
//
// Run together because the transcriber does not split words where the quote does: one
// episode's "80,000 Hours" was a single word in the segment the model read and two,
// "80" and ",000", in the timings the cut is placed on, and that break was refused in
// every run.
func matches(said, tokens []string) []int {
	quote := strings.Join(tokens, "")
	var at []int
	for i := range said {
		run := ""
		for k := i; k < len(said) && len(run) < len(quote); k++ {
			run += said[k]
		}
		if run == quote {
			at = append(at, i)
		}
	}
	return at
}

func normalise(quote string) []string {
	var tokens []string
	for _, field := range strings.Fields(quote) {
		if bare := fold(field); bare != "" {
			tokens = append(tokens, bare)
		}
	}
	return tokens
}

// fold is a word as it is compared: letters and digits only, case folded.
//
// Unicode-aware on purpose. Every episode this is measured on is German, and stripping
// anything that is not ASCII would take the umlauts out of half the nouns and make two
// different words equal.
func fold(s string) string {
	var b strings.Builder
	for _, r := range s {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(unicode.ToLower(r))
		}
	}
	return b.String()
}

// Chapters places the model's marks on the cues they name, in order, on the publisher's
// timeline.
//
// A mark naming a cue the transcript does not have is dropped and the rest are kept: a
// chapter is a convenience, and unlike a cut a wrong one costs the listener nothing they
// cannot see.
func Chapters(t *transcript.Transcript, marks []classify.Mark) []timeline.Chapter {
	var out []timeline.Chapter
	for _, m := range marks {
		cue, ok := t.Cue(m.Cue)
		if !ok || strings.TrimSpace(m.Title) == "" {
			continue
		}
		out = append(out, timeline.Chapter{At: cue.Start, Title: strings.TrimSpace(m.Title)})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].At < out[j].At })
	return out
}
