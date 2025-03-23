"""Tests for the main CLI module."""

import pytest
from unittest.mock import patch, MagicMock
import sys
import importlib
from podcleaner.__main__ import parse_args

def test_parse_args_process_mode():
    """Test argument parsing for process mode."""
    args = parse_args(["process", "https://example.com/podcast.mp3"])
    assert args.mode == "process"
    assert args.url == "https://example.com/podcast.mp3"
    assert args.output is None
    
    # Test with output option
    args = parse_args(["process", "https://example.com/podcast.mp3", "-o", "output.mp3"])
    assert args.mode == "process"
    assert args.url == "https://example.com/podcast.mp3"
    assert args.output == "output.mp3"

def test_parse_args_service_mode():
    """Test argument parsing for service mode."""
    args = parse_args(["service", "--service", "web"])
    assert args.mode == "service"
    assert args.service == "web"

@patch("podcleaner.__main__.load_config")
@patch("podcleaner.__main__.configure_logging")
@patch("podcleaner.__main__.MQTTMessageBroker")
@patch("podcleaner.__main__.WebServer")
@patch("podcleaner.__main__.PodcastDownloader")
def test_main_process_mode(mock_downloader, mock_web, mock_broker, mock_logging, mock_config):
    """Test main function in process mode."""
    # Mock config
    mock_config.return_value = MagicMock()
    
    # Mock the message broker
    broker_instance = MagicMock()
    published_messages = []
    
    # Create a custom publish method that captures messages
    def record_publish(message):
        published_messages.append(message)
    
    broker_instance.publish.side_effect = record_publish
    mock_broker.return_value = broker_instance
    
    # Mock PodcastDownloader
    downloader_instance = MagicMock()
    mock_downloader.return_value = downloader_instance
    
    # Import the main function dynamically to avoid running it on import
    with patch.object(sys, "argv", ["podcleaner", "process", "https://example.com/podcast.mp3"]):
        from podcleaner.__main__ import main
        
        # Patch signal handler to prevent it from running
        with patch("podcleaner.__main__.signal.signal"):
            # Mock time.sleep to exit loop after one iteration
            with patch("podcleaner.__main__.time.sleep", side_effect=KeyboardInterrupt):
                main()
                
    # Verify message broker was started
    broker_instance.start.assert_called_once()
    
    # Verify subscriptions to completion topics
    assert broker_instance.subscribe.call_count >= 4
    
    # Verify a publish call was made with the URL
    assert broker_instance.publish.called
    
    # Check if any of the published messages contain our URL
    url_found = False
    for message in published_messages:
        if hasattr(message, 'data') and isinstance(message.data, dict) and message.data.get('url') == "https://example.com/podcast.mp3":
            url_found = True
            break
    
    assert url_found, "No message published with the expected URL"

@patch("podcleaner.__main__.load_config")
@patch("podcleaner.__main__.configure_logging")
@patch("podcleaner.__main__.importlib.import_module")
def test_main_service_mode(mock_import, mock_logging, mock_config):
    """Test main function in service mode."""
    # Mock config
    mock_config.return_value = MagicMock()
    
    # Mock the run_service module
    mock_run_service = MagicMock()
    mock_import.return_value = mock_run_service
    
    # Import the main function dynamically to avoid running it on import
    with patch.object(sys, "argv", ["podcleaner", "service", "--service", "web"]):
        from podcleaner.__main__ import main
        
        # Run main
        main()
        
    # Verify run_service.main was called
    mock_run_service.main.assert_called_once() 