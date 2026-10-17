package classify

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"unicode"
)

// A Segment is one run the model reported as promotional.
type Segment struct {
	StartCue   int     `json:"start_cue"`
	EndCue     int     `json:"end_cue"`
	Category   string  `json:"category"`
	Confidence float64 `json:"confidence"`
	Voices     names   `json:"voices"`
	Reason     string  `json:"reason"`
	FirstWords string  `json:"first_words"`
	LastWords  string  `json:"last_words"`
}

// names is a list of people the model named. It is read leniently -- a single string is
// one name, anything else is nobody -- because it only ever decides whose plug a segment
// is, and a malformed one must not cost the whole reading.
type names []string

func (n *names) UnmarshalJSON(raw []byte) error {
	var list []string
	if json.Unmarshal(raw, &list) == nil {
		*n = list
		return nil
	}
	var one string
	if json.Unmarshal(raw, &one) == nil && one != "" {
		*n = names{one}
	}
	return nil
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

// errUnreadable is a reply this server cannot read. It is not a verdict about the audio:
// the episode is served whole and the next play asks again.
var errUnreadable = errors.New("unreadable model reply")

// parseReply reads one answer, allowing for a model that wrapped its JSON in a fence.
func parseReply(content string) (Reply, error) {
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
		return Reply{}, fmt.Errorf("%w: %v", errUnreadable, err)
	}
	segments, ok := fields["segments"]
	if !ok {
		return Reply{}, fmt.Errorf("%w: no segments in the answer", errUnreadable)
	}
	var reply Reply
	if err := json.Unmarshal(segments, &reply.Segments); err != nil {
		return Reply{}, fmt.Errorf("%w: %v", errUnreadable, err)
	}
	if chapters, ok := fields["chapters"]; ok {
		_ = json.Unmarshal(chapters, &reply.Chapters) // a courtesy, never a dependency
	}
	var hosts names
	if raw, ok := fields["hosts"]; ok {
		_ = json.Unmarshal(raw, &hosts)
	}
	settleWhosePlug(reply.Segments, hosts)
	return reply, nil
}

// settleWhosePlug files as cross_promo every self_promo in which none of the hosts speaks.
//
// self_promo is this show's hosts plugging their own things, and it is never cut. Asked
// whether a plug was the hosts' own, the model judged from the speaker's side: Ina Garten
// trailing her own podcast, Kara Swisher her own tour, came back as self_promo and stayed
// in. Who speaks in a segment, and who this show's hosts are, it names reliably -- so the
// comparison is made here. A segment with nobody named, or a reply naming no hosts, is
// left as the model filed it: what cannot be told stays in.
func settleWhosePlug(segments []Segment, hosts names) {
	known := map[string]bool{}
	for _, host := range hosts {
		for _, part := range nameParts(host) {
			known[part] = true
		}
	}
	if len(known) == 0 {
		return
	}
	for i, s := range segments {
		if s.Category != "self_promo" || len(s.Voices) == 0 {
			continue
		}
		host := false
		for _, voice := range s.Voices {
			for _, part := range nameParts(voice) {
				host = host || known[part]
			}
		}
		if !host {
			segments[i].Category = "cross_promo"
		}
	}
}

// nameParts is a name's words, lowercased, of three letters or more: "Mike Murphy" and
// "Murphy" are the same host, and an initial or a "Dr." says nothing about who it is.
func nameParts(name string) []string {
	var parts []string
	for _, word := range strings.FieldsFunc(strings.ToLower(name), func(r rune) bool {
		return !unicode.IsLetter(r)
	}) {
		if len([]rune(word)) >= 3 {
			parts = append(parts, word)
		}
	}
	return parts
}
