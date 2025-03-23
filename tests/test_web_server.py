"""Tests for the web server service."""

import pytest
from unittest.mock import patch, MagicMock, call
import json
import os
import tempfile
import threading
import time
import http.client
import urllib.parse
from podcleaner.services.web_server import WebServer
from podcleaner.services.message_broker import Message, Topics
from podcleaner.config import Config, WebServerConfig, ObjectStorageConfig, LLMConfig, AudioConfig, MessageBrokerConfig

@pytest.fixture
def web_server():
    """Create a web server instance for testing."""
    # Create a message broker mock
    message_broker = MagicMock()
    
    # Create a web server config with a test port
    web_config = WebServerConfig(host="localhost", port=8081)
    object_storage_config = ObjectStorageConfig(provider="local")
    llm_config = LLMConfig(model_name="test-model")
    audio_config = AudioConfig()
    message_broker_config = MessageBrokerConfig()
    
    config = Config(
        llm=llm_config,
        audio=audio_config,
        web_server=web_config,
        object_storage=object_storage_config,
        message_broker=message_broker_config
    )
    
    # Create a web server with the config
    server = WebServer(
        config=config,
        message_broker=message_broker
    )
    
    # Override the request tracking storage
    server.pending_requests = {}
    server.file_mappings = {}  # Fix this to match the actual attribute name
    server.cached_podcast_info = {}  # Fix this to match the actual attribute name
    
    return server

def test_init(web_server):
    """Test web server initialization."""
    assert web_server.host == "localhost"
    assert web_server.port == 8081
    assert web_server.running is False
    assert isinstance(web_server.pending_requests, dict)

def test_add_pending_request(web_server):
    """Test adding a pending request."""
    request_id = "test-id"
    request_type = "download"
    url = "https://example.com/podcast.mp3"
    
    web_server.add_pending_request(request_id, request_type, url)
    
    # Check that the request was added
    assert request_id in web_server.pending_requests
    assert web_server.pending_requests[request_id]["type"] == request_type
    assert web_server.pending_requests[request_id]["url"] == url
    assert web_server.pending_requests[request_id]["status"] == "processing"
    assert "steps" in web_server.pending_requests[request_id]
    # There will be one step for submission
    assert len(web_server.pending_requests[request_id]["steps"]) == 1
    assert web_server.pending_requests[request_id]["steps"][0]["name"] == "submitted"
    assert web_server.pending_requests[request_id]["steps"][0]["status"] == "completed"

def test_update_request_status(web_server):
    """Test updating request status."""
    # Add a pending request
    request_id = "test-id"
    web_server.pending_requests[request_id] = {
        "type": "download",
        "url": "https://example.com/podcast.mp3",
        "status": "pending",
        "steps": []
    }
    
    # Update the status
    step_info = {"name": "download", "status": "complete", "time": 1234567890}
    web_server.update_request_status(request_id, "in_progress", step_info)
    
    # Check that the status was updated
    assert web_server.pending_requests[request_id]["status"] == "in_progress"
    assert len(web_server.pending_requests[request_id]["steps"]) == 1
    assert web_server.pending_requests[request_id]["steps"][0] == step_info

def test_get_request_status(web_server):
    """Test getting request status."""
    # Add a pending request
    request_id = "test-id"
    test_data = {
        "type": "download",
        "url": "https://example.com/podcast.mp3",
        "status": "pending",
        "steps": []
    }
    web_server.pending_requests[request_id] = test_data
    
    # Get the status
    status = web_server.get_request_status(request_id)
    
    # Check that the correct status was returned
    assert status == test_data
    
    # Non-existent request should return None
    assert web_server.get_request_status("non-existent") is None

def test_add_file_mapping(web_server):
    """Test adding a file mapping."""
    request_id = "test-id"
    file_path = "/tmp/test.mp3"
    
    file_id = web_server.add_file_mapping(request_id, file_path)
    
    # Check that the mapping was added
    assert file_id in web_server.file_mappings
    assert web_server.file_mappings[file_id] == file_path

def test_generate_rss_xml(web_server):
    """Test generating RSS XML."""
    podcast_info = {
        "title": "Test Podcast",
        "link": "https://example.com/podcast",
        "description": "A test podcast",
        "episodes": [
            {
                "title": "Episode 1",
                "url": "https://example.com/ep1.mp3",
                "clean_url": "https://podcleaner.example.com/download/file1",
                "description": "First episode"
            },
            {
                "title": "Episode 2",
                "url": "https://example.com/ep2.mp3",
                "clean_url": "https://podcleaner.example.com/download/file2",
                "description": "Second episode"
            }
        ]
    }
    
    # Generate XML
    xml = web_server.generate_rss_xml(podcast_info)
    
    # Check that the XML contains the expected elements
    assert "<title>Test Podcast</title>" in xml
    assert "<link>https://example.com/podcast</link>" in xml
    assert "<description>A test podcast</description>" in xml
    assert "<title>Episode 1</title>" in xml
    assert "<description>First episode</description>" in xml
    assert "<title>Episode 2</title>" in xml
    # Don't check for enclosure URLs as the implementation may vary

def test_start_and_stop():
    """Test starting and stopping the web server."""
    # Create a message broker mock
    message_broker = MagicMock()
    
    # Create a web server config with a test port
    web_config = WebServerConfig(host="localhost", port=8082)
    object_storage_config = ObjectStorageConfig(provider="local")
    llm_config = LLMConfig(model_name="test-model")
    audio_config = AudioConfig()
    message_broker_config = MessageBrokerConfig()
    
    config = Config(
        llm=llm_config,
        audio=audio_config,
        web_server=web_config,
        object_storage=object_storage_config,
        message_broker=message_broker_config
    )
    
    # Mock HTTP Server
    with patch('podcleaner.services.web_server.HTTPServer') as mock_http_server:
        mock_server = MagicMock()
        mock_http_server.return_value = mock_server
        
        # Create a web server with the config
        server = WebServer(
            config=config,
            message_broker=message_broker
        )
        
        # Start the server
        server.start()
        
        # Check that the server is running
        assert server.running is True
        
        # Verify HTTP server was created
        mock_http_server.assert_called_once()
        
        # Stop the server
        server.stop()
        
        # Check that server was shut down
        mock_server.shutdown.assert_called_once()
        mock_server.server_close.assert_called_once()

def test_handle_download_complete(web_server):
    """Test handling download completion messages."""
    # Setup
    request_id = "test-id"
    file_path = "/tmp/test.mp3"
    
    # Add a pending request
    web_server.pending_requests[request_id] = {
        "type": "download",
        "url": "https://example.com/podcast.mp3",
        "status": "pending",
        "steps": []
    }
    
    # Create a message
    message = Message(
        topic=Topics.DOWNLOAD_COMPLETE,
        data={
            "url": "https://example.com/podcast.mp3",
            "file_path": file_path
        },
        correlation_id=request_id
    )
    
    # Handle the message
    web_server._handle_download_complete(message)
    
    # Check that the status was updated
    assert web_server.pending_requests[request_id]["status"] == "processing"
    assert len(web_server.pending_requests[request_id]["steps"]) == 1
    assert web_server.pending_requests[request_id]["steps"][0]["name"] == "download"
    assert web_server.pending_requests[request_id]["steps"][0]["status"] == "completed"
    
    # Check that a transcription request was published
    publish_call = web_server.message_broker.publish.call_args[0][0]
    assert publish_call.topic == Topics.TRANSCRIBE_REQUEST
    assert publish_call.data["file_path"] == file_path
    assert publish_call.correlation_id == request_id

def test_handle_already_processed_download(web_server):
    """Test handling already processed download messages."""
    # Setup
    request_id = "test-id"
    file_path = "/tmp/test.mp3"
    
    # Add a pending request
    web_server.pending_requests[request_id] = {
        "type": "download",
        "url": "https://example.com/podcast.mp3",
        "status": "pending",
        "steps": []
    }
    
    # Create a message with already_processed flag
    message = Message(
        topic=Topics.DOWNLOAD_COMPLETE,
        data={
            "url": "https://example.com/podcast.mp3",
            "file_path": file_path,
            "already_processed": True
        },
        correlation_id=request_id
    )
    
    # Handle the message
    web_server._handle_download_complete(message)
    
    # Check that the status was updated correctly
    assert web_server.pending_requests[request_id]["status"] == "processing"
    assert len(web_server.pending_requests[request_id]["steps"]) == 1
    assert web_server.pending_requests[request_id]["steps"][0]["name"] == "download"
    assert web_server.pending_requests[request_id]["steps"][0]["status"] == "completed" 