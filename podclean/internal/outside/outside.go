// Package outside is everything this server says to somebody else, and nothing else.
//
// Three third parties: the publisher, a transcriber and a model. Nothing here retries --
// a second ask is a second bill, and every one of these calls is either free to lose or
// expensive to repeat. Only HTTP 200 is a success; anything else becomes the text of a
// 502, so a listener is told the publisher failed rather than handed something that is
// not the episode.
package outside

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"strings"
	"time"

	"podclean/internal/timeline"
)

// podcatcherUserAgent is what the audio is asked for as, and it is not cosmetic.
//
// Publishers who stitch advertising in serve the ad-free master to a plain client and the
// stitched copy to a podcatcher. A request that does not look like a podcatcher would
// fetch audio no listener is ever served -- and `./run verify`, which fetches the master
// itself to compare against, would then be comparing a file with itself.
const podcatcherUserAgent = "AntennaPod/3.6.0"

// transcriptionModel is the only model this server transcribes with.
const transcriptionModel = "openai/whisper-large-v3-turbo"

const (
	feedTimeout  = 120 * time.Second
	audioTimeout = 600 * time.Second
	paidTimeout  = 900 * time.Second
)

// A Client talks to the three of them.
type Client struct {
	TranscribeBaseURL string
	LLMBaseURL        string
	APIKey            string
}

// send makes one request and returns the body of a 200, or an error naming the url.
//
// The client does not negotiate content encoding. The bytes fetched are hashed and
// recorded, and `./run verify` re-fetches them later to notice that the publisher has
// re-stitched the episode. A transport that transparently gzipped and ungzipped a
// download would hand back bytes nobody ever sent.
func send(req *http.Request, timeout time.Duration) ([]byte, error) {
	client := &http.Client{
		Timeout:   timeout,
		Transport: &http.Transport{DisableCompression: true},
	}
	url := req.URL.String()
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%s: %v", url, err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("%s: %v", url, err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s answered %d: %s",
			url, resp.StatusCode, first200(body))
	}
	return body, nil
}

func (c *Client) get(url string, timeout time.Duration, header http.Header) ([]byte, error) {
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("cannot ask for %s: %v", url, err)
	}
	for name, values := range header {
		req.Header[name] = values
	}
	return send(req, timeout)
}

// Feed fetches the publisher's document.
//
// Deliberately without the podcatcher user-agent: the document is the same for every
// client, and only the audio request needs to look like a podcatcher.
func (c *Client) Feed(url string) ([]byte, error) { return c.get(url, feedTimeout, nil) }

// Audio fetches one episode, as a podcatcher would.
func (c *Client) Audio(url string) ([]byte, error) {
	return c.get(url, audioTimeout, http.Header{"User-Agent": {podcatcherUserAgent}})
}

// PublisherChapters is the publisher's own marks, where the feed named any: url is nil or
// empty when it did not.
//
// A courtesy and never a dependency: they are handed to the model as a starting point,
// and anything at all going wrong here is silently no hint rather than a failure. The
// episode is no worse off for it than one whose publisher wrote none.
func (c *Client) PublisherChapters(url *string) []timeline.Chapter {
	if url == nil || *url == "" {
		return nil
	}
	body, err := c.get(*url, feedTimeout, nil)
	if err != nil {
		return nil
	}
	var doc struct {
		Chapters []struct {
			StartTime float64 `json:"startTime"`
			Title     string  `json:"title"`
		} `json:"chapters"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil
	}
	marks := make([]timeline.Chapter, 0, len(doc.Chapters))
	for _, c := range doc.Chapters {
		marks = append(marks, timeline.Chapter{At: c.StartTime, Title: c.Title})
	}
	return marks
}

// Transcribe sends one piece of audio up and returns the reply's body.
//
// Both timestamp granularities are asked for, and both are needed: the segments become
// the numbered cues the model answers about, and the words are what a cut is placed on.
func (c *Client) Transcribe(audio []byte) ([]byte, error) {
	var body bytes.Buffer
	form := multipart.NewWriter(&body)

	head := textproto.MIMEHeader{}
	head.Set("Content-Disposition", `form-data; name="file"; filename="episode.mp3"`)
	head.Set("Content-Type", "audio/mpeg")
	part, err := form.CreatePart(head)
	if err != nil {
		return nil, fmt.Errorf("cannot build the transcription request: %v", err)
	}
	if _, err := part.Write(audio); err != nil {
		return nil, fmt.Errorf("cannot build the transcription request: %v", err)
	}
	for _, field := range [][2]string{
		{"model", transcriptionModel},
		{"response_format", "verbose_json"},
		{"timestamp_granularities[]", "segment"},
		{"timestamp_granularities[]", "word"},
	} {
		if err := form.WriteField(field[0], field[1]); err != nil {
			return nil, fmt.Errorf("cannot build the transcription request: %v", err)
		}
	}
	form.Close()

	return c.post(c.TranscribeBaseURL+"/audio/transcriptions", form.FormDataContentType(), body.Bytes())
}

// Complete asks one model one question and returns what it said.
func (c *Client) Complete(model, system, user string) (string, error) {
	request, err := json.Marshal(map[string]any{
		"model": model,
		"messages": []map[string]string{
			{"role": "system", "content": system},
			{"role": "user", "content": user},
		},
		// Zero temperature and a JSON object asked for by name: this is a reading task
		// with one right answer, not a writing one, and the answer is parsed.
		"temperature":     0,
		"response_format": map[string]string{"type": "json_object"},
	})
	if err != nil {
		return "", fmt.Errorf("cannot build the request to %s: %v", model, err)
	}
	body, err := c.post(c.LLMBaseURL+"/chat/completions", "application/json", request)
	if err != nil {
		return "", err
	}
	var reply struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(body, &reply); err != nil || len(reply.Choices) == 0 {
		return "", fmt.Errorf("%s answered something that is not a completion: %s",
			model, first200(body))
	}
	return reply.Choices[0].Message.Content, nil
}

func (c *Client) post(url, contentType string, body []byte) ([]byte, error) {
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("cannot ask %s: %v", url, err)
	}
	req.Header.Set("Content-Type", contentType)
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}
	return send(req, paidTimeout)
}

// first200 is as much of somebody else's failure as is worth repeating.
func first200(body []byte) string {
	text := strings.TrimSpace(string(body))
	if len(text) > 200 {
		return text[:200]
	}
	return text
}
