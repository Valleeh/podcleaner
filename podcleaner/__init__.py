"""PodClean v2.

Removes advertising from podcast episodes and serves the result behind a rewritten
RSS feed.  See ``docs/architecture.md`` (arc42) for the design.  The v1 MQTT pipeline
is archived under ``old/``.

Only the detection slice exists so far:

* :mod:`podcleaner.transcripts`      -- cue/word transcript model, SRT/VTT/whisper JSON parsing
* :mod:`podcleaner.detect.transcribe` -- whisper.cpp behind a small interface
* :mod:`podcleaner.detect.llm`        -- LLM classification of transcript cues into ad segments
* :mod:`podcleaner.eval`              -- asymmetric scorer, WER, labels, integration fixtures
"""

__version__ = "2.0.0a0"
