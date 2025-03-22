"""Integration tests for PodCleaner services."""

import os
import pytest
import tempfile
import json
from unittest.mock import MagicMock, patch

from podcleaner.services.message_broker import MQTTMessageBroker, Message, Topics
from podcleaner.services.transcriber import Transcriber
from podcleaner.services.ad_detector import AdDetector
from podcleaner.services.audio_processor import AudioProcessor
from podcleaner.models import Transcript, Segment
from podcleaner.config import LLMConfig, AudioConfig

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
def services(mock_broker, mock_transcribe, mock_detect_ads, mock_process_audio):
    """Create and start all services."""
    # Create services
    transcriber = Transcriber(message_broker=mock_broker, model_name="base")
    
    llm_config = LLMConfig(model_name="test-model", api_key="test-key")
    ad_detector = AdDetector(message_broker=mock_broker, config=llm_config)
    
    audio_config = AudioConfig(min_duration=1.0, max_gap=0.5, download_dir="/tmp")
    audio_processor = AudioProcessor(message_broker=mock_broker, config=audio_config)
    
    # Create explicit connections to simulate workflow (in real app, these would come from MQTT subscriptions)
    def on_transcribe_complete(message):
        mock_broker.publish(Message(
            topic=Topics.AD_DETECTION_REQUEST,
            data=message.data,
            correlation_id=message.correlation_id
        ))
    
    def on_ad_detection_complete(message):
        mock_broker.publish(Message(
            topic=Topics.AUDIO_PROCESSING_REQUEST,
            data=message.data,
            correlation_id=message.correlation_id
        ))
    
    # Register handlers manually to ensure message flow
    mock_broker.subscribe(Topics.TRANSCRIBE_COMPLETE, on_transcribe_complete)
    mock_broker.subscribe(Topics.AD_DETECTION_COMPLETE, on_ad_detection_complete)
    
    # Start services
    transcriber.start()
    ad_detector.start()
    audio_processor.start()
    mock_broker.start()
    
    yield {
        'broker': mock_broker,
        'transcriber': transcriber,
        'ad_detector': ad_detector,
        'audio_processor': audio_processor
    }
    
    # Stop services
    transcriber.stop()
    ad_detector.stop()
    audio_processor.stop()
    mock_broker.stop()

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