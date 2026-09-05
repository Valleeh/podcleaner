"""Tests for the transcriber service."""

import os
import json
import pytest
from unittest.mock import MagicMock, patch, mock_open

from podcleaner.services.transcriber import Transcriber, TranscriptionError
from podcleaner.services.message_broker import Message, Topics
from podcleaner.models import Transcript, Segment

@pytest.fixture
def mock_whisper():
    """Mock the whisper module."""
    with patch('podcleaner.services.transcriber.whisper') as mock:
        yield mock

def test_transcriber_init(mock_mqtt_broker):
    """Test transcriber initialization."""
    transcriber = Transcriber(
        message_broker=mock_mqtt_broker,
        model_name="base"
    )
    
    assert transcriber.model_name == "base"
    assert transcriber._model is None
    assert transcriber.message_broker == mock_mqtt_broker
    assert transcriber.running is False
    
    # Check that the transcriber subscribes to the correct topic
    mock_mqtt_broker.subscribe.assert_called_once_with(
        Topics.TRANSCRIBE_REQUEST,
        transcriber._handle_transcription_request
    )

def test_transcriber_model_lazy_loading(mock_whisper):
    """Test that the whisper model is lazy loaded."""
    mock_model = MagicMock()
    mock_whisper.load_model.return_value = mock_model
    
    transcriber = Transcriber(model_name="base")
    
    # Model should not be loaded yet
    mock_whisper.load_model.assert_not_called()
    
    # Access the model property
    model = transcriber.model
    
    # Model should be loaded now
    mock_whisper.load_model.assert_called_once_with("base")
    assert model == mock_model

def test_transcribe_with_cache_hit():
    """Test transcription with a cache hit."""
    mock_transcript = Transcript(segments=[
        Segment(id=0, text="Test segment", start=0.0, end=1.0, is_ad=False)
    ])
    mock_json = json.dumps(mock_transcript.to_dict())
    
    with patch('builtins.open', mock_open(read_data=mock_json)):
        with patch('os.path.exists', return_value=True):
            transcriber = Transcriber(model_name="base")
            result = transcriber.transcribe("test.mp3")
            
            assert isinstance(result, Transcript)
            assert len(result.segments) == 1
            assert result.segments[0].text == "Test segment"

def test_transcribe_with_cache_miss(mock_whisper):
    """Test transcription with a cache miss."""
    mock_model = MagicMock()
    mock_whisper.load_model.return_value = mock_model
    
    mock_result = {
        "segments": [
            {"text": "Test segment", "start": 0.0, "end": 1.0}
        ]
    }
    mock_model.transcribe.return_value = mock_result
    
    with patch('builtins.open', mock_open()):
        with patch('os.path.exists', return_value=False):
            transcriber = Transcriber(model_name="base")
            transcriber._model = mock_model  # Bypass lazy loading
            
            result = transcriber.transcribe("test.mp3")
            
            assert isinstance(result, Transcript)
            assert len(result.segments) == 1
            assert result.segments[0].text == "Test segment"
            mock_model.transcribe.assert_called_once_with("test.mp3")

def test_handle_transcription_request_success(mock_mqtt_broker, mock_whisper):
    """Test handling a transcription request with success."""
    mock_model = MagicMock()
    mock_whisper.load_model.return_value = mock_model
    
    mock_result = {
        "segments": [
            {"text": "Test segment", "start": 0.0, "end": 1.0}
        ]
    }
    mock_model.transcribe.return_value = mock_result
    
    with patch('builtins.open', mock_open()):
        with patch('os.path.exists', return_value=False):
            transcriber = Transcriber(message_broker=mock_mqtt_broker, model_name="base")
            transcriber._model = mock_model  # Bypass lazy loading
            transcriber.running = True
            
            message = Message(
                topic=Topics.TRANSCRIBE_REQUEST,
                data={"file_path": "test.mp3"},
                correlation_id="test-id"
            )
            
            transcriber._handle_transcription_request(message)
            
            # Check that the transcriber published a success message
            mock_mqtt_broker.publish.assert_called_once()
            publish_call_args = mock_mqtt_broker.publish.call_args[0][0]
            assert publish_call_args.topic == Topics.TRANSCRIBE_COMPLETE
            assert publish_call_args.data["file_path"] == "test.mp3"
            assert publish_call_args.data["transcript_path"] == "test.mp3.transcript.json"
            assert publish_call_args.correlation_id == "test-id"

def test_start_stop():
    """Test starting and stopping the transcriber."""
    transcriber = Transcriber(model_name="base")
    
    assert transcriber.running is False
    
    transcriber.start()
    assert transcriber.running is True
    
    transcriber.stop()
    assert transcriber.running is False

def test_model_loading_error():
    """Test that an error is raised when whisper.load_model is not available."""
    with patch('podcleaner.services.transcriber.whisper') as mock_whisper:
        # Set up the mock to raise the actual error
        mock_whisper.load_model.side_effect = AttributeError("module 'whisper' has no attribute 'load_model'")
        
        transcriber = Transcriber(model_name="base")
        
        # Try to access the model property which should trigger the load_model call
        with pytest.raises(AttributeError) as exc_info:
            _ = transcriber.model
        
        # Verify the error message
        assert "module 'whisper' has no attribute 'load_model'" in str(exc_info.value)

def test_transcribe_with_model_loading_error():
    """Test that the transcribe method handles whisper model loading errors properly."""
    with patch('podcleaner.services.transcriber.whisper') as mock_whisper:
        # Set up the mock to raise the error
        mock_whisper.load_model.side_effect = AttributeError("module 'whisper' has no attribute 'load_model'")
        
        transcriber = Transcriber(model_name="base")
        
        # Try to transcribe an audio file which should trigger the model loading
        with pytest.raises(TranscriptionError) as exc_info:
            transcriber.transcribe("test_audio.mp3")
        
        # Verify that the error is wrapped in a TranscriptionError
        assert "Failed to transcribe audio" in str(exc_info.value)
        assert "module 'whisper' has no attribute 'load_model'" in str(exc_info.value) 