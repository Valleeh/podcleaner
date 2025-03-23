"""Tests for the podcast downloader service."""

import os
import pytest
import json
from unittest.mock import patch, MagicMock, mock_open, call
import tempfile
import shutil
from podcleaner.services.downloader import PodcastDownloader, DownloadError
from podcleaner.services.message_broker import Message, Topics
from podcleaner.config import AudioConfig
import requests

@pytest.fixture
def temp_dir():
    """Create a temporary directory for downloads."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)

@pytest.fixture
def downloader(temp_dir):
    """Create a downloader instance for testing."""
    config = AudioConfig(download_dir=temp_dir)
    message_broker = MagicMock()
    
    downloader = PodcastDownloader(config=config, message_broker=message_broker)
    
    # Override the processed files sets and paths
    downloader.processed_files = set()
    downloader.rss_feeds_processed = set()
    downloader.processed_files_path = os.path.join(temp_dir, "processed_files.json")
    downloader.rss_feeds_path = os.path.join(temp_dir, "processed_rss.json")
    
    return downloader

def test_generate_file_path(downloader):
    """Test generating a file path from a URL."""
    url = "https://example.com/podcast.mp3"
    file_path = downloader._generate_file_path(url)
    
    # Check that the path is correct
    assert file_path.startswith(downloader.download_dir)
    # Should be a hash, not ending with .mp3
    assert not file_path.endswith(".mp3")
    
    # Generate another path for the same URL - should be the same
    file_path2 = downloader._generate_file_path(url)
    assert file_path == file_path2
    
    # Different URL should have different path
    url2 = "https://example.com/another-podcast.mp3"
    file_path3 = downloader._generate_file_path(url2)
    assert file_path != file_path3

@patch("requests.get")
def test_download_success(mock_get, downloader, temp_dir):
    """Test successful podcast download."""
    # Mock the response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.iter_content.return_value = [b"test audio content"]
    mock_get.return_value = mock_response
    
    # Download a test URL
    url = "https://example.com/podcast.mp3"
    file_path = downloader.download(url)
    
    # Verify the request was made
    mock_get.assert_called_once_with(url, stream=True)
    
    # Check that the file was created
    assert os.path.exists(file_path)
    
    # Verify the file contains the expected content
    with open(file_path, "rb") as f:
        content = f.read()
        assert content == b"test audio content"
    
    # URL should be added to processed files
    assert url in downloader.processed_files

@patch("requests.get")
def test_download_error(mock_get, downloader):
    """Test handling of download errors."""
    # Mock the response for a failed download with an exception
    mock_get.side_effect = requests.RequestException("Not Found")
    
    # Attempt to download
    url = "https://example.com/not-found.mp3"
    
    # Should raise DownloadError
    with pytest.raises(DownloadError):
        downloader.download(url)
    
    # URL should not be in processed files
    assert url not in downloader.processed_files

@patch("feedparser.parse")
def test_download_rss_success(mock_parse, downloader):
    """Test successful RSS feed parsing."""
    # Create a mock feed object that mimics feedparser's output
    mock_feed = MagicMock()
    mock_feed.bozo = False
    mock_feed.feed = {
        "title": "Test Podcast",
        "description": "A test podcast",
        "link": "https://example.com/podcast"
    }
    mock_feed.entries = [
        {
            "title": "Episode 1",
            "description": "First episode",
            "published": "Mon, 01 Jan 2023 00:00:00 +0000",
            "links": [
                {
                    "rel": "enclosure",
                    "type": "audio/mpeg",
                    "href": "https://example.com/ep1.mp3"
                }
            ]
        },
        {
            "title": "Episode 2",
            "description": "Second episode",
            "published": "Mon, 02 Jan 2023 00:00:00 +0000",
            "links": [
                {
                    "rel": "enclosure",
                    "type": "audio/mpeg",
                    "href": "https://example.com/ep2.mp3"
                }
            ]
        }
    ]
    mock_parse.return_value = mock_feed
    
    # Download and parse RSS
    rss_url = "https://example.com/podcast.xml"
    result = downloader.download_rss(rss_url)
    
    # Verify feedparser was called
    mock_parse.assert_called_once_with(rss_url)
    
    # Check the result
    assert result["title"] == "Test Podcast"
    assert len(result["episodes"]) == 2
    assert result["episodes"][0]["audio_url"] == "https://example.com/ep1.mp3"
    
    # RSS URL should be added to processed feeds
    assert rss_url in downloader.rss_feeds_processed

@patch("podcleaner.services.downloader.PodcastDownloader.download")
def test_handle_download_request(mock_download, downloader):
    """Test handling of download requests."""
    # Set the service to running
    downloader.running = True
    
    # Setup
    url = "https://example.com/podcast.mp3"
    file_path = os.path.join(downloader.download_dir, "test.mp3")
    mock_download.return_value = file_path
    
    # Create a test message
    message = Message(
        topic=Topics.DOWNLOAD_REQUEST,
        data={"url": url},
        correlation_id="test-id"
    )
    
    # Handle the request
    downloader._handle_download_request(message)
    
    # Verify download was called
    mock_download.assert_called_once_with(url)
    
    # Verify publish was called with the right message
    publish_call = downloader.message_broker.publish.call_args[0][0]
    assert publish_call.topic == Topics.DOWNLOAD_COMPLETE
    assert publish_call.data["file_path"] == file_path
    assert publish_call.correlation_id == "test-id"

def test_handle_already_processed_file(downloader):
    """Test handling of already processed files."""
    # Set the service to running
    downloader.running = True
    
    # Add a URL to processed files
    url = "https://example.com/podcast.mp3"
    downloader.processed_files.add(url)
    
    # Create file path to ensure it exists for the test
    file_path = downloader._generate_file_path(url)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'wb') as f:
        f.write(b"test content")
    
    # Create a test message
    message = Message(
        topic=Topics.DOWNLOAD_REQUEST,
        data={"url": url},
        correlation_id="test-id"
    )
    
    # Handle the request
    downloader._handle_download_request(message)
    
    # Verify publish was called with already_processed flag
    publish_call = downloader.message_broker.publish.call_args[0][0]
    assert publish_call.topic == Topics.DOWNLOAD_COMPLETE
    assert publish_call.data["already_processed"] is True
    assert publish_call.correlation_id == "test-id"

@patch("podcleaner.services.downloader.PodcastDownloader.download_rss")
def test_handle_rss_download_request(mock_download_rss, downloader):
    """Test handling of RSS download requests."""
    # Set the service to running
    downloader.running = True
    
    # Setup
    rss_url = "https://example.com/podcast.xml"
    mock_download_rss.return_value = {
        "title": "Test Podcast",
        "episodes": [
            {"title": "Episode 1", "audio_url": "https://example.com/ep1.mp3"},
            {"title": "Episode 2", "audio_url": "https://example.com/ep2.mp3"}
        ]
    }
    
    # Create a test message
    message = Message(
        topic=Topics.RSS_DOWNLOAD_REQUEST,
        data={"rss_url": rss_url},
        correlation_id="test-id"
    )
    
    # Handle the request
    downloader._handle_rss_download_request(message)
    
    # Verify download_rss was called
    mock_download_rss.assert_called_once_with(rss_url)
    
    # Verify publish was called with the right message
    publish_call = downloader.message_broker.publish.call_args[0][0]
    assert publish_call.topic == Topics.RSS_DOWNLOAD_COMPLETE
    assert publish_call.data["rss_url"] == rss_url
    assert "podcast_info" in publish_call.data
    assert publish_call.correlation_id == "test-id"

def test_start_and_stop(downloader):
    """Test starting and stopping the downloader service."""
    # Start the service
    downloader.start()
    
    # Verify subscriptions
    downloader.message_broker.subscribe.assert_has_calls([
        call(Topics.DOWNLOAD_REQUEST, downloader._handle_download_request),
        call(Topics.RSS_DOWNLOAD_REQUEST, downloader._handle_rss_download_request)
    ])
    
    # Verify running flag
    assert downloader.running is True
    
    # Stop the service
    downloader.stop()
    
    # Verify running flag
    assert downloader.running is False 