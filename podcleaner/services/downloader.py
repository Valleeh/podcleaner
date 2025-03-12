"""Service for downloading podcast audio files."""

import os
import hashlib
from typing import Optional
import requests
from ..logging import get_logger
from ..config import AudioConfig

logger = get_logger(__name__)

class DownloadError(Exception):
    """Raised when podcast download fails."""
    pass

class PodcastDownloader:
    """Service for downloading podcast audio files."""
    
    def __init__(self, config: AudioConfig):
        """Initialize the downloader with configuration."""
        self.download_dir = config.download_dir
        os.makedirs(self.download_dir, exist_ok=True)
    
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