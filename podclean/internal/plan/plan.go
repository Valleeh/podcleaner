// Package plan turns what the model said into what may be removed, and is where the one
// rule is enforced: no second of programme is ever removed.
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
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"unicode"

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

// A Segment is one run the model reported as promotional.
type Segment struct {
	StartCue   int     `json:"start_cue"`
	EndCue     int     `json:"end_cue"`
	Category   string  `json:"category"`
	Confidence float64 `json:"confidence"`
	Reason     string  `json:"reason"`
	FirstWords string  `json:"first_words"`
}

// A Mark is one chapter the model proposed, by the cue it starts on.
type Mark struct {
	Cue   int    `json:"cue"`
	Title string `json:"title"`
}

// A Reply is one model's answer, read but not yet judged.
type Reply struct {
	Segments []Segment `json:"segments"`
	Chapters []Mark    `json:"chapters"`
}

// ErrUnreadable is a reply this server cannot read. It is not a verdict about the audio:
// the episode is served whole and the next play asks again.
var ErrUnreadable = errors.New("unreadable model reply")

// ParseReply reads one answer, allowing for a model that wrapped its JSON in a fence.
func ParseReply(content string) (Reply, error) {
	body := strings.TrimSpace(content)
	if strings.HasPrefix(body, "```") {
		body = strings.TrimPrefix(strings.TrimPrefix(body, "```json"), "```")
		if end := strings.LastIndex(body, "```"); end >= 0 {
			body = body[:end]
		}
		body = strings.TrimSpace(body)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(body), &fields); err != nil {
		return Reply{}, fmt.Errorf("%w: %v", ErrUnreadable, err)
	}
	segments, ok := fields["segments"]
	if !ok {
		return Reply{}, fmt.Errorf("%w: no segments in the answer", ErrUnreadable)
	}
	var reply Reply
	if err := json.Unmarshal(segments, &reply.Segments); err != nil {
		return Reply{}, fmt.Errorf("%w: %v", ErrUnreadable, err)
	}
	if chapters, ok := fields["chapters"]; ok {
		_ = json.Unmarshal(chapters, &reply.Chapters) // a courtesy, never a dependency
	}
	return reply, nil
}

// A Plan is what will be done to one episode.
type Plan struct {
	State    string          // cut, clean or refused
	Cuts     []timeline.Span // on the publisher's timeline, sorted and merged
	Refusals []string        // why each candidate that was not cut was not cut
}

// Timeline is this plan as the arithmetic both sidecars are moved by.
func (p Plan) Timeline() timeline.Timeline { return timeline.Timeline{Removed: p.Cuts} }

// Build decides what may be removed from this episode, and nothing else.
func Build(t *transcript.Transcript, segments []Segment, seconds float64) Plan {
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
// end is where the last of those cues ends. The two edges are guarded differently because
// they fail differently. A transcriber's segment does not begin where an advertisement
// does, so the cue that opens a break usually opens with the host still talking, and a
// cut from the cue's start would take that sentence -- the start has to be placed on the
// break's own first word. The cue that closes a break ends with it: in every recorded cut
// the advertisement's last cue ended where the advertisement did, while the quote asked
// for that end caused every refusal there was, so the end takes the cue's own bound.
func place(t *transcript.Transcript, s Segment) (timeline.Span, error) {
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

	start, err := opening(t.Words(s.StartCue, s.EndCue), s.FirstWords)
	if err != nil {
		return timeline.Span{}, fmt.Errorf("first_words %q: %w", s.FirstWords, err)
	}
	end := last.End

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
var errAmbiguous = errors.New("matches more than one place in the cues the segment names")

// opening finds where the quoted first words are spoken, and returns the moment the break
// begins.
//
// Every retry drops the quote's first word, which moves the start later. So a quote the
// model got slightly wrong costs seconds of advertising left in and can never cost a
// second of programme.
func opening(words []transcript.Word, quote string) (float64, error) {
	tokens := normalise(quote)
	if len(tokens) < minTokens {
		return 0, fmt.Errorf("%w: %d words is too few to place an edge on", errNotFound, len(tokens))
	}
	if len(tokens) > maxTokens {
		tokens = tokens[:maxTokens]
	}
	spoken, said := spokenWords(words)
	for len(tokens) >= minTokens {
		found := matches(said, tokens)
		if len(found) > 1 {
			return 0, errAmbiguous
		}
		if len(found) == 1 {
			return spoken[found[0]].Start, nil
		}
		tokens = tokens[1:]
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

func matches(said, tokens []string) []int {
	var at []int
	for i := 0; i+len(tokens) <= len(said); i++ {
		hit := true
		for j, token := range tokens {
			if said[i+j] != token {
				hit = false
				break
			}
		}
		if hit {
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

// A Chapter is one mark placed on the publisher's timeline.
type Chapter struct {
	At    float64
	Title string
}

// Chapters places the model's marks on the cues they name, in order.
//
// A mark naming a cue the transcript does not have is dropped and the rest are kept: a
// chapter is a convenience, and unlike a cut a wrong one costs the listener nothing they
// cannot see.
func Chapters(t *transcript.Transcript, marks []Mark) []Chapter {
	var out []Chapter
	for _, m := range marks {
		cue, ok := t.Cue(m.Cue)
		if !ok || strings.TrimSpace(m.Title) == "" {
			continue
		}
		out = append(out, Chapter{At: cue.Start, Title: strings.TrimSpace(m.Title)})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].At < out[j].At })
	return out
}

// Served is the marks as they fall on the audio that was served: a mark whose audio was
// removed is dropped rather than moved to the join, and the rest move by exactly what was
// removed before them.
//
// Dropped, because moving it would stand an advertiser's name over the programme that
// follows the cut. The cost is that the stretch after a cut carries the title of the
// chapter before it -- a title written for audio that is still there.
func Served(chapters []Chapter, tl timeline.Timeline) []Chapter {
	var out []Chapter
	for _, c := range chapters {
		if tl.Holds(c.At) {
			continue
		}
		out = append(out, Chapter{At: tl.At(c.At), Title: c.Title})
	}
	return out
}
