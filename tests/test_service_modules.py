"""Tests for the service modules."""

import os
import pytest
import signal
import sys
import json
from unittest.mock import patch, MagicMock, mock_open

from podcleaner.run_web_service import main as web_main
from podcleaner.run_transcriber_service import main as transcriber_main
from podcleaner.run_ad_detector_service import main as ad_detector_main
from podcleaner.run_audio_processor_service import main as audio_processor_main
from podcleaner.services.message_broker import Message, Topics
from podcleaner.services.transcriber import Transcriber
from podcleaner.services.ad_detector import AdDetector
from podcleaner.services.audio_processor import AudioProcessor
from podcleaner.services.downloader import PodcastDownloader


@pytest.fixture
def mock_signal():
    """Mock signal handlers."""
    with patch('signal.signal') as mock:
        yield mock

@pytest.fixture
def mock_config():
    """Mock configuration."""
    config_mock = MagicMock()
    config_mock.message_broker.mqtt.host = "test-host"
    config_mock.message_broker.mqtt.port = 1883
    config_mock.message_broker.mqtt.username = None
    config_mock.message_broker.mqtt.password = None
    config_mock.web_server.host = "0.0.0.0"
    config_mock.web_server.port = 8080
    config_mock.web_server.use_https = False
    config_mock.log_level = "INFO"
    config_mock.audio.download_dir = "/tmp"
    config_mock.llm.model_name = "test-model"
    config_mock.llm.api_key = "test-key"
    config_mock.audio.min_duration = 1.0
    config_mock.audio.max_gap = 0.5
    
    with patch('podcleaner.config.load_config', return_value=config_mock):
        yield config_mock

@pytest.fixture
def mock_mqtt_broker_class():
    """Mock MQTTMessageBroker class."""
    with patch('podcleaner.services.message_broker.MQTTMessageBroker') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        # Make sure start and stop methods don't try to connect to a real broker
        mock_instance.start = MagicMock()
        mock_instance.stop = MagicMock()
        yield mock

@pytest.fixture
def mock_transcriber_class():
    """Mock Transcriber class."""
    with patch('podcleaner.services.transcriber.Transcriber') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        yield mock

@pytest.fixture
def mock_ad_detector_class():
    """Mock AdDetector class."""
    with patch('podcleaner.services.ad_detector.AdDetector') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        yield mock

@pytest.fixture
def mock_audio_processor_class():
    """Mock AudioProcessor class."""
    with patch('podcleaner.services.audio_processor.AudioProcessor') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        yield mock

@pytest.fixture
def mock_web_server_class():
    """Mock WebServer class."""
    with patch('podcleaner.services.web_server.WebServer') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        yield mock

@pytest.fixture
def mock_downloader_class():
    """Mock PodcastDownloader class."""
    with patch('podcleaner.services.downloader.PodcastDownloader') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        yield mock

@pytest.fixture
def mock_sleep():
    """Mock sleep function to break infinite loop."""
    def mock_sleep_side_effect(*args, **kwargs):
        raise KeyboardInterrupt()
    
    with patch('time.sleep', side_effect=mock_sleep_side_effect):
        yield

def test_web_service_init(
    mock_config, 
    mock_signal,
    mock_sleep
):
    """Test web service initialization."""
    with patch('sys.argv', ['run_web_service.py']):
        # Create mocks for all services
        mqtt_mock = MagicMock()
        mqtt_mock.start = MagicMock()
        mqtt_mock.stop = MagicMock()
        
        web_server_mock = MagicMock()
        web_server_mock.start = MagicMock()
        
        downloader_mock = MagicMock()
        downloader_mock.start = MagicMock()
        
        with patch('podcleaner.run_web_service.MQTTMessageBroker', return_value=mqtt_mock):
            with patch('podcleaner.run_web_service.WebServer', return_value=web_server_mock):
                with patch('podcleaner.run_web_service.PodcastDownloader', return_value=downloader_mock):
                    # Since we're using the mock_sleep fixture, we don't need to expect KeyboardInterrupt here
                    web_main()
        
        # Check that services were started
        mqtt_mock.start.assert_called_once()
        web_server_mock.start.assert_called_once()
        downloader_mock.start.assert_called_once()

def test_transcriber_service_init(
    mock_config, 
    mock_signal,
    mock_sleep
):
    """Test transcriber service initialization."""
    with patch('sys.argv', ['run_transcriber_service.py']):
        # Create a special mock of MQTTMessageBroker that won't try to connect
        mqtt_mock = MagicMock()
        mqtt_mock.start = MagicMock()
        mqtt_mock.stop = MagicMock()
        
        # Create a special mock of Transcriber
        transcriber_mock = MagicMock()
        transcriber_mock.start = MagicMock()
        transcriber_mock.stop = MagicMock()
        
        with patch('podcleaner.run_transcriber_service.MQTTMessageBroker', return_value=mqtt_mock):
            with patch('podcleaner.run_transcriber_service.Transcriber', return_value=transcriber_mock):
                # Since we're using the mock_sleep fixture, we don't need to expect KeyboardInterrupt here
                transcriber_main()
        
        # Check that transcriber was initialized and started with correct parameters
        transcriber_mock.start.assert_called_once()

def test_ad_detector_service_init(
    mock_config, 
    mock_signal,
    mock_sleep
):
    """Test ad detector service initialization."""
    with patch('sys.argv', ['run_ad_detector_service.py']):
        # Create mocks for services
        mqtt_mock = MagicMock()
        mqtt_mock.start = MagicMock()
        mqtt_mock.stop = MagicMock()
        
        ad_detector_mock = MagicMock()
        ad_detector_mock.start = MagicMock()
        
        with patch('podcleaner.run_ad_detector_service.MQTTMessageBroker', return_value=mqtt_mock):
            with patch('podcleaner.run_ad_detector_service.AdDetector', return_value=ad_detector_mock):
                # Since we're using the mock_sleep fixture, we don't need to expect KeyboardInterrupt here
                ad_detector_main()
        
        # Check that services were started
        mqtt_mock.start.assert_called_once()
        ad_detector_mock.start.assert_called_once()

def test_audio_processor_service_init(
    mock_config, 
    mock_signal,
    mock_sleep
):
    """Test audio processor service initialization."""
    with patch('sys.argv', ['run_audio_processor_service.py']):
        # Create mocks for services
        mqtt_mock = MagicMock()
        mqtt_mock.start = MagicMock()
        mqtt_mock.stop = MagicMock()
        
        audio_processor_mock = MagicMock()
        audio_processor_mock.start = MagicMock()
        
        with patch('podcleaner.run_audio_processor_service.MQTTMessageBroker', return_value=mqtt_mock):
            with patch('podcleaner.run_audio_processor_service.AudioProcessor', return_value=audio_processor_mock):
                # Since we're using the mock_sleep fixture, we don't need to expect KeyboardInterrupt here
                audio_processor_main()
        
        # Check that services were started
        mqtt_mock.start.assert_called_once()
        audio_processor_mock.start.assert_called_once()

def test_transcriber_duplicate_prevention():
    """Test that the Transcriber service prevents duplicate processing."""
    # Create a message broker mock
    message_broker_mock = MagicMock()
    
    # Create the test file paths
    test_file_path = "/tmp/test_podcast.mp3"
    transcript_path = f"{test_file_path}.transcript.json"
    
    # Create a Transcriber instance
    transcriber = Transcriber(message_broker=message_broker_mock)
    transcriber.running = True
    
    # Override processed_files to ensure it's empty
    transcriber.processed_files = set()
    
    # Mock the file operations
    with patch('os.makedirs'), patch('os.path.exists', return_value=False):
        # Create a test message
        message = Message(
            topic=Topics.TRANSCRIBE_REQUEST,
            data={"file_path": test_file_path},
            correlation_id="test-id"
        )
        
        # Mock the transcribe method to avoid actually processing
        with patch.object(transcriber, 'transcribe') as mock_transcribe, \
             patch('builtins.open', mock_open()):
            mock_transcribe.return_value = MagicMock()
            
            # Handle the first request
            transcriber._handle_transcription_request(message)
            
            # Verify it was processed
            mock_transcribe.assert_called_once()
            message_broker_mock.publish.assert_called_once()
            
            # Reset the mocks
            mock_transcribe.reset_mock()
            message_broker_mock.reset_mock()
            
            # Handle the same request again
            transcriber._handle_transcription_request(message)
            
            # Verify it wasn't processed again but a completion message was sent
            mock_transcribe.assert_not_called()
            message_broker_mock.publish.assert_called_once()
            
            # Check that the published message indicates it was already processed
            published_message = message_broker_mock.publish.call_args[0][0]
            assert published_message.topic == Topics.TRANSCRIBE_COMPLETE
            assert published_message.data.get("already_processed") is True

def test_ad_detector_duplicate_prevention():
    """Test that the AdDetector service prevents duplicate processing."""
    # Create a message broker mock
    message_broker_mock = MagicMock()
    
    # Create a config mock
    config_mock = MagicMock()
    config_mock.llm.api_key = "test-key"
    config_mock.llm.model_name = "test-model"
    
    # Create the test file paths
    test_file_path = "/tmp/test_podcast.mp3"
    transcript_path = f"{test_file_path}.transcript.json"
    
    # Mock the OpenAI client to avoid actual API calls
    with patch('openai.OpenAI') as mock_openai:
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        
        # Create an AdDetector instance
        ad_detector = AdDetector(config=config_mock, message_broker=message_broker_mock)
        ad_detector.running = True
        
        # Override processed_files to ensure it's empty
        ad_detector.processed_files = set()
        
        # Mock the file operations
        with patch('os.makedirs'), patch('os.path.exists', return_value=False):
            # Add the file to processed_files
            ad_detector.processed_files.add(test_file_path)
            
            # Create a test message
            message = Message(
                topic=Topics.AD_DETECTION_REQUEST,
                data={
                    "file_path": test_file_path,
                    "transcript_path": transcript_path
                },
                correlation_id="test-id"
            )
            
            # Handle the request (it should be already processed)
            ad_detector._handle_ad_detection_request(message)
            
            # Verify a completion message was sent with already_processed flag
            message_broker_mock.publish.assert_called_once()
            published_message = message_broker_mock.publish.call_args[0][0]
            assert published_message.topic == Topics.AD_DETECTION_COMPLETE
            assert published_message.data.get("already_processed") is True

def test_audio_processor_duplicate_prevention():
    """Test that the AudioProcessor service prevents duplicate processing."""
    # Create a message broker mock
    message_broker_mock = MagicMock()
    
    # Create a config mock
    config_mock = MagicMock()
    config_mock.audio = MagicMock()
    config_mock.audio.min_duration = 1.0
    config_mock.audio.max_gap = 0.5
    
    # Create the test file paths
    test_file_path = "/tmp/test_podcast.mp3"
    transcript_path = f"{test_file_path}.transcript.json"
    
    # Create an AudioProcessor instance
    audio_processor = AudioProcessor(config=config_mock, message_broker=message_broker_mock)
    audio_processor.running = True
    
    # Override processed_files to ensure it's empty
    audio_processor.processed_files = set()
    
    # Mock the file operations
    with patch('os.makedirs'), patch('os.path.exists', return_value=False):
        # Add the file to processed_files
        audio_processor.processed_files.add(test_file_path)
        
        # Create a test message
        message = Message(
            topic=Topics.AUDIO_PROCESSING_REQUEST,
            data={
                "file_path": test_file_path,
                "transcript_path": transcript_path
            },
            correlation_id="test-id"
        )
        
        # Handle the request (it should be already processed)
        audio_processor._handle_audio_processing_request(message)
        
        # Verify a completion message was sent with already_processed flag
        message_broker_mock.publish.assert_called_once()
        published_message = message_broker_mock.publish.call_args[0][0]
        assert published_message.topic == Topics.AUDIO_PROCESSING_COMPLETE
        assert published_message.data.get("already_processed") is True

def test_downloader_duplicate_prevention():
    """Test that the PodcastDownloader service prevents duplicate processing."""
    # Create a message broker mock
    message_broker_mock = MagicMock()
    
    # Create a config mock
    config_mock = MagicMock()
    config_mock.download_dir = "/tmp"
    
    # Create a test URL
    test_url = "https://example.com/podcast.mp3"
    
    # Create a PodcastDownloader instance
    downloader = PodcastDownloader(config=config_mock, message_broker=message_broker_mock)
    downloader.running = True
    
    # Override processed_files to ensure it's empty
    downloader.processed_files = set()
    
    # Mock the file operations
    with patch('os.makedirs'), patch('os.path.exists', return_value=True):
        # Add the URL to processed files
        downloader.processed_files.add(test_url)
        
        # Create a test message
        message = Message(
            topic=Topics.DOWNLOAD_REQUEST,
            data={"url": test_url},
            correlation_id="test-id"
        )
        
        # Handle the request
        with patch.object(downloader, '_generate_file_path') as mock_generate_path:
            mock_generate_path.return_value = "/tmp/test_hash"
            
            downloader._handle_download_request(message)
            
            # Verify it wasn't processed again but a completion message was sent
            message_broker_mock.publish.assert_called_once()
            
            # Check that the published message indicates it was already processed
            published_message = message_broker_mock.publish.call_args[0][0]
            assert published_message.topic == Topics.DOWNLOAD_COMPLETE
            assert published_message.data.get("already_processed") is True 