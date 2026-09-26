// Package feed rewrites a publisher's document so that its links point here.
//
// The document is never parsed and re-serialised. That is the whole of how "nothing else
// changed" stays true byte for byte: the CDATA sections, the entities, the whitespace,
// the tags nobody here has thought of. Re-serialising is how the first version of this
// server silently dropped every itunes tag, image and category from every feed it
// touched. So the edits are made in place, by hand, and everything not edited is copied.
package feed

import (
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strings"
)

// podcastNamespace is the namespace the two sidecar links are written in.
//
// A feed that does not declare it gets neither link and keeps exactly what it had:
// declaring a namespace in somebody else's document is a bigger change than this server
// promises to make.
const podcastNamespace = "https://podcastindex.org/namespace/1.0"

var (
	itemPattern      = regexp.MustCompile(`(?s)<item[\s>].*?</item>`)
	guidPattern      = regexp.MustCompile(`(?s)<guid[^>]*>(.*?)</guid>`)
	enclosurePattern = regexp.MustCompile(`<enclosure[^>]*>`)
	urlPattern       = regexp.MustCompile(`(?i)\burl\s*=\s*("[^"]*"|'[^']*')`)
	typePattern      = regexp.MustCompile(`(?i)\btype\s*=\s*("[^"]*"|'[^']*')`)
	nsPattern        = regexp.MustCompile(
		`xmlns:([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*["']` + regexp.QuoteMeta(podcastNamespace) + `["']`)
)

// An Episode is one item this server can address.
type Episode struct {
	GUID        string
	Enclosure   string
	ChaptersURL *string
}

// ErrNothingBindable is a feed with no episode in it this server can address. It is
// answered as an error rather than served back unchanged: a feed that comes back looking
// rewritten but is not would have a podcatcher downloading from the publisher for ever
// with nothing to show that anything went wrong.
var ErrNothingBindable = errors.New("no item in this feed has both a guid and an enclosure this server can address")

// Rewrite returns the document with its links repointed, and the episodes it bound.
func Rewrite(document []byte, feedURL, base string) ([]byte, []Episode, error) {
	doc := string(document)
	prefix := ""
	if m := nsPattern.FindStringSubmatch(doc); m != nil {
		prefix = m[1]
	}

	var episodes []Episode
	seen := map[string]int{}
	for _, item := range itemPattern.FindAllString(doc, -1) {
		if guid, ok := guidOf(item); ok {
			seen[guid]++
		}
	}

	out := itemPattern.ReplaceAllStringFunc(doc, func(item string) string {
		guid, ok := guidOf(item)
		// Left exactly as the publisher wrote it: an item with no name, no audio, or a
		// name another item in the same feed also carries. A link that resolves to the
		// wrong episode is worse than no link.
		if !ok || seen[guid] != 1 {
			return item
		}
		enclosure := enclosurePattern.FindString(item)
		if enclosure == "" {
			return item
		}
		origin, ok := attr(enclosure, urlPattern)
		if !ok || origin == "" {
			return item
		}

		episode := Episode{GUID: guid, Enclosure: origin}
		if prefix != "" {
			if was, ok := attr(sidecar(item, prefix, "chapters"), urlPattern); ok && was != "" {
				episode.ChaptersURL = &was
			}
		}
		episodes = append(episodes, episode)

		item = strings.Replace(item, enclosure,
			setAttr(enclosure, urlPattern, "url", link(base, "podcast", feedURL, guid)), 1)
		if prefix == "" {
			return item
		}
		item = point(item, prefix, "chapters", link(base, "chapters", feedURL, guid),
			"application/json+chapters")
		return point(item, prefix, "transcript", link(base, "transcript", feedURL, guid), "text/vtt")
	})

	if len(episodes) == 0 {
		return nil, nil, ErrNothingBindable
	}
	return []byte(out), episodes, nil
}

// point repoints one sidecar element of this item, or writes one where the publisher had
// none.
//
// The publisher's own chapter link is repointed rather than left alone because their
// marks are timed against their audio, and this server serves shorter audio. Leaving it
// would put every mark minutes out.
func point(item, prefix, kind, href, mime string) string {
	tag := sidecar(item, prefix, kind)
	if tag == "" {
		written := fmt.Sprintf(`<%s:%s url="%s" type="%s"/>`, prefix, kind, href, mime)
		return strings.Replace(item, "</item>", written+"</item>", 1)
	}
	pointed := setAttr(setAttr(tag, urlPattern, "url", href), typePattern, "type", mime)
	return strings.Replace(item, tag, pointed, 1)
}

// sidecar is this item's first chapters or transcript element in that prefix, or "".
// Only the first: an item carrying several is carrying several languages or formats, and
// the ones this server did not write still belong to the publisher.
func sidecar(item, prefix, kind string) string {
	return regexp.MustCompile(`<` + regexp.QuoteMeta(prefix+":"+kind) + `[\s/>][^>]*>`).FindString(item)
}

// link is one of this server's URLs, written for an XML attribute.
//
// The query is URL-encoded and its separator written as an entity, because it is going
// into a document and a bare ampersand there is not a separator, it is a syntax error.
func link(base, route, feedURL, guid string) string {
	return fmt.Sprintf("%s/%s?feed=%s&amp;guid=%s",
		strings.TrimRight(base, "/"), route, url.QueryEscape(feedURL), url.QueryEscape(guid))
}

// guidOf is the episode's name as the publisher gave it: a text node, with CDATA markers
// stripped and the whitespace around it trimmed.
//
// Megaphone writes every guid in CDATA. Carrying the markers into the link would store
// and serve the episode under a name nothing outside this server could address -- it
// would play, and `./run verify` and the listener reading the URL would both be lost.
func guidOf(item string) (string, bool) {
	m := guidPattern.FindStringSubmatch(item)
	if m == nil {
		return "", false
	}
	text := strings.TrimSpace(m[1])
	if inner, ok := strings.CutPrefix(text, "<![CDATA["); ok {
		text = strings.TrimSpace(strings.TrimSuffix(inner, "]]>"))
	}
	if text == "" {
		return "", false
	}
	return text, true
}

func attr(tag string, pattern *regexp.Regexp) (string, bool) {
	m := pattern.FindStringSubmatch(tag)
	if m == nil {
		return "", false
	}
	return unescape(strings.Trim(m[1], `"'`)), true
}

// setAttr writes one attribute of a start tag, adding it if it was not there.
//
// It keeps the tag closing exactly the way the publisher closed it. A sidecar written as
// a pair -- <p:transcript ...>...</p:transcript> -- still has its end tag in the
// document, and turning its start tag into a self-closing one would leave a podcatcher
// parsing a document that is no longer XML.
func setAttr(tag string, pattern *regexp.Regexp, name, value string) string {
	written := fmt.Sprintf(`%s="%s"`, name, value)
	if pattern.MatchString(tag) {
		return pattern.ReplaceAllStringFunc(tag, func(string) string { return written })
	}
	body, closer := strings.TrimSuffix(tag, ">"), ">"
	if trimmed, selfClosing := strings.CutSuffix(body, "/"); selfClosing {
		body, closer = trimmed, "/>"
	}
	return strings.TrimRight(body, " \t\r\n") + " " + written + closer
}

// unescape reads an attribute value the way a parser would. Only the five that matter:
// an attribute this server reads back is a URL, not prose.
func unescape(value string) string {
	return strings.NewReplacer("&amp;", "&", "&lt;", "<", "&gt;", ">",
		"&quot;", `"`, "&apos;", "'").Replace(value)
}
