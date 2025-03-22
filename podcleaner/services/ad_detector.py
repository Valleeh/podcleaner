"""Service for detecting advertisements in podcast transcripts."""

import json
import time
import os
import threading
from typing import List, Dict, Optional, Set
import requests
import openai
from ..logging import get_logger
from ..config import LLMConfig
from ..models import Segment, Transcript, TranscriptChunk, ProcessingResult
from .message_broker import Message, MessageBroker, Topics

logger = get_logger(__name__)

class AdDetectionError(Exception):
    """Raised when ad detection fails."""
    pass

class AdDetector:
    """Service for detecting advertisements in podcast transcripts."""
    
    def __init__(self, config: LLMConfig, message_broker: Optional[MessageBroker] = None):
        """Initialize the ad detector with the specified configuration and message broker."""
        self.config = config
        self.message_broker = message_broker
        self.running = False
        
        # Track files being processed and already processed
        self.files_in_process = set()
        self.processed_files = set()
        self.file_lock = threading.Lock()  # Lock for thread-safe access
        
        # File to persist processed files
        self.debug_dir = "debug_output"
        os.makedirs(self.debug_dir, exist_ok=True)
        self.processed_files_path = os.path.join(self.debug_dir, "processed_files.json")
        
        # Load processed files from disk if available
        self._load_processed_files()
        
        # Set up OpenAI client
        self.client = openai.OpenAI(
            api_key=config.api_key,
            base_url=config.base_url
        )
        
        # Subscribe to ad detection requests if message broker is provided
        if self.message_broker:
            self.message_broker.subscribe(
                Topics.AD_DETECTION_REQUEST,
                self._handle_ad_detection_request
            )
        
        os.makedirs(self.debug_dir, exist_ok=True)
    
    def _load_processed_files(self):
        """Load the list of processed files from disk."""
        try:
            if os.path.exists(self.processed_files_path):
                with open(self.processed_files_path, 'r') as f:
                    files_list = json.load(f)
                    self.processed_files = set(files_list)
                    logger.info("loaded_processed_files", count=len(self.processed_files))
        except Exception as e:
            logger.error("failed_to_load_processed_files", error=str(e))
            # Initialize with empty set if loading fails
            self.processed_files = set()
    
    def _save_processed_files(self):
        """Save the list of processed files to disk."""
        try:
            with open(self.processed_files_path, 'w') as f:
                json.dump(list(self.processed_files), f)
            logger.debug("saved_processed_files", count=len(self.processed_files))
        except Exception as e:
            logger.error("failed_to_save_processed_files", error=str(e))
    
    def _write_debug_info(self, filename: str, data: dict):
        """Write debug information to a file."""
        filepath = os.path.join(self.debug_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.debug("debug_info_written", file=filepath)

    def _create_chunks(self, transcript: Transcript) -> List[TranscriptChunk]:
        """Split the transcript into fixed-size chunks."""
        chunks = []
        for i in range(0, len(transcript.segments), self.config.chunk_size):
            chunk_segments = transcript.segments[i:i + self.config.chunk_size]
            chunks.append(TranscriptChunk(
                segments=chunk_segments,
                chunk_id=i // self.config.chunk_size
            ))
        return chunks
    
    def _build_prompt(self, chunk: TranscriptChunk) -> List[Dict]:
        """Generate the prompt for ad detection."""
        segments_text = "\n".join([
            f"ID: {seg.id} Text: {seg.text}"
            for seg in chunk.segments
        ])
        
        return [{
            "role": "system",
            "content": (
                "You are an AI trained to detect advertisements and sponsored content in podcast transcripts. "
                "Consider the following patterns for ads:\n"
                "1. Transition phrases like 'We'll be right back', 'After this break', etc.\n"
                "2. Promotional content for events, products, or services\n"
                "3. Call to action phrases like 'Visit our website', 'Use code X for discount'\n"
                "4. Sponsor mentions and sponsored content\n"
                "5. Advertisement blocks that start with a transition and end with a return phrase\n\n"
                "You must respond with ONLY a JSON object containing segment classifications. "
                "The response must be a valid JSON object with a 'segments' array containing "
                "'id' (integer) and 'ad' (boolean) fields for each segment. "
                "Do not include any explanations or additional text in your response."
            )
        }, {
            "role": "user",
            "content": (
                "Review the transcript as a continuous text and identify complete advertisement blocks.\n"
                "Important rules:\n"
                "1. If you find a transition to ads (like 'We'll be back after this'), mark it AND the following segments as ads\n"
                "2. If segments are part of the same ad block, they should ALL be marked as ads\n"
                "3. Look for return phrases (like 'Welcome back') to identify where ad blocks end\n"
                "4. Consider promotional content (event announcements, product placements) as ads\n\n"
                f"Segments to analyze:\n{segments_text}\n\n"
                "Return ONLY a JSON object with this structure:\n"
                "{\n"
                '    "segments": [\n'
                '        {"id": <segment_id>, "ad": true/false},\n'
                "        ...\n"
                "    ]\n"
                "}\n"
            )
        }]
    
    def _process_chunk(self, chunk: TranscriptChunk) -> ProcessingResult:
        """Process a single chunk of the transcript."""
        attempts = 0
        last_error = None
        
        while attempts < self.config.max_attempts:
            try:
                messages = self._build_prompt(chunk)
                logger.info("processing_chunk", chunk_id=chunk.chunk_id, 
                          segment_count=len(chunk.segments),
                          segment_ids=[s.id for s in chunk.segments])
                
                # Write input segments to debug file
                self._write_debug_info(
                    f"chunk_{chunk.chunk_id}_input.json",
                    {
                        "chunk_id": chunk.chunk_id,
                        "segments": [
                            {
                                "id": seg.id,
                                "text": seg.text,
                                "start": seg.start,
                                "end": seg.end
                            } for seg in chunk.segments
                        ]
                    }
                )
                
                response = self.client.chat.completions.create(
                    model=self.config.model_name,
                    messages=messages,
                    temperature=self.config.temperature
                )
                
                result = json.loads(response.choices[0].message.content)
                logger.debug("chunk_response_received", 
                           chunk_id=chunk.chunk_id,
                           response_segments=len(result["segments"]),
                           model_name=self.config.model_name)
                
                # Write LLM response to debug file
                self._write_debug_info(
                    f"chunk_{chunk.chunk_id}_llm_response.json",
                    result
                )
                
                # Update segment ad status
                updated_count = 0
                processed_segments = []
                for segment in chunk.segments:
                    matching_result = next(
                        (r for r in result["segments"] if r["id"] == segment.id),
                        None
                    )
                    if matching_result:
                        segment.is_ad = matching_result["ad"]
                        updated_count += 1
                        processed_segments.append({
                            "id": segment.id,
                            "text": segment.text,
                            "start": segment.start,
                            "end": segment.end,
                            "is_ad": segment.is_ad
                        })
                
                # Write processed segments to debug file
                self._write_debug_info(
                    f"chunk_{chunk.chunk_id}_processed.json",
                    {
                        "chunk_id": chunk.chunk_id,
                        "segments": processed_segments,
                        "stats": {
                            "total_segments": len(chunk.segments),
                            "updated_segments": updated_count,
                            "ad_segments": len([s for s in processed_segments if s["is_ad"]])
                        }
                    }
                )
                
                logger.debug("chunk_processing_complete", 
                           chunk_id=chunk.chunk_id,
                           segments_updated=updated_count,
                           segments_total=len(chunk.segments))
                
                return ProcessingResult(
                    chunk_id=chunk.chunk_id,
                    segments=chunk.segments
                )
                
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    "chunk_processing_failed",
                    chunk_id=chunk.chunk_id,
                    attempt=attempts + 1,
                    error=last_error,
                    remaining_attempts=self.config.max_attempts - attempts - 1
                )
                attempts += 1
                if attempts < self.config.max_attempts:
                    time.sleep(2.0)  # Add delay between retries
                    logger.info("retrying_chunk", chunk_id=chunk.chunk_id, attempt=attempts + 1)
        
        return ProcessingResult(
            chunk_id=chunk.chunk_id,
            segments=chunk.segments,
            error=last_error
        )
    
    def _merge_adjacent_ads(self, segments: List[Segment]) -> None:
        """Merge adjacent ad segments into blocks."""
        # First, find segments that are marked as ads by the LLM
        ad_segments = set(i for i, seg in enumerate(segments) if seg.is_ad)
        if not ad_segments:
            return

        # Find the start of an ad block (marked by transition phrases)
        for i, segment in enumerate(segments):
            if i in ad_segments:
                continue
            
            transition_phrases = [
                "nach einer kurzen unterbrechung",
                "bleiben sie dran",
                "wir sind gleich wieder da",
                "gleich geht es weiter"
            ]
            
            if any(phrase in segment.text.lower() for phrase in transition_phrases):
                # Mark this segment as the start of an ad block
                segments[i].is_ad = True
                ad_segments.add(i)
                
                # Look forward to find the end of the ad block
                j = i + 1
                while j < len(segments):
                    if j in ad_segments or self._is_promotional_content(segments[j].text):
                        segments[j].is_ad = True
                        ad_segments.add(j)
                        j += 1
                    else:
                        # Check if the next segment is within 5 seconds
                        if j + 1 < len(segments) and j + 1 in ad_segments:
                            time_gap = segments[j + 1].start - segments[j].end
                            if time_gap <= 5.0:
                                segments[j].is_ad = True
                                ad_segments.add(j)
                                j += 1
                            else:
                                break
                        else:
                            break

    def _is_promotional_content(self, text: str) -> bool:
        """Check if the text contains promotional content indicators."""
        promotional_indicators = [
            "tickets",
            "infos",
            "anmeldung",
            "weitere informationen",
            "sparen sie",
            "rabatt",
            "vorteilscode",
            "jetzt buchen",
            "besuchen sie",
            "mehr erfahren"
        ]
        return any(indicator in text.lower() for indicator in promotional_indicators)

    def detect_ads(self, transcript: Transcript) -> Transcript:
        """
        Detect advertisements in the transcript.
        
        Args:
            transcript: The transcript to process.
            
        Returns:
            Transcript: The processed transcript with ads marked.
            
        Raises:
            AdDetectionError: If ad detection fails.
        """
        chunks = self._create_chunks(transcript)
        logger.info("starting_ad_detection", 
                   total_chunks=len(chunks),
                   total_segments=len(transcript.segments))
        
        # Write initial transcript to debug file
        self._write_debug_info(
            "initial_transcript.json",
            {
                "total_segments": len(transcript.segments),
                "segments": [
                    {
                        "id": seg.id,
                        "text": seg.text,
                        "start": seg.start,
                        "end": seg.end
                    } for seg in transcript.segments
                ]
            }
        )
        
        # Process chunks and collect results
        processed_segments = {}  # Use dict to maintain segment order and avoid duplicates
        errors = []
        
        for i, chunk in enumerate(chunks, 1):
            logger.debug("processing_chunk_progress", 
                        current_chunk=i, 
                        total_chunks=len(chunks),
                        chunk_size=len(chunk.segments))
            
            result = self._process_chunk(chunk)
            
            # Store processed segments in dictionary to maintain uniqueness
            for segment in result.segments:
                processed_segments[segment.id] = segment
            
            if result.error:
                errors.append(f"Chunk {result.chunk_id}: {result.error}")
        
        # Convert processed segments back to list, maintaining order
        all_results = [processed_segments[i] for i in sorted(processed_segments.keys())]
        
        # Merge adjacent ad segments
        self._merge_adjacent_ads(all_results)
        
        # Get ad blocks for logging
        ad_blocks = self._get_ad_blocks(all_results)
        
        # Write final results to debug file
        self._write_debug_info(
            "final_results.json",
            {
                "total_segments": len(all_results),
                "total_ad_segments": len([s for s in all_results if s.is_ad]),
                "errors": errors,
                "segments": [
                    {
                        "id": seg.id,
                        "text": seg.text,
                        "start": seg.start,
                        "end": seg.end,
                        "is_ad": seg.is_ad,
                        "duration": seg.end - seg.start
                    } for seg in all_results
                ],
                "ad_blocks": [
                    {
                        "start_segment": block[0].id,
                        "end_segment": block[-1].id,
                        "start_time": block[0].start,
                        "end_time": block[-1].end,
                        "duration": block[-1].end - block[0].start,
                        "segments": [seg.id for seg in block]
                    }
                    for block in ad_blocks
                ]
            }
        )
        
        # Log detailed ad block information
        for i, block in enumerate(ad_blocks):
            logger.info(
                "ad_block_detected",
                block_number=i + 1,
                start_segment=block[0].id,
                end_segment=block[-1].id,
                start_time=block[0].start,
                end_time=block[-1].end,
                duration=block[-1].end - block[0].start,
                segment_count=len(block)
            )
        
        if errors:
            logger.warning("ad_detection_completed_with_errors", 
                         error_count=len(errors),
                         errors=errors,
                         successful_segments=len(all_results))
        else:
            logger.info("ad_detection_completed",
                       total_segments=len(all_results),
                       ad_segments=len([s for s in all_results if s.is_ad]),
                       ad_blocks=len(ad_blocks))
        
        return Transcript(segments=all_results)
    
    def _get_ad_blocks(self, segments: List[Segment], max_gap: float = 5.0) -> List[List[Segment]]:
        """Get continuous blocks of advertisements."""
        blocks = []
        current_block = []
        
        for i, seg in enumerate(segments):
            if seg.is_ad:
                if not current_block:
                    current_block = [seg]
                else:
                    # Check if this segment is continuous with the current block
                    if (seg.start - current_block[-1].end) <= max_gap:
                        current_block.append(seg)
                    else:
                        blocks.append(current_block)
                        current_block = [seg]
            elif current_block:
                blocks.append(current_block)
                current_block = []
        
        if current_block:
            blocks.append(current_block)
        
        return blocks 

    def _handle_ad_detection_request(self, message: Message) -> None:
        """Handle an ad detection request message."""
        if not self.running:
            logger.warning("ad_detector_not_running")
            return
        
        file_path = message.data.get("file_path")
        transcript_path = message.data.get("transcript_path")
        correlation_id = message.correlation_id
        
        if not file_path or not transcript_path:
            logger.warning("invalid_ad_detection_request", message_id=message.message_id)
            self.message_broker.publish(Message(
                topic=Topics.AD_DETECTION_FAILED,
                data={"error": "Missing file_path or transcript_path"},
                correlation_id=correlation_id
            ))
            return
        
        # Check if file is already processed or in process
        with self.file_lock:
            if file_path in self.processed_files:
                logger.info("file_already_processed", file_path=file_path)
                self.message_broker.publish(Message(
                    topic=Topics.AD_DETECTION_COMPLETE,
                    data={
                        "file_path": file_path,
                        "transcript_path": transcript_path,
                        "already_processed": True
                    },
                    correlation_id=correlation_id
                ))
                return
            
            if file_path in self.files_in_process:
                logger.info("file_already_in_process", file_path=file_path)
                self.message_broker.publish(Message(
                    topic=Topics.AD_DETECTION_IN_PROGRESS,
                    data={
                        "file_path": file_path,
                        "transcript_path": transcript_path,
                    },
                    correlation_id=correlation_id
                ))
                return
            
            # Mark file as in process
            self.files_in_process.add(file_path)
        
        try:
            # Load transcript
            with open(transcript_path, 'r') as f:
                transcript_data = json.load(f)
                transcript = Transcript.from_dict(transcript_data)
            
            # Detect ads
            processed_transcript = self.detect_ads(transcript)
            
            # Save updated transcript
            with open(transcript_path, 'w') as f:
                json.dump(processed_transcript.to_dict(), f, indent=2)
            
            # Mark file as processed and remove from in-process list
            with self.file_lock:
                self.processed_files.add(file_path)
                self.files_in_process.remove(file_path)
                # Save to disk when a new file is processed
                self._save_processed_files()
            
            self.message_broker.publish(Message(
                topic=Topics.AD_DETECTION_COMPLETE,
                data={
                    "file_path": file_path,
                    "transcript_path": transcript_path
                },
                correlation_id=correlation_id
            ))
        except Exception as e:
            # Remove file from in-process list on error
            with self.file_lock:
                self.files_in_process.remove(file_path)
                
            logger.error("ad_detection_request_failed", file=file_path, error=str(e))
            self.message_broker.publish(Message(
                topic=Topics.AD_DETECTION_FAILED,
                data={
                    "file_path": file_path,
                    "error": str(e)
                },
                correlation_id=correlation_id
            ))
    
    def start(self) -> None:
        """Start the ad detector service."""
        self.running = True
        logger.info("ad_detector_started")
    
    def stop(self) -> None:
        """Stop the ad detector service."""
        self.running = False
        # Save processed files when stopping the service
        self._save_processed_files()
        logger.info("ad_detector_stopped") 