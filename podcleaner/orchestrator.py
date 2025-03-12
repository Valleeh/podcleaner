"""Main orchestrator for the PodCleaner package."""

import os
from typing import Optional
from .logging import get_logger
from .config import Config
from .services.downloader import PodcastDownloader
from .services.transcriber import Transcriber
from .services.ad_detector import AdDetector
from .services.audio_processor import AudioProcessor

logger = get_logger(__name__)

class PodcastCleaner:
    """Main orchestrator for cleaning podcasts."""
    
    def __init__(self, config: Config):
        """Initialize the orchestrator with configuration."""
        self.config = config
        
        # Initialize services
        self.downloader = PodcastDownloader(config.audio)
        self.transcriber = Transcriber()
        self.ad_detector = AdDetector(config.llm)
        self.audio_processor = AudioProcessor(config.audio)
    
    def process_podcast(
        self,
        url: str,
        output_file: Optional[str] = None,
        keep_intermediate: bool = False
    ) -> str:
        """
        Process a podcast: download, transcribe, detect ads, and remove them.
        
        Args:
            url: URL of the podcast to process.
            output_file: Path to save the processed audio. If None, generates one.
            keep_intermediate: Whether to keep intermediate files.
            
        Returns:
            str: Path to the processed audio file.
        """
        try:
            # Download podcast
            logger.info("starting_podcast_processing", url=url, output_file=output_file, keep_intermediate=keep_intermediate)
            audio_file = self.downloader.download(url)
            logger.debug("downloaded_podcast", audio_file=audio_file, file_size=os.path.getsize(audio_file))
            
            # Transcribe
            logger.info("transcribing_podcast", audio_file=audio_file)
            transcript = self.transcriber.transcribe(audio_file)
            logger.debug("transcription_complete", transcript_segments=len(transcript.segments))
            
            # Detect advertisements
            logger.info("detecting_advertisements", transcript_id=id(transcript))
            transcript = self.ad_detector.detect_ads(transcript)
            ad_segments = [seg for seg in transcript.segments if seg.is_ad]
            logger.debug("ad_detection_complete", total_segments=len(transcript.segments), 
                        ad_segments=len(ad_segments), 
                        ad_segment_ids=[seg.id for seg in ad_segments])
            
            # Generate output path if not provided
            if output_file is None:
                base, ext = os.path.splitext(audio_file)
                output_file = f"{base}_clean{ext}"
            
            # Remove advertisements
            logger.info("removing_advertisements", input_file=audio_file, output_file=output_file)
            processed_file = self.audio_processor.remove_ads(
                audio_file,
                output_file,
                transcript
            )
            logger.debug("ad_removal_complete", processed_file=processed_file, 
                        output_size=os.path.getsize(processed_file))
            
            # Clean up intermediate files if requested
            if not keep_intermediate and processed_file != audio_file:
                try:
                    os.remove(audio_file)
                    os.remove(f"{audio_file}.transcript.json")
                    logger.debug("cleanup_complete", removed_files=[audio_file, f"{audio_file}.transcript.json"])
                except Exception as e:
                    logger.warning("cleanup_failed", error=str(e), files=[audio_file, f"{audio_file}.transcript.json"])
            
            logger.info("podcast_processing_complete", input_url=url, output=processed_file)
            return processed_file
            
        except Exception as e:
            logger.error("podcast_processing_failed", error=str(e))
            raise 