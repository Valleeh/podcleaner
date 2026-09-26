// Package config is the ten environment variables, read at use.
//
// There is no configuration file and no struct passed around: a value is read when it is
// wanted, so that a deployment changes by restarting with a different environment and by
// nothing else.
package config

import (
	"os"
	"strconv"
)

func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

func Host() string { return env("PODCLEANER_HOST", "0.0.0.0") }

func Port() string { return env("PODCLEANER_PORT", "8080") }

// BaseURL is what the server calls itself in the links it writes into a feed. It is not
// derived from the request: a podcatcher stores the link it was given and asks for it
// again days later, so the name has to be the one the operator published, not the one
// whichever proxy happened to forward the request used.
func BaseURL() string { return env("PODCLEANER_BASE_URL", "http://127.0.0.1:"+Port()) }

func StoreRoot() string { return env("PODCLEANER_STORE_ROOT", "var/episodes") }

func LLMBaseURL() string { return env("PODCLEANER_LLM_BASE_URL", "https://openrouter.ai/api/v1") }

func TranscribeBaseURL() string {
	return env("PODCLEANER_TRANSCRIBE_BASE_URL", "https://openrouter.ai/api/v1")
}

// APIKey is the bearer token for both endpoints above. They are one account.
func APIKey() string { return os.Getenv("PODCLEANER_LLM_API_KEY") }

func LLMSpec() string {
	return env("PODCLEANER_LLM_SPEC", "cascade:qwen/qwen3.7-flash>deepseek/deepseek-v4-flash")
}

// TranscribeMaxBytes is the largest piece of audio put into one transcription request.
//
// 24 MiB against the endpoint's 25: the multipart wrapper and the form fields go up too,
// and a request refused for being a few hundred bytes over costs the whole episode.
func TranscribeMaxBytes() int {
	if n, err := strconv.Atoi(os.Getenv("PODCLEANER_TRANSCRIBE_MAX_BYTES")); err == nil && n > 0 {
		return n
	}
	return 25165824
}

// ParentPID, when set, is a process this server dies with: `./run serve` is a `docker run`
// that would otherwise outlive the shell that started it.
func ParentPID() string { return os.Getenv("PODCLEANER_PARENT_PID") }
