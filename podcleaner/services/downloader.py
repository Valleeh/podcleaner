"""Service for downloading podcast audio files."""

import os
import hashlib
import threading
from typing import Optional
import requests
import feedparser
from ..logging import get_logger
from ..config import AudioConfig
from .message_broker import Message, MessageBroker, Topics

logger = get_logger(__name__)

class DownloadError(Exception):
    """Raised when podcast download fails."""
    pass

class PodcastDownloader:
    """Service for downloading podcast audio files."""
    
    def __init__(self, config: AudioConfig, message_broker: MessageBroker):
        """Initialize the downloader with configuration and message broker."""
        self.download_dir = config.download_dir
        self.message_broker = message_broker
        self.running = False
        os.makedirs(self.download_dir, exist_ok=True)
        
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
    
    def _generate_file_path(self, url: str) -> str:
        """Generate a unique file path for the podcast URL."""
        hash_key = hashlib.md5(url.encode()).hexdigest()
        return os.path.join(self.download_dir, hash_key)
    
    def download(self, url: str) -> str:
        """
        Download a podcast from the given URL.
        
        Args:
            url: The URL of the podcast to download.
            
        Returns:
            str: Path to the downloaded file.
            
        Raises:
            DownloadError: If the download fails.
        """
        file_path = self._generate_file_path(url)
        
        if os.path.exists(file_path):
            logger.info("podcast_exists", path=file_path)
            return file_path
        
        try:
            logger.info("downloading_podcast", url=url)
            response = requests.get(url, stream=True)
            response.raise_for_status()
            
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            logger.info("download_complete", path=file_path)
            return file_path
            
        except requests.RequestException as e:
            logger.error("download_failed", url=url, error=str(e))
            raise DownloadError(f"Failed to download podcast: {str(e)}")
    
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
        
        try:
            file_path = self.download(url)
            
            self.message_broker.publish(Message(
                topic=Topics.DOWNLOAD_COMPLETE,
                data={
                    "url": url,
                    "file_path": file_path
                },
                correlation_id=correlation_id
            ))
        except Exception as e:
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
        logger.info("downloader_stopped") 