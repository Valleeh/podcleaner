"""Domain models for the PodCleaner package."""

from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime

@dataclass
class Segment:
    """A segment of transcribed audio."""
    id: int
    text: str
    start: float
    end: float
    is_ad: bool = False

@dataclass
class TranscriptChunk:
    """A chunk of transcript segments for processing."""
    segments: List[Segment]
    chunk_id: int

@dataclass
class ProcessingResult:
    """Result of processing a transcript chunk."""
    chunk_id: int
    segments: List[Segment]
    error: Optional[str] = None

@dataclass
class Transcript:
    """A complete podcast transcript."""
    segments: List[Segment]
    processed_at: datetime = datetime.now()
    
    @property
    def ad_segments(self) -> List[Segment]:
        """Get all segments marked as advertisements."""
        return [seg for seg in self.segments if seg.is_ad]
    
    @property
    def non_ad_segments(self) -> List[Segment]:
        """Get all segments not marked as advertisements."""
        return [seg for seg in self.segments if not seg.is_ad]
    
    def to_dict(self) -> dict:
        """Convert transcript to dictionary format."""
        return {
            "segments": [
                {
                    "id": seg.id,
                    "text": seg.text,
                    "start": seg.start,
                    "end": seg.end,
                    "is_ad": seg.is_ad
                }
                for seg in self.segments
            ],
            "processed_at": self.processed_at.isoformat()
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Transcript':
        """Create transcript from dictionary format."""
        segments = [
            Segment(
                id=seg["id"],
                text=seg["text"],
                start=seg["start"],
                end=seg["end"],
                is_ad=seg["is_ad"]
            )
            for seg in data["segments"]
        ]
        processed_at = datetime.fromisoformat(data["processed_at"])
        return cls(segments=segments, processed_at=processed_at) 