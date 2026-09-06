"""PodClean v2.

Removes advertising from podcast episodes and serves the result behind a rewritten
RSS feed.  See ``docs/architecture.md`` (arc42) for the design.  The v1 MQTT pipeline
is archived under ``old/``.

What exists so far:

* :mod:`podcleaner.fetch`             -- download an episode, trying the ad-free-master shortcut first
* :mod:`podcleaner.feed`              -- fetch the origin RSS and patch enclosure URLs in place
* :mod:`podcleaner.store`             -- episode path from the feed guid, per-episode lock, atomic publish
* :mod:`podcleaner.server`            -- ``/rss`` and ``/podcast``, the skeleton with no detection wired in yet
* :mod:`podcleaner.transcripts`      -- cue/word transcript model, SRT/VTT/whisper JSON parsing
* :mod:`podcleaner.detect.transcribe` -- whisper.cpp behind a small interface
* :mod:`podcleaner.detect.llm`        -- LLM classification of transcript cues into ad segments
* :mod:`podcleaner.eval`              -- asymmetric scorer, WER, labels, integration fixtures
"""

__version__ = "2.0.0a0"
