"""Service for transcribing audio files to text."""

import os
import json
import threading
from typing import List, Optional
import whisper
from ..logging import get_logger
from ..models import Segment, Transcript
from .message_broker import Message, MessageBroker, Topics

logger = get_logger(__name__)

class TranscriptionError(Exception):
    """Raised when transcription fails."""
    pass

class Transcriber:
    """Service for transcribing audio files to text."""
    
    def __init__(self, 
                 message_broker: Optional[MessageBroker] = None, 
                 model_name: str = "base"):
        """Initialize the transcriber with the specified model and message broker."""
        self.model_name = model_name
        self._model = None
        self.message_broker = message_broker
        self.running = False
        
        # Subscribe to transcription requests if message broker is provided
        if self.message_broker:
            self.message_broker.subscribe(
                Topics.TRANSCRIBE_REQUEST,
                self._handle_transcription_request
            )
    
    @property
    def model(self):
        """Lazy load the whisper model."""
        if self._model is None:
            logger.info("loading_model", model=self.model_name)
            self._model = whisper.load_model(self.model_name)
        return self._model
    
    def _convert_whisper_segments(self, result: dict) -> List[Segment]:
        """Convert whisper segments to our Segment model."""
        segments = []
        for i, seg in enumerate(result["segments"]):
            segments.append(Segment(
                id=i,
                text=seg["text"].strip(),
                start=seg["start"],
                end=seg["end"],
                is_ad=False
            ))
        return segments
    
    def transcribe(self, audio_file: str, cache: bool = True) -> Transcript:
        """
        Transcribe an audio file to text.
        
        Args:
            audio_file: Path to the audio file.
            cache: Whether to cache the transcription to disk.
            
        Returns:
            Transcript: The transcribed audio with segments.
            
        Raises:
            TranscriptionError: If transcription fails.
        """
        transcript_file = f"{audio_file}.transcript.json"
        
        # Check cache first
        if cache and os.path.exists(transcript_file):
            logger.info("loading_cached_transcript", file=transcript_file)
            try:
                with open(transcript_file, 'r') as f:
                    data = json.load(f)
                return Transcript.from_dict(data)
            except Exception as e:
                logger.warning("cache_load_failed", error=str(e))
        
        try:
            logger.info("transcribing_audio", file=audio_file)
            result = self.model.transcribe(audio_file)
            segments = self._convert_whisper_segments(result)
            transcript = Transcript(segments=segments)
            
            # Cache the result
            if cache:
                logger.info("caching_transcript", file=transcript_file)
                with open(transcript_file, 'w') as f:
                    json.dump(transcript.to_dict(), f, indent=2)
            
            return transcript
            
        except Exception as e:
            logger.error("transcription_failed", file=audio_file, error=str(e))
            raise TranscriptionError(f"Failed to transcribe audio: {str(e)}")
    
    def _handle_transcription_request(self, message: Message) -> None:
        """Handle a transcription request message."""
        if not self.running:
            logger.warning("transcriber_not_running")
            return
        
        file_path = message.data.get("file_path")
        correlation_id = message.correlation_id
        
        if not file_path:
            logger.warning("invalid_transcription_request", message_id=message.message_id)
            self.message_broker.publish(Message(
                topic=Topics.TRANSCRIBE_FAILED,
                data={"error": "No file path provided"},
                correlation_id=correlation_id
            ))
            return
        
        try:
            transcript = self.transcribe(file_path)
            transcript_path = f"{file_path}.transcript.json"
            
            self.message_broker.publish(Message(
                topic=Topics.TRANSCRIBE_COMPLETE,
                data={
                    "file_path": file_path,
                    "transcript_path": transcript_path
                },
                correlation_id=correlation_id
            ))
        except Exception as e:
            logger.error("transcription_request_failed", file=file_path, error=str(e))
            self.message_broker.publish(Message(
                topic=Topics.TRANSCRIBE_FAILED,
                data={
                    "file_path": file_path,
                    "error": str(e)
                },
                correlation_id=correlation_id
            ))
    
    def start(self) -> None:
        """Start the transcriber service."""
        self.running = True
        logger.info("transcriber_started", model=self.model_name)
    
    def stop(self) -> None:
        """Stop the transcriber service."""
        self.running = False
        logger.info("transcriber_stopped") 