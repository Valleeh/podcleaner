// Package web is the four routes and nothing else.
//
// Everything here is about answering the way a podcatcher expects, because the failures
// that have actually reached listeners were of that kind: an episode stuck at "waiting to
// download" for ever because a HEAD was answered 404, a phone restarting an 86 MB
// download from zero every time the connection dropped because a range was answered with
// the whole file, and a player refusing audio because the reply claimed a character
// encoding for bytes that are not characters.
package web

import (
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"

	"podclean/internal/episode"
)

// The two error bodies worth keeping recognisable: each answers the question its reader
// actually has.
const (
	unknownEpisode = "unknown episode: no feed fetched by this server has named it"
	notProducedYet = "not produced yet: written when the episode is first played"
)

// A Server is the routes and the orchestrator each of them asks.
type Server struct {
	Episodes *episode.Orchestrator
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/rss", s.rss)
	mux.HandleFunc("/podcast", s.podcast)
	mux.HandleFunc("/chapters", sidecar(s.Episodes.Chapters, "application/json+chapters"))
	mux.HandleFunc("/transcript", sidecar(s.Episodes.Transcript, "text/vtt"))
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { text(w, r, 404, "not here") })
	return logging(mux)
}

// rss is the publisher's feed with its links repointed here.
func (s *Server) rss(w http.ResponseWriter, r *http.Request) {
	url := strings.TrimSpace(r.URL.Query().Get("feed"))
	if url == "" {
		text(w, r, http.StatusBadRequest, "ask for a feed: /rss?feed=<the publisher's feed url>")
		return
	}
	rewritten, err := s.Episodes.Feed(url)
	if err != nil {
		text(w, r, http.StatusBadGateway, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/rss+xml")
	body(w, r, http.StatusOK, rewritten)
}

// podcast is the episode's audio, produced on the first play and read from disk after.
func (s *Server) podcast(w http.ResponseWriter, r *http.Request) {
	feedURL, guid, ok := pair(w, r)
	if !ok {
		return
	}
	audio, err := s.Episodes.Play(feedURL, guid)
	switch {
	case errors.Is(err, episode.ErrUnknown):
		text(w, r, http.StatusNotFound, unknownEpisode)
		return
	case err != nil:
		text(w, r, http.StatusBadGateway, err.Error())
		return
	}
	reader, size, err := audio.Open()
	if err != nil {
		text(w, r, http.StatusBadGateway, err.Error())
		return
	}
	defer reader.Close()
	// audio/mpeg with no charset: these are not characters and saying they are is a claim
	// about them that some players believe.
	w.Header().Set("Content-Type", "audio/mpeg")
	file(w, r, reader, size)
}

// sidecar serves one of the two documents about an episode, once there is an episode.
func sidecar(read func(feed, guid string) ([]byte, bool), contentType string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		feedURL, guid, ok := pair(w, r)
		if !ok {
			return
		}
		document, found := read(feedURL, guid)
		if !found {
			text(w, r, http.StatusNotFound, notProducedYet)
			return
		}
		w.Header().Set("Content-Type", contentType)
		body(w, r, http.StatusOK, document)
	}
}

func pair(w http.ResponseWriter, r *http.Request) (string, string, bool) {
	query := r.URL.Query()
	feedURL := strings.TrimSpace(query.Get("feed"))
	guid := strings.TrimSpace(query.Get("guid"))
	if feedURL == "" || guid == "" {
		text(w, r, http.StatusBadRequest, "ask for an episode: ?feed=<the publisher's feed url>&guid=<the episode id>")
		return "", "", false
	}
	return feedURL, guid, true
}

// file answers with the whole thing or with the one range that was asked for.
func file(w http.ResponseWriter, r *http.Request, reader io.ReadSeeker, size int64) {
	w.Header().Set("Accept-Ranges", "bytes")
	from, to, partial := wanted(r.Header.Get("Range"), size)
	status, length := http.StatusOK, size
	if partial {
		status, length = http.StatusPartialContent, to-from+1
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", from, to, size))
		if _, err := reader.Seek(from, io.SeekStart); err != nil {
			text(w, r, http.StatusBadGateway, err.Error())
			return
		}
	}
	w.Header().Set("Content-Length", strconv.FormatInt(length, 10))
	w.WriteHeader(status)
	if r.Method == http.MethodHead {
		return
	}
	if _, err := io.CopyN(w, reader, length); err != nil {
		log.Printf("the listener went away after %d bytes: %v", length, err)
	}
}

// wanted reads the Range header.
//
// One range, in all three forms. Several ranges, an unparsable one, or one starting past
// the end are answered with the whole file, which a client asking for a range is always
// allowed to be given -- and which is a better answer than an error for a header this
// server could not make sense of.
func wanted(header string, size int64) (from, to int64, ok bool) {
	spec, isBytes := strings.CutPrefix(strings.TrimSpace(header), "bytes=")
	if !isBytes || strings.Contains(spec, ",") || size == 0 {
		return 0, 0, false
	}
	first, last, split := strings.Cut(spec, "-")
	if !split {
		return 0, 0, false
	}
	first, last = strings.TrimSpace(first), strings.TrimSpace(last)
	switch {
	case first == "": // -n: the last n bytes
		n, err := strconv.ParseInt(last, 10, 64)
		if err != nil || n <= 0 {
			return 0, 0, false
		}
		if n > size {
			n = size
		}
		return size - n, size - 1, true
	case last == "": // a-: from a to the end
		from, err := strconv.ParseInt(first, 10, 64)
		if err != nil || from < 0 || from >= size {
			return 0, 0, false
		}
		return from, size - 1, true
	default: // a-b
		from, err := strconv.ParseInt(first, 10, 64)
		if err != nil || from < 0 || from >= size {
			return 0, 0, false
		}
		to, err := strconv.ParseInt(last, 10, 64)
		if err != nil || to < from {
			return 0, 0, false
		}
		if to > size-1 {
			to = size - 1 // asked past the end of a file that does have this range's start
		}
		return from, to, true
	}
}

func text(w http.ResponseWriter, r *http.Request, status int, message string) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	body(w, r, status, []byte(message+"\n"))
}

func body(w http.ResponseWriter, r *http.Request, status int, content []byte) {
	w.Header().Set("Content-Length", strconv.Itoa(len(content)))
	w.WriteHeader(status)
	if r.Method != http.MethodHead {
		w.Write(content)
	}
}

// logging records every request with what is needed to diagnose a podcatcher's download
// failures: who asked, as what, for what, and what they were given.
func logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		watched := &watcher{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(watched, r)
		log.Printf("from=%s agent=%q %s %s status=%d bytes=%d range=%q",
			r.RemoteAddr, r.UserAgent(), r.Method, r.URL.RequestURI(),
			watched.status, watched.written, r.Header.Get("Range"))
	})
}

type watcher struct {
	http.ResponseWriter
	status  int
	written int
}

func (w *watcher) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}

func (w *watcher) Write(b []byte) (int, error) {
	n, err := w.ResponseWriter.Write(b)
	w.written += n
	return n, err
}
