package mp3

import (
	"encoding/binary"
	"fmt"

	"podclean/internal/timeline"
)

// ChapterTag is an ID3v2.4 tag carrying the same marks the sidecar carries, to go ahead
// of the frames of a cut episode.
//
// It exists because Overcast displays chapter marks it reads out of the file and ignores
// the feed's link entirely, and Apple Podcasts reads the file first. One list written
// twice, so the two cannot disagree. It is written only for an episode that was actually
// cut: an untouched one keeps whatever the publisher's own file carried, which is already
// right for audio nothing was removed from.
func ChapterTag(marks []timeline.Chapter, seconds float64) []byte {
	if len(marks) == 0 {
		return nil
	}
	ids := make([]string, len(marks))
	for i := range marks {
		ids[i] = fmt.Sprintf("ch%d", i+1)
	}

	body := frame("CTOC", toc(ids))
	for i, m := range marks {
		end := seconds
		if i+1 < len(marks) {
			end = marks[i+1].At
		}
		body = append(body, frame("CHAP", chap(ids[i], m.At, end, m.Title))...)
	}

	tag := []byte{'I', 'D', '3', 4, 0, 0}
	return append(append(tag, encodeSyncsafe(len(body))...), body...)
}

// toc is the table of contents: top-level and ordered, listing every chapter.
func toc(ids []string) []byte {
	b := append([]byte("toc"), 0)
	b = append(b, 0x03, byte(len(ids))) // top-level | ordered, then the entry count
	for _, id := range ids {
		b = append(b, append([]byte(id), 0)...)
	}
	return b
}

func chap(id string, start, end float64, title string) []byte {
	b := append([]byte(id), 0)
	b = binary.BigEndian.AppendUint32(b, uint32(start*1000))
	b = binary.BigEndian.AppendUint32(b, uint32(end*1000))
	// Byte offsets are not given: the times are the answer, and an offset that disagreed
	// with them would be believed by some players over the times.
	b = binary.BigEndian.AppendUint32(b, 0xFFFFFFFF)
	b = binary.BigEndian.AppendUint32(b, 0xFFFFFFFF)
	return append(b, frame("TIT2", append([]byte{0x03}, title...))...) // 0x03: UTF-8
}

func frame(id string, body []byte) []byte {
	out := append([]byte(id), encodeSyncsafe(len(body))...)
	return append(append(out, 0, 0), body...)
}

// encodeSyncsafe writes a size the way ID3v2.4 writes every one of them: seven bits to
// the byte, so that no size can ever look like a frame sync.
func encodeSyncsafe(n int) []byte {
	return []byte{byte(n >> 21 & 0x7f), byte(n >> 14 & 0x7f), byte(n >> 7 & 0x7f), byte(n & 0x7f)}
}
