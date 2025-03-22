"""Service for processing audio files and removing advertisements."""

import os
import json
import threading
from typing import List, Optional, Tuple
from pydub import AudioSegment
from ..logging import get_logger
from ..config import AudioConfig
from ..models import Transcript
from .message_broker import Message, MessageBroker, Topics

logger = get_logger(__name__)

class AudioProcessingError(Exception):
    """Raised when audio processing fails."""
    pass

class AudioProcessor:
    """Service for processing audio files and removing advertisements."""
    
    def __init__(self, config: AudioConfig, message_broker: Optional[MessageBroker] = None):
        """Initialize the processor with configuration and message broker."""
        self.config = config
        self.message_broker = message_broker
        self.running = False
        
        # Subscribe to audio processing requests if message broker is provided
        if self.message_broker:
            self.message_broker.subscribe(
                Topics.AUDIO_PROCESSING_REQUEST,
                self._handle_audio_processing_request
            )
    
    def _merge_segments(self, segments: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """
        Merge overlapping or close segments.
        
        Args:
            segments: List of (start, end) time tuples in seconds.
            
        Returns:
            List[Tuple[float, float]]: Merged segments.
        """
        if not segments:
            return []
            
        # Sort segments by start time
        segments = sorted(segments)
        logger.debug("merging_segments", 
                    initial_segments=len(segments),
                    min_duration=self.config.min_duration,
                    max_gap=self.config.max_gap)
        
        merged = []
        current_start, current_end = segments[0]
        
        for start, end in segments[1:]:
            # If segments overlap or are close enough
            if start <= current_end + self.config.max_gap:
                current_end = max(current_end, end)
                logger.debug("merged_adjacent_segments", 
                           start=start, end=end, 
                           current_end=current_end)
            else:
                # Only keep segments longer than minimum duration
                if current_end - current_start >= self.config.min_duration:
                    merged.append((current_start, current_end))
                    logger.debug("segment_added", 
                               start=current_start, 
                               end=current_end,
                               duration=current_end - current_start)
                current_start, current_end = start, end
        
        # Add the last segment if it's long enough
        if current_end - current_start >= self.config.min_duration:
            merged.append((current_start, current_end))
            logger.debug("final_segment_added", 
                        start=current_start, 
                        end=current_end,
                        duration=current_end - current_start)
        
        logger.info("segments_merged", 
                   initial_count=len(segments),
                   final_count=len(merged))
        return merged
    
    def _get_ad_segments(self, transcript: Transcript) -> List[Tuple[float, float]]:
        """Extract time segments for advertisements."""
        segments = []
        for seg in transcript.ad_segments:
            segments.append((seg.start, seg.end))
        logger.debug("extracted_ad_segments", count=len(segments))
        return self._merge_segments(segments)
    
    def remove_ads(self, input_file: str, output_file: str, transcript: Transcript) -> str:
        """
        Remove advertisements from an audio file.
        
        Args:
            input_file: Path to the input audio file.
            output_file: Path to save the processed audio.
            transcript: Transcript with marked advertisements.
            
        Returns:
            str: Path to the processed audio file.
            
        Raises:
            AudioProcessingError: If processing fails.
        """
        try:
            logger.info("loading_audio", 
                       input_file=input_file,
                       output_file=output_file)
            audio = AudioSegment.from_file(input_file)
            logger.debug("audio_loaded", 
                        duration_ms=len(audio),
                        channels=audio.channels,
                        sample_width=audio.sample_width,
                        frame_rate=audio.frame_rate)
            
            # Get ad segments
            ad_segments = self._get_ad_segments(transcript)
            if not ad_segments:
                logger.info("no_ads_found", input_file=input_file)
                return input_file
            
            logger.info("removing_ads", 
                       segments_count=len(ad_segments),
                       total_duration_ms=sum((end - start) * 1000 for start, end in ad_segments))
            
            # Create output audio by concatenating non-ad segments
            output_audio = AudioSegment.empty()
            current_pos = 0
            segments_processed = 0
            
            for start, end in ad_segments:
                # Add audio up to the ad
                start_ms = int(start * 1000)
                if start_ms > current_pos:
                    segment_duration = start_ms - current_pos
                    output_audio += audio[current_pos:start_ms]
                    logger.debug("non_ad_segment_added", 
                               start_ms=current_pos,
                               end_ms=start_ms,
                               duration_ms=segment_duration)
                current_pos = int(end * 1000)
                segments_processed += 1
                logger.debug("ad_segment_removed",
                           start_ms=start_ms,
                           end_ms=current_pos,
                           duration_ms=current_pos - start_ms,
                           progress=f"{segments_processed}/{len(ad_segments)}")
            
            # Add remaining audio after last ad
            if current_pos < len(audio):
                final_duration = len(audio) - current_pos
                output_audio += audio[current_pos:]
                logger.debug("final_segment_added",
                           start_ms=current_pos,
                           end_ms=len(audio),
                           duration_ms=final_duration)
            
            # Export processed audio
            logger.info("exporting_audio", 
                       output_file=output_file,
                       format=os.path.splitext(output_file)[1][1:],
                       duration_ms=len(output_audio))
            output_audio.export(output_file, format=os.path.splitext(output_file)[1][1:])
            
            logger.info("audio_processing_complete",
                       input_duration_ms=len(audio),
                       output_duration_ms=len(output_audio),
                       reduction_percent=((len(audio) - len(output_audio)) / len(audio)) * 100)
            
            return output_file
            
        except Exception as e:
            logger.error("audio_processing_failed", error=str(e))
            raise AudioProcessingError(f"Failed to process audio: {str(e)}")
    
    def _handle_audio_processing_request(self, message: Message) -> None:
        """Handle an audio processing request message."""
        if not self.running:
            logger.warning("audio_processor_not_running")
            return
        
        file_path = message.data.get("file_path")
        transcript_path = message.data.get("transcript_path")
        correlation_id = message.correlation_id
        
        if not file_path or not transcript_path:
            logger.warning("invalid_audio_processing_request", message_id=message.message_id)
            self.message_broker.publish(Message(
                topic=Topics.AUDIO_PROCESSING_FAILED,
                data={"error": "Missing file_path or transcript_path"},
                correlation_id=correlation_id
            ))
            return
        
        try:
            # Load transcript
            with open(transcript_path, 'r') as f:
                transcript_data = json.load(f)
                transcript = Transcript.from_dict(transcript_data)
            
            # Generate output file path
            base, ext = os.path.splitext(file_path)
            output_path = f"{base}_clean{ext}"
            
            # Process audio
            processed_file = self.remove_ads(file_path, output_path, transcript)
            
            self.message_broker.publish(Message(
                topic=Topics.AUDIO_PROCESSING_COMPLETE,
                data={
                    "input_path": file_path,
                    "output_path": processed_file
                },
                correlation_id=correlation_id
            ))
        except Exception as e:
            logger.error("audio_processing_request_failed", file=file_path, error=str(e))
            self.message_broker.publish(Message(
                topic=Topics.AUDIO_PROCESSING_FAILED,
                data={
                    "file_path": file_path,
                    "error": str(e)
                },
                correlation_id=correlation_id
            ))
    
    def start(self) -> None:
        """Start the audio processor service."""
        self.running = True
        logger.info("audio_processor_started")
    
    def stop(self) -> None:
        """Stop the audio processor service."""
        self.running = False
        logger.info("audio_processor_stopped") 