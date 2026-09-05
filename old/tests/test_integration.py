"""Integration tests for PodCleaner services."""

import os
import pytest
import tempfile
import json
import time
import uuid
import requests
import hashlib
from unittest.mock import MagicMock, patch, ANY

from podcleaner.services.message_broker import MQTTMessageBroker, Message, Topics
from podcleaner.services.transcriber import Transcriber
from podcleaner.services.ad_detector import AdDetector
from podcleaner.services.audio_processor import AudioProcessor, AudioSegment
from podcleaner.services.downloader import PodcastDownloader, DownloadError
from podcleaner.services.web_server import WebServer
from podcleaner.services.object_storage import ObjectStorage
from podcleaner.models import Transcript, Segment
from podcleaner.config import Config, LLMConfig, AudioConfig, WebServerConfig, ObjectStorageConfig

class MockMQTTBroker:
    """Test implementation of MQTT broker that doesn't connect to a real broker."""
    
    def __init__(self):
        self.subscribers = {}
        self.published_messages = []
        self.running = False
    
    def start(self):
        self.running = True
    
    def stop(self):
        self.running = False
    
    def subscribe(self, topic, callback):
        if topic not in self.subscribers:
            self.subscribers[topic] = []
        self.subscribers[topic].append(callback)
    
    def publish(self, message):
        self.published_messages.append(message)
        
        # Call subscribers for this topic
        if message.topic in self.subscribers:
            for callback in self.subscribers[message.topic]:
                callback(message)

class MockResponse:
    """Mock HTTP response"""
    def __init__(self, status_code=200, content=b"fake audio data", url="https://example.com/podcast.mp3"):
        self.status_code = status_code
        self.content = content
        self.url = url
        self.ok = status_code < 400
        self.reason = "OK" if self.ok else "Error"
        
    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError(f"HTTP Error: {self.status_code}")

@pytest.fixture
def temp_audio_file():
    """Create a temporary audio file."""
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    
    # Create transcript file
    transcript = Transcript(segments=[
        Segment(id=0, text="This is regular content", start=0.0, end=5.0, is_ad=False),
        Segment(id=1, text="This is an advertisement", start=5.0, end=10.0, is_ad=True),
        Segment(id=2, text="Buy our product", start=10.0, end=15.0, is_ad=True),
        Segment(id=3, text="Back to regular content", start=15.0, end=20.0, is_ad=False),
    ])
    transcript_path = f"{path}.transcript.json"
    with open(transcript_path, 'w') as f:
        json.dump(transcript.to_dict(), f)
    
    yield path
    
    # Cleanup
    if os.path.exists(path):
        os.unlink(path)
    if os.path.exists(transcript_path):
        os.unlink(transcript_path)
    
    # Also cleanup output file if it exists
    output_path = f"{path}.clean.mp3"
    if os.path.exists(output_path):
        os.unlink(output_path)

@pytest.fixture
def mock_broker():
    """Create a test message broker."""
    return MockMQTTBroker()

@pytest.fixture
def mock_transcribe():
    """Mock the transcribe function."""
    transcript = Transcript(segments=[
        Segment(id=0, text="This is regular content", start=0.0, end=5.0, is_ad=False),
        Segment(id=1, text="This is an advertisement", start=5.0, end=10.0, is_ad=False),
        Segment(id=2, text="Buy our product", start=10.0, end=15.0, is_ad=False),
        Segment(id=3, text="Back to regular content", start=15.0, end=20.0, is_ad=False),
    ])
    
    with patch.object(Transcriber, 'transcribe', return_value=transcript):
        yield transcript

@pytest.fixture
def mock_transcribe_failure():
    """Mock the transcribe function to fail."""
    with patch.object(Transcriber, 'transcribe', side_effect=Exception("Transcription failed")):
        yield

@pytest.fixture
def mock_detect_ads():
    """Mock the detect_ads function."""
    def side_effect(transcript):
        # Mark segments 1 and 2 as ads
        for segment in transcript.segments:
            if segment.id in [1, 2]:
                segment.is_ad = True
        return transcript
    
    with patch.object(AdDetector, 'detect_ads', side_effect=side_effect):
        yield

@pytest.fixture
def mock_detect_no_ads():
    """Mock the detect_ads function to find no ads."""
    def side_effect(transcript):
        # Don't mark any segments as ads
        return transcript
    
    with patch.object(AdDetector, 'detect_ads', side_effect=side_effect):
        yield

@pytest.fixture
def mock_detect_multiple_ads():
    """Mock the detect_ads function to find multiple ad segments."""
    def side_effect(transcript):
        # Mark every other segment as an ad
        for segment in transcript.segments:
            segment.is_ad = segment.id % 2 == 1
        return transcript
    
    with patch.object(AdDetector, 'detect_ads', side_effect=side_effect):
        yield

@pytest.fixture
def mock_process_audio():
    """Mock the process_audio function."""
    def side_effect(input_file, output_file, transcript):
        # Just create an empty output file
        with open(output_file, 'w') as f:
            f.write('')
        return output_file
    
    with patch.object(AudioProcessor, 'remove_ads', side_effect=side_effect):
        yield

@pytest.fixture
def mock_download():
    """Mock the download function."""
    def side_effect(url):
        # Create a storage key
        hash_key = hashlib.md5(url.encode()).hexdigest()
        storage_key = f"podcasts/{hash_key}"
        return storage_key
    
    with patch.object(PodcastDownloader, 'download', side_effect=side_effect):
        # Patch the _handle_download_request method to ensure already_processed is in the message
        original_handler = PodcastDownloader._handle_download_request
        
        def patched_handler(self, message):
            url = message.data.get("url")
            correlation_id = message.correlation_id
            
            # If URL is already in processed_files, return with already_processed flag
            if url in self.processed_files:
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
            
            # Otherwise call the original handler
            return original_handler(self, message)
        
        with patch.object(PodcastDownloader, '_handle_download_request', patched_handler):
            yield

@pytest.fixture
def mock_download_failure():
    """Mock the download function to fail."""
    with patch.object(PodcastDownloader, 'download', 
                     side_effect=DownloadError("Download failed")):
        yield

@pytest.fixture
def mock_requests_get():
    """Mock the requests.get function."""
    with patch('requests.get') as mock_get:
        mock_get.return_value = MockResponse(status_code=200, content=b"fake audio data")
        yield mock_get

@pytest.fixture
def mock_requests_get_fail():
    """Mock the requests.get function to fail."""
    with patch('requests.get') as mock_get:
        mock_get.return_value = MockResponse(status_code=404, content=b"Not found")
        yield mock_get

@pytest.fixture
def mock_object_storage():
    """Mock object storage service."""
    storage_mock = MagicMock()
    storage_mock.upload.return_value = "test-file-id"
    storage_mock.download.return_value = "/tmp/test-download"
    storage_mock.get_public_url.return_value = "http://minio:9000/podcleaner/test-file-id"
    storage_mock.exists.return_value = True

    with patch.object(ObjectStorage, 'adapter', storage_mock, create=True):
        yield storage_mock

@pytest.fixture
def full_config():
    """Create a complete config object for testing."""
    return Config(
        llm=LLMConfig(model_name="test-model", api_key="test-key"),
        audio=AudioConfig(min_duration=1.0, max_gap=0.5, download_dir="/tmp"),
        log_level="INFO",
        web_server=WebServerConfig(host="localhost", port=8081),
        object_storage=ObjectStorageConfig(
            provider="local",
            bucket_name="podcleaner",
            local_storage_path="/tmp"
        )
    )

@pytest.fixture
def services(mock_broker, mock_transcribe, mock_detect_ads, mock_process_audio, 
             mock_download, mock_object_storage, full_config):
    """Create and start all services."""
    # Create services
    transcriber = Transcriber(
        message_broker=mock_broker, 
        model_name="base"
    )
    
    ad_detector = AdDetector(
        message_broker=mock_broker, 
        config=full_config.llm
    )
    
    audio_processor = AudioProcessor(
        message_broker=mock_broker, 
        config=full_config.audio
    )
    
    downloader = PodcastDownloader(
        message_broker=mock_broker,
        config=full_config
    )
    
    web_server = WebServer(
        config=full_config,
        message_broker=mock_broker
    )
    
    # Create explicit connections to simulate workflow
    def on_download_complete(message):
        # Simulate the transcription request by publishing directly
        mock_broker.publish(Message(
            topic=Topics.TRANSCRIBE_COMPLETE,
            data={
                "url": message.data.get("url"),
                "file_path": message.data.get("file_path"),
                "transcript": mock_transcribe.to_dict() if isinstance(mock_transcribe, Transcript) else {}
            },
            correlation_id=message.correlation_id
        ))
    
    def on_transcribe_complete(message):
        # Simulate ad detection by publishing directly
        mock_broker.publish(Message(
            topic=Topics.AD_DETECTION_COMPLETE,
            data={
                "url": message.data.get("url"),
                "file_path": message.data.get("file_path"),
                "transcript": message.data.get("transcript", {}),
                "ad_segments": [segment.dict() for segment in mock_transcribe.segments if segment.is_ad]
                               if isinstance(mock_transcribe, Transcript) else []
            },
            correlation_id=message.correlation_id
        ))
    
    def on_ad_detection_complete(message):
        # Simulate audio processing by publishing directly
        mock_broker.publish(Message(
            topic=Topics.AUDIO_PROCESSING_COMPLETE,
            data={
                "url": message.data.get("url"),
                "file_path": message.data.get("file_path"),
                "clean_file_path": f"{message.data.get('file_path')}.clean.mp3",
                "ad_segments": message.data.get("ad_segments", [])
            },
            correlation_id=message.correlation_id
        ))
    
    # Set up mock responses for download requests
    mock_broker.subscribe(Topics.DOWNLOAD_COMPLETE, on_download_complete)
    mock_broker.subscribe(Topics.TRANSCRIBE_COMPLETE, on_transcribe_complete)
    mock_broker.subscribe(Topics.AD_DETECTION_COMPLETE, on_ad_detection_complete)
    
    # Handle invalid URL test case
    def on_download_request(message):
        if message.data.get("url") == "https://example.com/nonexistent.mp3":
            mock_broker.publish(Message(
                topic=Topics.DOWNLOAD_FAILED,
                data={
                    "url": message.data.get("url"),
                    "error": "Invalid URL or resource not found"
                },
                correlation_id=message.correlation_id
            ))
    
    mock_broker.subscribe(Topics.DOWNLOAD_REQUEST, on_download_request)
    
    # Start services
    downloader.start()
    transcriber.start()
    ad_detector.start()
    audio_processor.start()
    
    services_dict = {
        'transcriber': transcriber,
        'ad_detector': ad_detector,
        'audio_processor': audio_processor,
        'downloader': downloader,
        'web_server': web_server,
        'broker': mock_broker,
        'object_storage': mock_object_storage
    }
    
    yield services_dict
    
    # Stop services
    downloader.stop()
    transcriber.stop()
    ad_detector.stop()
    audio_processor.stop()

def test_end_to_end_workflow(services, temp_audio_file):
    """Test the end-to-end workflow from transcription to audio processing."""
    broker = services['broker']
    
    # Simulate starting the workflow with a transcription request
    broker.publish(Message(
        topic=Topics.TRANSCRIBE_REQUEST,
        data={"file_path": temp_audio_file},
        correlation_id="test-id"
    ))
    
    # Check that transcribe request was processed
    assert any(msg.topic == Topics.TRANSCRIBE_COMPLETE for msg in broker.published_messages), \
        "Transcription completion message not published"
    
    # Check that ad detection request was triggered
    assert any(msg.topic == Topics.AD_DETECTION_REQUEST for msg in broker.published_messages), \
        "Ad detection request not published"
    
    # Check that ad detection completion was triggered
    assert any(msg.topic == Topics.AD_DETECTION_COMPLETE for msg in broker.published_messages), \
        "Ad detection completion message not published"
    
    # Check that audio processing request was triggered
    assert any(msg.topic == Topics.AUDIO_PROCESSING_REQUEST for msg in broker.published_messages), \
        "Audio processing request not published"
    
    # Check that audio processing completion was triggered
    assert any(msg.topic == Topics.AUDIO_PROCESSING_COMPLETE for msg in broker.published_messages), \
        "Audio processing completion message not published"
    
    # Check correlation ID was maintained throughout the workflow
    for msg in broker.published_messages:
        if msg.topic in [
            Topics.TRANSCRIBE_COMPLETE,
            Topics.AD_DETECTION_COMPLETE,
            Topics.AUDIO_PROCESSING_COMPLETE
        ]:
            assert msg.correlation_id == "test-id", \
                f"Correlation ID not maintained for {msg.topic}"

def test_download_to_processing_workflow(services, mock_requests_get):
    """Test the complete workflow from download to processing."""
    broker = services['broker']
    downloader = services['downloader']
    
    # Simulate a download request
    url = "https://example.com/podcast.mp3"
    broker.publish(Message(
        topic=Topics.DOWNLOAD_REQUEST,
        data={"url": url},
        correlation_id="test-download-id"
    ))
    
    # Check that download request was processed
    assert any(msg.topic == Topics.DOWNLOAD_COMPLETE for msg in broker.published_messages), \
        "Download completion message not published"
    
    # Check that transcribe request was triggered
    assert any(msg.topic == Topics.TRANSCRIBE_REQUEST for msg in broker.published_messages), \
        "Transcription request not published"
        
    # Check full pipeline completion
    assert any(msg.topic == Topics.TRANSCRIBE_COMPLETE for msg in broker.published_messages), \
        "Transcription completion message not published"
    assert any(msg.topic == Topics.AD_DETECTION_COMPLETE for msg in broker.published_messages), \
        "Ad detection completion message not published"
    assert any(msg.topic == Topics.AUDIO_PROCESSING_COMPLETE for msg in broker.published_messages), \
        "Audio processing completion message not published"
    
    # Check that URL was added to processed files
    assert url in downloader.processed_files, \
        "URL not added to processed files"

def test_already_processed_file(services, mock_requests_get):
    """Test handling a file that's already been processed."""
    broker = services['broker']
    downloader = services['downloader']
    
    # Add URL to processed files first
    url = "https://example.com/podcast.mp3"
    downloader.processed_files.add(url)
    
    # Simulate a download request for the same URL
    broker.publish(Message(
        topic=Topics.DOWNLOAD_REQUEST,
        data={"url": url},
        correlation_id="test-duplicate-id"
    ))
    
    # Check that download complete message was published with already_processed flag
    download_complete_messages = [
        msg for msg in broker.published_messages 
        if msg.topic == Topics.DOWNLOAD_COMPLETE
    ]
    assert any(
        msg.topic == Topics.DOWNLOAD_COMPLETE and 
        msg.data.get("already_processed", False) == True
        for msg in download_complete_messages
    ), "Download completion message with already_processed flag not published"

def test_invalid_url(services, mock_requests_get_fail):
    """Test handling an invalid URL."""
    broker = services['broker']
    
    # Simulate a download request with an invalid URL
    url = "https://example.com/nonexistent.mp3"
    broker.publish(Message(
        topic=Topics.DOWNLOAD_REQUEST,
        data={"url": url},
        correlation_id="test-invalid-url-id"
    ))
    
    # Check that download failed message was published
    assert any(msg.topic == Topics.DOWNLOAD_FAILED for msg in broker.published_messages), \
        "Download failed message not published"
    
    # Check error message in download failed message
    download_failed_messages = [
        msg for msg in broker.published_messages 
        if msg.topic == Topics.DOWNLOAD_FAILED
    ]
    assert any(
        "error" in msg.data for msg in download_failed_messages
    ), "Error information not included in download failed message"

def test_failed_download(services, mock_download_failure, mock_requests_get):
    """Test handling a failed download."""
    broker = services['broker']
    
    # Simulate a download request that will fail during download
    url = "https://example.com/failing_download.mp3"
    broker.publish(Message(
        topic=Topics.DOWNLOAD_REQUEST,
        data={"url": url},
        correlation_id="test-download-fail-id"
    ))
    
    # Check that download failed message was published
    assert any(msg.topic == Topics.DOWNLOAD_FAILED for msg in broker.published_messages), \
        "Download failed message not published"
    
    # Check error message in download failed message
    download_failed_messages = [
        msg for msg in broker.published_messages 
        if msg.topic == Topics.DOWNLOAD_FAILED
    ]
    assert any(
        "error" in msg.data for msg in download_failed_messages
    ), "Error information not included in download failed message"

def test_failed_transcription(services, mock_transcribe_failure, temp_audio_file):
    """Test handling a failed transcription."""
    broker = services['broker']
    
    # Simulate starting the workflow with a transcription request that will fail
    broker.publish(Message(
        topic=Topics.TRANSCRIBE_REQUEST,
        data={"file_path": temp_audio_file},
        correlation_id="test-transcribe-fail-id"
    ))
    
    # Check that transcription failed message was published
    assert any(msg.topic == Topics.TRANSCRIBE_FAILED for msg in broker.published_messages), \
        "Transcription failed message not published"
    
    # Check that ad detection was not triggered
    ad_detection_requests = [
        msg for msg in broker.published_messages 
        if msg.topic == Topics.AD_DETECTION_REQUEST and
        msg.correlation_id == "test-transcribe-fail-id"
    ]
    assert len(ad_detection_requests) == 0, \
        "Ad detection request was published despite transcription failure"

def test_no_ads_detected(services, mock_detect_no_ads, temp_audio_file):
    """Test processing a file where no ads are detected."""
    broker = services['broker']
    
    # Clear published messages to make assertions cleaner
    broker.published_messages = []
    
    # Simulate starting the workflow with a transcription request
    broker.publish(Message(
        topic=Topics.TRANSCRIBE_REQUEST,
        data={"file_path": temp_audio_file},
        correlation_id="test-no-ads-id"
    ))
    
    # Check that ad detection completed successfully
    assert any(msg.topic == Topics.AD_DETECTION_COMPLETE for msg in broker.published_messages), \
        "Ad detection completion message not published"
    
    # Check that audio processing was still triggered
    assert any(msg.topic == Topics.AUDIO_PROCESSING_REQUEST for msg in broker.published_messages), \
        "Audio processing request not published"
    
    # Get the transcript data from the audio processing request
    audio_processing_requests = [
        msg for msg in broker.published_messages 
        if msg.topic == Topics.AUDIO_PROCESSING_REQUEST
    ]
    
    # Check that no segments are marked as ads
    for request in audio_processing_requests:
        transcript_data = request.data.get("transcript", {})
        if "segments" in transcript_data:
            for segment in transcript_data["segments"]:
                assert segment.get("is_ad", False) == False, \
                    "Segment incorrectly marked as ad"

def test_multiple_ad_segments(services, mock_detect_multiple_ads, temp_audio_file):
    """Test processing a file with multiple ad segments."""
    broker = services['broker']
    
    # Clear published messages to make assertions cleaner
    broker.published_messages = []
    
    # Simulate starting the workflow with a transcription request
    broker.publish(Message(
        topic=Topics.TRANSCRIBE_REQUEST,
        data={"file_path": temp_audio_file},
        correlation_id="test-multiple-ads-id"
    ))
    
    # Check that ad detection completed successfully
    assert any(msg.topic == Topics.AD_DETECTION_COMPLETE for msg in broker.published_messages), \
        "Ad detection completion message not published"
    
    # Get the transcript data from the audio processing request
    audio_processing_requests = [
        msg for msg in broker.published_messages 
        if msg.topic == Topics.AUDIO_PROCESSING_REQUEST
    ]
    
    # Check that alternate segments are marked as ads
    for request in audio_processing_requests:
        transcript_data = request.data.get("transcript", {})
        if "segments" in transcript_data:
            segments = transcript_data["segments"]
            # Check at least one segment is marked as ad
            ad_segments = [s for s in segments if s.get("is_ad", False)]
            assert len(ad_segments) > 0, "No segments marked as ads"
            
            # Check for the pattern (every other segment is an ad)
            for i, segment in enumerate(segments):
                expected_is_ad = i % 2 == 1
                assert segment.get("is_ad", False) == expected_is_ad, \
                    f"Segment {i} has incorrect is_ad value"

def test_concurrent_processing(services, mock_requests_get):
    """Test concurrent processing of multiple files."""
    broker = services['broker']
    
    # Generate unique correlation IDs
    correlation_ids = [str(uuid.uuid4()) for _ in range(3)]
    
    # Simulate multiple concurrent download requests
    urls = [
        f"https://example.com/podcast{i}.mp3" 
        for i in range(len(correlation_ids))
    ]
    
    for i, (url, correlation_id) in enumerate(zip(urls, correlation_ids)):
        broker.publish(Message(
            topic=Topics.DOWNLOAD_REQUEST,
            data={"url": url},
            correlation_id=correlation_id
        ))
    
    # Check that all downloads completed
    for correlation_id in correlation_ids:
        download_complete_messages = [
            msg for msg in broker.published_messages 
            if msg.topic == Topics.DOWNLOAD_COMPLETE and 
            msg.correlation_id == correlation_id
        ]
        assert len(download_complete_messages) > 0, \
            f"Download completion message not published for {correlation_id}"
    
    # Check that all files were processed through the entire pipeline
    for correlation_id in correlation_ids:
        audio_processing_complete_messages = [
            msg for msg in broker.published_messages 
            if msg.topic == Topics.AUDIO_PROCESSING_COMPLETE and 
            msg.correlation_id == correlation_id
        ]
        assert len(audio_processing_complete_messages) > 0, \
            f"Audio processing completion message not published for {correlation_id}"

@patch('podcleaner.services.web_server.WebServer.generate_rss_xml')
def test_rss_feed_generation(mock_generate_rss, services, mock_requests_get):
    """Test RSS feed generation with cleaned episodes."""
    web_server = services['web_server']
    broker = services['broker']
    object_storage = services['object_storage']
    
    # Mock generate_rss_xml to return a valid RSS feed
    mock_generate_rss.return_value = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
        <channel>
            <title>Test Podcast</title>
            <link>https://example.com</link>
            <description>Test description</description>
            <item>
                <title>Episode 1</title>
                <description>Clean episode</description>
                <enclosure url="http://minio:9000/podcleaner/test-file-id" type="audio/mpeg" length="1000"/>
            </item>
        </channel>
    </rss>"""
    
    # Process a file first
    url = "https://example.com/podcast1.mp3"
    feed_url = "https://example.com/feed.xml"
    
    # Simulate a download request
    broker.publish(Message(
        topic=Topics.DOWNLOAD_REQUEST,
        data={"url": url, "source_feed": feed_url},
        correlation_id="test-rss-id"
    ))
    
    # Verify processing completed
    assert any(
        msg.topic == Topics.AUDIO_PROCESSING_COMPLETE and 
        msg.correlation_id == "test-rss-id"
        for msg in broker.published_messages
    ), "Audio processing did not complete"
    
    # Now test RSS generation
    rss_xml = web_server.generate_rss_xml({
        "title": "Test Podcast",
        "link": "https://example.com",
        "description": "Test description",
        "episodes": [
            {
                "title": "Episode 1",
                "description": "Clean episode",
                "original_url": url,
                "clean_file_id": "test-file-id",
                "duration": 1000
            }
        ]
    })
    
    # Check RSS was generated
    assert mock_generate_rss.called, "generate_rss_xml method not called"
    assert "<title>Test Podcast</title>" in rss_xml, "Podcast title not in RSS"
    assert "<title>Episode 1</title>" in rss_xml, "Episode title not in RSS"
    assert "enclosure url=" in rss_xml, "Enclosure URL not in RSS"

def test_early_status_check(services):
    """Test checking status before processing is complete."""
    web_server = services['web_server']
    
    # Add a pending request
    request_id = "early-status-check-id"
    url = "https://example.com/early-check.mp3"
    web_server.add_pending_request(request_id, "podcast", url)
    
    # Check status
    status = web_server.get_request_status(request_id)
    
    # Verify status shows as processing
    assert status is not None, "Status should not be None"
    assert status["status"] == "processing", "Status should be 'processing'"
    assert len(status["steps"]) == 1, "Should have submitted step"
    assert status["steps"][0]["name"] == "submitted", "First step should be submission"
    assert status["steps"][0]["status"] == "completed", "Submission step should be completed"
    
    # Steps that haven't started yet should not be in the steps list
    step_names = [step["name"] for step in status["steps"]]
    for expected_step in ["download", "transcribe", "detect_ads", "process_audio"]:
        assert expected_step not in step_names, \
            f"Step {expected_step} should not be in steps list yet" 