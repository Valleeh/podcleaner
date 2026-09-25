package classify

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

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
	return reply, nil
}
