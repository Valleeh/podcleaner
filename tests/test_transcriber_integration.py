"""Integration tests for the Transcriber service."""

import os
import pytest
import tempfile
import json
import time
import threading
from unittest.mock import patch, MagicMock

from podcleaner.services.message_broker import MQTTMessageBroker, Message, Topics
from podcleaner.services.transcriber import Transcriber, TranscriptionError

class TestTranscriberIntegration:
    """Integration tests for the Transcriber service."""
    
    def setup_method(self):
        """Set up test fixtures."""
        # Create a real message broker for integration testing
        self.message_broker = MQTTMessageBroker(
            broker_host="localhost",
            broker_port=1883,
            client_id="test_transcriber"
        )
        
        # Create a lock and events for synchronization
        self.lock = threading.Lock()
        self.transcribe_failed_event = threading.Event()
        self.received_messages = []
        
        # Subscribe to topics we want to monitor
        self.message_broker.subscribe(
            Topics.TRANSCRIBE_FAILED,
            self._handle_transcribe_failed
        )
        
        # Start the message broker
        self.message_broker.start()
    
    def teardown_method(self):
        """Tear down test fixtures."""
        self.message_broker.stop()
    
    def _handle_transcribe_failed(self, message):
        """Handle transcribe failed messages."""
        with self.lock:
            self.received_messages.append(message)
        self.transcribe_failed_event.set()
    
    def test_transcribe_request_with_model_loading_error(self):
        """Test that the transcriber correctly handles model loading errors."""
        # Create a temporary audio file
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"test audio data")
            test_file_path = f.name
        
        try:
            # Mock the whisper module to raise an AttributeError
            with patch('podcleaner.services.transcriber.whisper') as mock_whisper:
                # Set up the mock to raise the error when load_model is called
                mock_whisper.load_model = MagicMock(
                    side_effect=AttributeError("module 'whisper' has no attribute 'load_model'")
                )
                
                # Create and start the transcriber service
                transcriber = Transcriber(
                    message_broker=self.message_broker,
                    model_name="base"
                )
                transcriber.start()
                
                try:
                    # Send a transcribe request
                    correlation_id = "test_request_123"
                    self.message_broker.publish(Message(
                        topic=Topics.TRANSCRIBE_REQUEST,
                        data={"file_path": test_file_path},
                        correlation_id=correlation_id
                    ))
                    
                    # Wait for the failure message to be received
                    received = self.transcribe_failed_event.wait(timeout=5.0)
                    assert received, "Timed out waiting for transcribe failed message"
                    
                    # Check the received message
                    with self.lock:
                        assert len(self.received_messages) > 0, "No failure messages received"
                        failure_message = self.received_messages[0]
                        assert failure_message.topic == Topics.TRANSCRIBE_FAILED
                        assert failure_message.correlation_id == correlation_id
                        assert failure_message.data["file_path"] == test_file_path
                        assert "module 'whisper' has no attribute 'load_model'" in failure_message.data["error"]
                
                finally:
                    # Stop the transcriber
                    transcriber.stop()
                    
        finally:
            # Clean up the temporary file
            if os.path.exists(test_file_path):
                os.unlink(test_file_path) 