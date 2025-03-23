"""Web server for PodCleaner API."""

import os
import json
import uuid
import time
import html
import http.server
from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver
import threading
import re
import urllib.parse
from urllib.parse import urlparse, parse_qs
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple, Any
from ..logging import get_logger
from ..config import Config
from .message_broker import Message, MessageBroker, Topics
from .object_storage import ObjectStorage, ObjectStorageError
from ..services.downloader import PodcastDownloader
from ..config import AudioConfig

logger = get_logger(__name__)

class RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for PodCleaner API."""
    
    server_version = "PodCleaner/1.0"
    
    def do_GET(self):
        """Handle GET requests."""
        try:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            query = parse_qs(parsed_url.query)
            
            if path == "/process":
                self._handle_process_request(query)
            elif path == "/rss":
                self._handle_rss_request(query)
            elif path == "/status":
                self._handle_status_request(query)
            elif path.startswith("/download/"):
                self._handle_download_request()
            else:
                self.send_error(404, "Not Found")
        except BrokenPipeError:
            # Client disconnected, just log and return silently
            logger.info("client_disconnected", path=self.path)
            return
        except ConnectionResetError:
            # Connection reset by peer, just log and return silently
            logger.info("connection_reset", path=self.path)
            return
        except Exception as e:
            logger.error("request_handler_error", path=self.path, error=str(e))
            self.send_error(500, "Internal Server Error")
    
    def _handle_process_request(self, query):
        """Handle podcast processing request."""
        url = query.get("url", [""])[0]
        if not url:
            self.send_error(400, "Missing URL parameter")
            return
        
        # Get the web server instance
        server = self.server.web_server
        
        # Create request ID for tracking
        request_id = str(uuid.uuid4())
        
        # Check if this is a direct request for an MP3 file
        file_path = server.get_processed_file_path(url)
        if file_path and server.object_storage.exists(file_path):
            # Serve the file directly using the _serve_file method
            file_name = os.path.basename(url)
            server._serve_file(self, file_path, file_name)
            return
        
        # Store request information
        server.add_pending_request(request_id, "process", url)
        
        # Send message to download service
        server.message_broker.publish(Message(
            topic=Topics.DOWNLOAD_REQUEST,
            data={"url": url},
            correlation_id=request_id
        ))
        
        # Respond with a processing message (podcast clients will retry later)
        self.send_response(202)
        self.send_header("Content-Type", "audio/mpeg")
        self.end_headers()
        
        # A message that sounds like static or an error to let the client know to check back
        try:
            self.wfile.write(b"This podcast is being processed. Please try again later.")
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected, log and return silently
            logger.info("client_disconnected_during_response", url=url)
            return
    
    def _handle_rss_request(self, query):
        """Handle RSS feed processing request."""
        rss_url = query.get("url", [""])[0]
        if not rss_url:
            self.send_error(400, "Missing URL parameter")
            return
        
        # Get the web server instance
        server = self.server.web_server
        
        # Check if we already have this RSS feed processed
        cached_podcast_info = server.get_cached_podcast_info(rss_url)
        if cached_podcast_info:
            # Directly return the RSS feed
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            
            # Generate RSS XML from the cached podcast info
            rss_content = server.generate_rss_xml(cached_podcast_info)
            try:
                self.wfile.write(rss_content.encode('utf-8'))
            except (BrokenPipeError, ConnectionResetError):
                # Client disconnected, log and return silently
                logger.info("client_disconnected_during_rss_response", url=rss_url)
                return
            return
        
        # If not cached, directly process and return the RSS feed
        try:
            # Construct base URL for replacement
            host = self.headers.get("Host", "localhost")
            protocol = "https" if self.server.web_server.use_https else "http"
            base_url = f"{protocol}://{host}"
            
            # Directly download and process the RSS feed using our helper function
            podcast_info = self._directly_download_rss(rss_url)
            
            # Replace episode URLs with our server URLs
            for episode in podcast_info["episodes"]:
                original_url = episode["audio_url"]
                if original_url:
                    episode["original_url"] = original_url
                    episode["audio_url"] = f"{base_url}/process?url={original_url}"
            
            # Cache the podcast info for future requests
            server.processed_rss_feeds[rss_url] = podcast_info
            
            # Return the RSS feed
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            
            # Generate RSS XML from the podcast info
            rss_content = server.generate_rss_xml(podcast_info)
            try:
                self.wfile.write(rss_content.encode('utf-8'))
            except (BrokenPipeError, ConnectionResetError):
                # Client disconnected, log and return silently
                logger.info("client_disconnected_during_rss_response", url=rss_url)
                return
            
        except Exception as e:
            logger.error("rss_processing_failed", url=rss_url, error=str(e))
            self.send_error(500, f"Failed to process RSS feed: {str(e)}")
    
    def _directly_download_rss(self, rss_url: str) -> dict:
        """
        Download an RSS feed directly without using the PodcastDownloader class.
        
        Args:
            rss_url: The URL of the RSS feed.
            
        Returns:
            dict: Information about the podcast feed and episodes.
            
        Raises:
            Exception: If the RSS download fails.
        """
        import feedparser
        
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
    
    def _handle_status_request(self, query):
        """Handle status request."""
        request_id = query.get("id", [""])[0]
        if not request_id:
            self.send_error(400, "Missing ID parameter")
            return
        
        # Get the web server instance
        server = self.server.web_server
        
        # Get request status
        status = server.get_request_status(request_id)
        if not status:
            self.send_error(404, "Request not found")
            return
        
        # Send response
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        
        try:
            self.wfile.write(json.dumps(status).encode())
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected, log and return silently
            logger.info("client_disconnected_during_status_response", request_id=request_id)
            return
    
    def _handle_download_request(self):
        """Handle a request to download a processed file."""
        path_parts = self.path.split('/')
        if len(path_parts) < 3:
            self.send_error(400, "Missing file ID")
            return
        
        file_id = path_parts[2]
        server = self.server.web_server
        
        # Get the file path from the file ID
        file_path = server.get_file_path(file_id)
        if not file_path:
            self.send_error(404, "File not found")
            return
        
        # Determine a friendly filename for Content-Disposition
        file_name = f"podcast_{file_id}.mp3"
        
        # Serve the file
        server._serve_file(self, file_path, file_name)

class WebServer:
    """Web server for handling podcast processing requests."""
    
    def __init__(self, config: Config, message_broker: MessageBroker):
        """Initialize the web server."""
        self.config = config
        self.host = config.web_server.host
        self.port = config.web_server.port
        self.message_broker = message_broker
        
        # Initialize object storage
        self.object_storage = ObjectStorage(config.object_storage)
        
        # State tracking
        self.server = None
        self.running = False
        self.pending_requests = {}
        self.file_mappings = {}
        self.url_to_file = {}
        self.cached_podcast_info = {}
        
        # Subscribe to message broker topics
        self._setup_subscriptions()
        
        logger.info("web_server_initialized", host=self.host, port=self.port)
    
    def _setup_subscriptions(self):
        """Subscribe to message broker topics."""
        # Download results
        self.message_broker.subscribe(
            Topics.DOWNLOAD_COMPLETE, 
            self._handle_download_complete
        )
        self.message_broker.subscribe(
            Topics.DOWNLOAD_FAILED,
            self._handle_download_failed
        )
        
        # Transcription results
        self.message_broker.subscribe(
            Topics.TRANSCRIBE_COMPLETE,
            self._handle_transcription_complete
        )
        self.message_broker.subscribe(
            Topics.TRANSCRIBE_FAILED,
            self._handle_transcription_failed
        )
        
        # Ad detection results
        self.message_broker.subscribe(
            Topics.AD_DETECTION_COMPLETE,
            self._handle_ad_detection_complete
        )
        self.message_broker.subscribe(
            Topics.AD_DETECTION_FAILED,
            self._handle_ad_detection_failed
        )
        
        # Audio processing results
        self.message_broker.subscribe(
            Topics.AUDIO_PROCESSING_COMPLETE,
            self._handle_audio_processing_complete
        )
        self.message_broker.subscribe(
            Topics.AUDIO_PROCESSING_FAILED,
            self._handle_audio_processing_failed
        )
        
        # RSS download results
        self.message_broker.subscribe(
            Topics.RSS_DOWNLOAD_COMPLETE,
            self._handle_rss_download_complete
        )
        self.message_broker.subscribe(
            Topics.RSS_DOWNLOAD_FAILED,
            self._handle_rss_download_failed
        )
        
        # Status updates
        self.message_broker.subscribe(
            Topics.API_STATUS_UPDATE,
            self._handle_status_update
        )
    
    def add_pending_request(self, request_id: str, request_type: str, url: str) -> None:
        """Add a pending request to track."""
        self.pending_requests[request_id] = {
            "request_id": request_id,
            "type": request_type,
            "url": url,
            "status": "processing",
            "created_at": time.time(),
            "updated_at": time.time(),
            "steps": [
                {
                    "name": "submitted",
                    "status": "completed",
                    "timestamp": time.time()
                }
            ]
        }
    
    def update_request_status(self, request_id: str, status: str, step: Optional[dict] = None) -> None:
        """Update the status of a pending request."""
        if request_id not in self.pending_requests:
            logger.warning("unknown_request_id", request_id=request_id)
            return
        
        self.pending_requests[request_id]["status"] = status
        self.pending_requests[request_id]["updated_at"] = time.time()
        
        if step:
            self.pending_requests[request_id]["steps"].append(step)
    
    def get_request_status(self, request_id: str) -> Optional[dict]:
        """Get the status of a request."""
        return self.pending_requests.get(request_id)
    
    def add_file_mapping(self, request_id: str, file_path: str) -> str:
        """
        Add a file mapping for download.
        
        Args:
            request_id: The request ID.
            file_path: Path to the processed file.
            
        Returns:
            str: File ID for download URL.
        """
        file_id = str(uuid.uuid4())
        self.file_mappings[file_id] = file_path
        
        # Also map the original URL to the file path if available
        if request_id in self.pending_requests:
            original_url = self.pending_requests[request_id].get("url")
            if original_url:
                self.url_to_file[original_url] = file_path
        
        return file_id
    
    def get_file_path(self, file_id: str) -> Optional[str]:
        """Get the file path for a file ID."""
        return self.file_mappings.get(file_id)
    
    def get_processed_file_path(self, url: str) -> Optional[str]:
        """Get the processed file path for a URL if it exists."""
        return self.url_to_file.get(url)
    
    def get_cached_podcast_info(self, rss_url: str) -> Optional[dict]:
        """Get cached podcast info for an RSS URL if it exists."""
        return self.cached_podcast_info.get(rss_url)
    
    def generate_rss_xml(self, podcast_info: dict) -> str:
        """Generate RSS XML from podcast info."""
        rss = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss, "channel")
        
        # Add channel elements
        ET.SubElement(channel, "title").text = podcast_info.get("title", "PodCleaner Feed")
        ET.SubElement(channel, "link").text = podcast_info.get("link", "")
        ET.SubElement(channel, "description").text = podcast_info.get("description", "Cleaned podcast feed")
        
        # Add items
        for episode in podcast_info.get("episodes", []):
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = episode.get("title", "")
            ET.SubElement(item, "description").text = episode.get("description", "")
            if episode.get("published"):
                ET.SubElement(item, "pubDate").text = episode.get("published")
            
            if episode.get("audio_url"):
                enclosure = ET.SubElement(item, "enclosure")
                enclosure.set("url", episode.get("audio_url"))
                enclosure.set("type", "audio/mpeg")
        
        # Convert to string
        return ET.tostring(rss, encoding='unicode')
    
    def start(self) -> None:
        """Start the web server."""
        if self.running:
            return
            
        self.server = HTTPServer((self.host, self.port), RequestHandler)
        self.server.web_server = self  # Attach the web server instance
        
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()
        
        self.running = True
        logger.info("web_server_started", host=self.host, port=self.port)
    
    def stop(self) -> None:
        """Stop the web server."""
        if not self.running:
            return
            
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        
        self.running = False
        logger.info("web_server_stopped")
    
    def _handle_download_complete(self, message: Message) -> None:
        """Handle download complete message."""
        request_id = message.correlation_id
        file_path = message.data.get("file_path")
        
        if not request_id or not file_path:
            logger.warning("missing_correlation_id_or_file_path", topic=message.topic)
            return
        
        self.update_request_status(
            request_id,
            "processing",
            {
                "name": "download",
                "status": "completed",
                "timestamp": time.time()
            }
        )
        
        # Send message to transcription service
        self.message_broker.publish(Message(
            topic=Topics.TRANSCRIBE_REQUEST,
            data={"file_path": file_path},
            correlation_id=request_id
        ))
    
    def _handle_download_failed(self, message: Message) -> None:
        """Handle download failed message."""
        request_id = message.correlation_id
        error = message.data.get("error")
        
        if not request_id:
            logger.warning("missing_correlation_id", topic=message.topic)
            return
        
        self.update_request_status(
            request_id,
            "failed",
            {
                "name": "download",
                "status": "failed",
                "timestamp": time.time(),
                "error": error
            }
        )
    
    def _handle_transcription_complete(self, message: Message) -> None:
        """Handle transcription complete message."""
        request_id = message.correlation_id
        file_path = message.data.get("file_path")
        transcript_path = message.data.get("transcript_path")
        
        if not request_id or not file_path or not transcript_path:
            logger.warning("missing_correlation_id_or_paths", topic=message.topic)
            return
        
        self.update_request_status(
            request_id,
            "processing",
            {
                "name": "transcription",
                "status": "completed",
                "timestamp": time.time()
            }
        )
        
        # Send message to ad detection service
        self.message_broker.publish(Message(
            topic=Topics.AD_DETECTION_REQUEST,
            data={
                "file_path": file_path,
                "transcript_path": transcript_path
            },
            correlation_id=request_id
        ))
    
    def _handle_transcription_failed(self, message: Message) -> None:
        """Handle transcription failed message."""
        request_id = message.correlation_id
        error = message.data.get("error")
        
        if not request_id:
            logger.warning("missing_correlation_id", topic=message.topic)
            return
        
        self.update_request_status(
            request_id,
            "failed",
            {
                "name": "transcription",
                "status": "failed",
                "timestamp": time.time(),
                "error": error
            }
        )
    
    def _handle_ad_detection_complete(self, message: Message) -> None:
        """Handle ad detection complete message."""
        request_id = message.correlation_id
        file_path = message.data.get("file_path")
        transcript_path = message.data.get("transcript_path")
        
        if not request_id or not file_path or not transcript_path:
            logger.warning("missing_correlation_id_or_paths", topic=message.topic)
            return
        
        self.update_request_status(
            request_id,
            "processing",
            {
                "name": "ad_detection",
                "status": "completed",
                "timestamp": time.time()
            }
        )
        
        # Send message to audio processing service
        self.message_broker.publish(Message(
            topic=Topics.AUDIO_PROCESSING_REQUEST,
            data={
                "file_path": file_path,
                "transcript_path": transcript_path
            },
            correlation_id=request_id
        ))
    
    def _handle_ad_detection_failed(self, message: Message) -> None:
        """Handle ad detection failed message."""
        request_id = message.correlation_id
        error = message.data.get("error")
        
        if not request_id:
            logger.warning("missing_correlation_id", topic=message.topic)
            return
        
        self.update_request_status(
            request_id,
            "failed",
            {
                "name": "ad_detection",
                "status": "failed",
                "timestamp": time.time(),
                "error": error
            }
        )
    
    def _handle_audio_processing_complete(self, message: Message) -> None:
        """Handle audio processing complete message."""
        request_id = message.correlation_id
        output_path = message.data.get("output_path")
        
        if not request_id or not output_path:
            logger.warning("missing_correlation_id_or_output_path", topic=message.topic)
            return
        
        # Create file mapping for download
        file_id = self.add_file_mapping(request_id, output_path)
        
        # Generate download URL
        host = self.host if self.host != "0.0.0.0" else "localhost"
        protocol = "https" if self.config.web_server.use_https else "http"
        download_url = f"{protocol}://{host}:{self.port}/download/{file_id}"
        
        self.update_request_status(
            request_id,
            "completed",
            {
                "name": "audio_processing",
                "status": "completed",
                "timestamp": time.time(),
                "download_url": download_url
            }
        )
    
    def _handle_audio_processing_failed(self, message: Message) -> None:
        """Handle audio processing failed message."""
        request_id = message.correlation_id
        error = message.data.get("error")
        
        if not request_id:
            logger.warning("missing_correlation_id", topic=message.topic)
            return
        
        self.update_request_status(
            request_id,
            "failed",
            {
                "name": "audio_processing",
                "status": "failed",
                "timestamp": time.time(),
                "error": error
            }
        )
    
    def _handle_rss_download_complete(self, message: Message) -> None:
        """
        Handle RSS download complete message.
        
        This callback is still needed to cache processed RSS feeds so that
        subsequent requests can be served directly.
        """
        request_id = message.correlation_id
        podcast_info = message.data.get("podcast_info")
        rss_url = message.data.get("rss_url")
        
        if not request_id or not podcast_info or not rss_url:
            logger.warning("missing_correlation_id_or_podcast_info", topic=message.topic)
            return
        
        # Cache podcast info for future requests
        self.cached_podcast_info[rss_url] = podcast_info
        
        self.update_request_status(
            request_id,
            "completed",
            {
                "name": "rss_download",
                "status": "completed",
                "timestamp": time.time()
            }
        )
        
        # Add podcast info to request
        self.pending_requests[request_id]["podcast_info"] = podcast_info
    
    def _handle_rss_download_failed(self, message: Message) -> None:
        """Handle RSS download failed message."""
        request_id = message.correlation_id
        error = message.data.get("error")
        
        if not request_id:
            logger.warning("missing_correlation_id", topic=message.topic)
            return
        
        self.update_request_status(
            request_id,
            "failed",
            {
                "name": "rss_download",
                "status": "failed",
                "timestamp": time.time(),
                "error": error
            }
        )
    
    def _handle_status_update(self, message: Message) -> None:
        """Handle status update message."""
        request_id = message.correlation_id
        status = message.data.get("status")
        step = message.data.get("step")
        
        if not request_id or not status:
            logger.warning("missing_correlation_id_or_status", topic=message.topic)
            return
        
        self.update_request_status(request_id, status, step)
    
    def _serve_file(self, handler, file_path: str, file_name: Optional[str] = None):
        """Serve a file to the client."""
        try:
            # Determine content type based on file extension
            content_type = "application/octet-stream"
            if file_path.endswith(".mp3"):
                content_type = "audio/mpeg"
            elif file_path.endswith(".wav"):
                content_type = "audio/wav"
            
            # Get the file data from object storage
            try:
                file_data = self.object_storage.download(file_path)
                file_size = len(file_data)
                
                # Set up response headers
                handler.send_response(200)
                handler.send_header("Content-Type", content_type)
                handler.send_header("Content-Length", str(file_size))
                
                # Add Content-Disposition header if file_name is provided
                if file_name:
                    handler.send_header(
                        "Content-Disposition", 
                        f'attachment; filename="{file_name}"'
                    )
                
                handler.end_headers()
                
                # Send the file data
                handler.wfile.write(file_data)
                logger.info("file_served", path=file_path, size=file_size)
                
            except ObjectStorageError as e:
                logger.error("file_serving_failed", path=file_path, error=str(e))
                handler.send_error(404, f"File not found: {file_path}")
                
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected, log and return silently
            logger.info("client_disconnected_during_download", path=file_path)
            return 