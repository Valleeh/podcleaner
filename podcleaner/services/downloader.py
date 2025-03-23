"""Service for downloading podcast audio files."""

import os
import hashlib
import json
import threading
from typing import Optional, Set, Dict
import requests
import feedparser
from ..logging import get_logger
from ..config import AudioConfig, Config
from .message_broker import Message, MessageBroker, Topics
from .object_storage import ObjectStorage, ObjectStorageError

logger = get_logger(__name__)

class DownloadError(Exception):
    """Raised when podcast download fails."""
    pass

class PodcastDownloader:
    """Service for downloading podcast audio files."""
    
    def __init__(self, config: Config, message_broker: MessageBroker):
        """Initialize the downloader with configuration and message broker."""
        self.audio_config = config.audio
        self.download_dir = config.audio.download_dir
        self.message_broker = message_broker
        self.running = False
        
        # Initialize object storage
        self.object_storage = ObjectStorage(config.object_storage)
        
        # Create local directory for downloads if using local storage
        if config.object_storage.provider == "local":
            os.makedirs(self.download_dir, exist_ok=True)
        
        # Track files being processed and already processed
        self.files_in_process = set()
        self.processed_files = set()
        self.rss_feeds_processed = set()
        self.file_lock = threading.Lock()  # Lock for thread-safe access
        
        # File to persist processed files
        self.debug_dir = "debug_output"
        os.makedirs(self.debug_dir, exist_ok=True)
        self.processed_files_path = os.path.join(self.debug_dir, "downloader_processed_files.json")
        self.rss_feeds_path = os.path.join(self.debug_dir, "downloader_processed_rss.json")
        
        # Load processed files from disk if available
        self._load_processed_data()
        
        # Subscribe to download requests
        self.message_broker.subscribe(
            Topics.DOWNLOAD_REQUEST, 
            self._handle_download_request
        )
        
        # Subscribe to RSS download requests
        self.message_broker.subscribe(
            Topics.RSS_DOWNLOAD_REQUEST,
            self._handle_rss_download_request
        )
    
    def _load_processed_data(self):
        """Load the list of processed files and RSS feeds from disk."""
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
            
        try:
            if os.path.exists(self.rss_feeds_path):
                with open(self.rss_feeds_path, 'r') as f:
                    rss_list = json.load(f)
                    self.rss_feeds_processed = set(rss_list)
                    logger.info("loaded_processed_rss_feeds", count=len(self.rss_feeds_processed))
        except Exception as e:
            logger.error("failed_to_load_processed_rss", error=str(e))
            # Initialize with empty set if loading fails
            self.rss_feeds_processed = set()
    
    def _save_processed_data(self):
        """Save the list of processed files and RSS feeds to disk."""
        try:
            with open(self.processed_files_path, 'w') as f:
                json.dump(list(self.processed_files), f)
            logger.debug("saved_processed_files", count=len(self.processed_files))
        except Exception as e:
            logger.error("failed_to_save_processed_files", error=str(e))
            
        try:
            with open(self.rss_feeds_path, 'w') as f:
                json.dump(list(self.rss_feeds_processed), f)
            logger.debug("saved_processed_rss_feeds", count=len(self.rss_feeds_processed))
        except Exception as e:
            logger.error("failed_to_save_processed_rss", error=str(e))
    
    def _generate_file_path(self, url: str) -> str:
        """Generate a unique file path for the podcast URL."""
        hash_key = hashlib.md5(url.encode()).hexdigest()
        
        # Generate a storage key for the object
        storage_key = f"podcasts/{hash_key}"
        
        # For backward compatibility, also return a local path
        local_path = os.path.join(self.download_dir, hash_key)
        
        return storage_key
    
    def download(self, url: str) -> str:
        """
        Download a podcast from the given URL.
        
        Args:
            url: The URL of the podcast to download.
            
        Returns:
            str: Path or key to the downloaded file.
            
        Raises:
            DownloadError: If the download fails.
        """
        storage_key = self._generate_file_path(url)
        
        # Check if the file already exists in storage
        if self.object_storage.exists(storage_key):
            logger.info("podcast_exists", key=storage_key)
            return storage_key
        
        try:
            logger.info("downloading_podcast", url=url)
            response = requests.get(url, stream=True)
            response.raise_for_status()
            
            # Create a temporary file to download to
            temp_file_path = os.path.join(self.debug_dir, f"temp_{hashlib.md5(url.encode()).hexdigest()}")
            
            with open(temp_file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # Upload to object storage
            try:
                self.object_storage.upload(temp_file_path, storage_key)
                logger.info("podcast_uploaded", key=storage_key)
                
                # Clean up temporary file
                os.remove(temp_file_path)
            except ObjectStorageError as e:
                logger.error("storage_upload_failed", url=url, key=storage_key, error=str(e))
                raise DownloadError(f"Failed to upload podcast to storage: {str(e)}")
            
            # Add to processed files
            with self.file_lock:
                self.processed_files.add(url)
                self._save_processed_data()
                
            logger.info("download_complete", path=storage_key)
            return storage_key
            
        except requests.RequestException as e:
            logger.error("download_failed", url=url, error=str(e))
            raise DownloadError(f"Failed to download podcast: {str(e)}")
        except Exception as e:
            logger.error("unexpected_download_error", url=url, error=str(e))
            raise DownloadError(f"Unexpected error during download: {str(e)}")
    
    def download_rss(self, rss_url: str) -> dict:
        """
        Download an RSS feed and extract podcast episodes.
        
        Args:
            rss_url: The URL of the RSS feed.
            
        Returns:
            dict: Information about the podcast feed and episodes.
            
        Raises:
            DownloadError: If the RSS download fails.
        """
        try:
            logger.info("downloading_rss", url=rss_url)
            feed = feedparser.parse(rss_url)
            
            if feed.bozo:
                logger.warning("rss_parse_warning", url=rss_url, error=str(feed.bozo_exception))
            
            podcast_info = {
                "title": feed.feed.get("title", ""),
                "description": feed.feed.get("description", ""),
                "link": feed.feed.get("link", ""),
                "episodes": []
            }
            
            for entry in feed.entries:
                episode = {
                    "title": entry.get("title", ""),
                    "description": entry.get("description", ""),
                    "published": entry.get("published", ""),
                    "audio_url": None
                }
                
                # Extract the audio URL
                for link in entry.get("links", []):
                    if link.get("rel") == "enclosure" and link.get("type", "").startswith("audio/"):
                        episode["audio_url"] = link.get("href")
                        break
                
                if episode["audio_url"]:
                    podcast_info["episodes"].append(episode)
            
            # Add to processed RSS feeds
            with self.file_lock:
                self.rss_feeds_processed.add(rss_url)
                self._save_processed_data()
                
            logger.info("rss_download_complete", url=rss_url, episodes=len(podcast_info["episodes"]))
            return podcast_info
            
        except Exception as e:
            logger.error("rss_download_failed", url=rss_url, error=str(e))
            raise DownloadError(f"Failed to download RSS feed: {str(e)}")
    
    def _handle_download_request(self, message: Message) -> None:
        """Handle a download request message."""
        if not self.running:
            logger.warning("downloader_not_running")
            return
        
        url = message.data.get("url")
        correlation_id = message.correlation_id
        
        if not url:
            logger.warning("invalid_download_request", message_id=message.message_id)
            self.message_broker.publish(Message(
                topic=Topics.DOWNLOAD_FAILED,
                data={"error": "No URL provided"},
                correlation_id=correlation_id
            ))
            return
        
        # Check if URL is already processed or in process
        with self.file_lock:
            if url in self.processed_files and self.object_storage.exists(self._generate_file_path(url)):
                logger.info("file_already_downloaded", url=url)
                storage_key = self._generate_file_path(url)
                self.message_broker.publish(Message(
                    topic=Topics.DOWNLOAD_COMPLETE,
                    data={
                        "url": url,
                        "file_path": storage_key,
                        "already_processed": True
                    },
                    correlation_id=correlation_id
                ))
                return
            
            if url in self.files_in_process:
                logger.info("file_already_downloading", url=url)
                self.message_broker.publish(Message(
                    topic=Topics.DOWNLOAD_FAILED,
                    data={
                        "url": url,
                        "error": "File is already being downloaded"
                    },
                    correlation_id=correlation_id
                ))
                return
            
            # Mark as in process
            self.files_in_process.add(url)
        
        try:
            storage_key = self.download(url)
            
            # Remove from in-process list
            with self.file_lock:
                if url in self.files_in_process:
                    self.files_in_process.remove(url)
            
            self.message_broker.publish(Message(
                topic=Topics.DOWNLOAD_COMPLETE,
                data={
                    "url": url,
                    "file_path": storage_key
                },
                correlation_id=correlation_id
            ))
        except Exception as e:
            # Remove from in-process list on error
            with self.file_lock:
                if url in self.files_in_process:
                    self.files_in_process.remove(url)
                
            logger.error("download_request_failed", url=url, error=str(e))
            self.message_broker.publish(Message(
                topic=Topics.DOWNLOAD_FAILED,
                data={
                    "url": url,
                    "error": str(e)
                },
                correlation_id=correlation_id
            ))
    
    def _handle_rss_download_request(self, message: Message) -> None:
        """Handle an RSS download request message."""
        if not self.running:
            logger.warning("downloader_not_running")
            return
        
        rss_url = message.data.get("rss_url")
        correlation_id = message.correlation_id
        
        if not rss_url:
            logger.warning("invalid_rss_download_request", message_id=message.message_id)
            self.message_broker.publish(Message(
                topic=Topics.RSS_DOWNLOAD_FAILED,
                data={"error": "No RSS URL provided"},
                correlation_id=correlation_id
            ))
            return
        
        # Check if RSS feed is already processed or in process
        with self.file_lock:
            if rss_url in self.rss_feeds_processed:
                logger.info("rss_already_processed", url=rss_url)
                # We could cache the podcast info structure for faster responses
                # But for now, just re-download it since it's not that expensive
                try:
                    podcast_info = self.download_rss(rss_url)
                    
                    # Replace episode URLs with our server URLs if needed
                    base_url = message.data.get("base_url")
                    if base_url:
                        for episode in podcast_info["episodes"]:
                            original_url = episode["audio_url"]
                            if original_url:
                                episode["original_url"] = original_url
                                episode["audio_url"] = f"{base_url}/process?url={original_url}"
                    
                    self.message_broker.publish(Message(
                        topic=Topics.RSS_DOWNLOAD_COMPLETE,
                        data={
                            "rss_url": rss_url,
                            "podcast_info": podcast_info,
                            "already_processed": True
                        },
                        correlation_id=correlation_id
                    ))
                    return
                except Exception as e:
                    # If re-download fails, just continue to fresh download
                    logger.warning("rss_cached_download_failed", url=rss_url, error=str(e))
        
        try:
            podcast_info = self.download_rss(rss_url)
            
            # Replace episode URLs with our server URLs if needed
            base_url = message.data.get("base_url")
            if base_url:
                for episode in podcast_info["episodes"]:
                    original_url = episode["audio_url"]
                    if original_url:
                        episode["original_url"] = original_url
                        episode["audio_url"] = f"{base_url}/process?url={original_url}"
            
            self.message_broker.publish(Message(
                topic=Topics.RSS_DOWNLOAD_COMPLETE,
                data={
                    "rss_url": rss_url,
                    "podcast_info": podcast_info
                },
                correlation_id=correlation_id
            ))
        except Exception as e:
            logger.error("rss_download_request_failed", rss_url=rss_url, error=str(e))
            self.message_broker.publish(Message(
                topic=Topics.RSS_DOWNLOAD_FAILED,
                data={
                    "rss_url": rss_url,
                    "error": str(e)
                },
                correlation_id=correlation_id
            ))
    
    def start(self) -> None:
        """Start the downloader service."""
        self.running = True
        logger.info("downloader_started")
    
    def stop(self) -> None:
        """Stop the downloader service."""
        self.running = False
        # Save processed files when stopping the service
        self._save_processed_data()
        logger.info("downloader_stopped") 