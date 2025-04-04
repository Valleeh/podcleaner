import pytest
from unittest.mock import MagicMock, patch
from podcleaner.config import LLMConfig
from podcleaner.models import Segment, Transcript
from podcleaner.services.ad_detector import AdDetector
from podcleaner.services.message_broker import Message, Topics

@pytest.fixture
def mock_openai():
    with patch('openai.Client') as mock:
        yield mock

def test_merge_adjacent_ads(mock_openai):
    """Test that adjacent ad segments are properly merged."""
    config = LLMConfig(model_name="test-model", api_key="test-key")
    detector = AdDetector(config)
    
    # Create test segments that simulate a real ad block
    segments = [
        Segment(id=147, text="Some content", start=460.0, end=464.0, is_ad=False),
        Segment(id=148, text="Nach einer kurzen Unterbrechung geht es gleich weiter.", start=464.2, end=467.84, is_ad=False),
        Segment(id=149, text="Bleiben Sie dran.", start=467.92, end=469.2, is_ad=False),
        Segment(id=150, text="Die Wasserstoffwirtschaft stoppt, doch die Zeit drängt.", start=469.72, end=472.88, is_ad=False),
        Segment(id=151, text="Wasserstoff kann ein Schlüssel für den Klimaschutz sein", start=472.96, end=475.72, is_ad=False),
        Segment(id=152, text="als Rohstoff für die Industrie, Speicher für Grünestrom oder Erdgasersatz.", start=475.8, end=480.24, is_ad=False),
        Segment(id=153, text="Aber wie kommen wir schneller voran?", start=480.32, end=482.24, is_ad=False),
        Segment(id=154, text="Antworten gibt der Handelsblatt Wasserstoffgipfel 2025.", start=482.32, end=486.56, is_ad=True),
        Segment(id=155, text="Am 21. und 22. Mai in Saarbrücken.", start=486.56, end=489.88, is_ad=True),
        Segment(id=156, text="Alle Infos und Tickets auf Handelsblatt-Wasserstoffgipfel.de.", start=489.96, end=494.04, is_ad=True),
        Segment(id=157, text="Mit dem Vorteilskot Wasserstoff 2025 sparen sie 15 Prozent.", start=494.12, end=499.56, is_ad=True),
        Segment(id=158, text="Regular content continues...", start=499.6, end=505.0, is_ad=False),
    ]
    
    # Test merging
    detector._merge_adjacent_ads(segments)
    
    # Verify that segments are correctly marked as ads:
    # 1. Segments with transition phrases (148-149)
    # 2. Segments marked as ads by LLM (154-157)
    # 3. Segments with promotional content
    expected_ad_segments = {148, 149, 154, 155, 156, 157}
    actual_ad_segments = set(seg.id for seg in segments if seg.is_ad)
    
    assert actual_ad_segments == expected_ad_segments, \
        f"Expected segments {expected_ad_segments} to be ads, but got {actual_ad_segments}"
    
    # Verify that segments are properly connected
    ad_segments = [seg for seg in segments if seg.is_ad]
    for i in range(len(ad_segments) - 1):
        time_gap = ad_segments[i + 1].start - ad_segments[i].end
        assert time_gap <= 5.0 or ad_segments[i + 1].id - ad_segments[i].id > 1, \
            f"Segments {ad_segments[i].id} and {ad_segments[i + 1].id} should be connected"

def test_get_ad_blocks(mock_openai):
    """Test that ad blocks are properly identified."""
    config = LLMConfig(model_name="test-model", api_key="test-key")
    detector = AdDetector(config)
    
    # Create test segments
    segments = [
        Segment(id=147, text="Some content", start=460.0, end=464.0, is_ad=False),
        Segment(id=148, text="Nach einer kurzen Unterbrechung geht es gleich weiter.", start=464.2, end=467.84, is_ad=True),
        Segment(id=149, text="Bleiben Sie dran.", start=467.92, end=469.2, is_ad=True),
        Segment(id=150, text="Die Wasserstoffwirtschaft stoppt, doch die Zeit drängt.", start=469.72, end=472.88, is_ad=True),
        Segment(id=151, text="Wasserstoff kann ein Schlüssel für den Klimaschutz sein", start=472.96, end=475.72, is_ad=True),
        Segment(id=152, text="als Rohstoff für die Industrie, Speicher für Grünestrom oder Erdgasersatz.", start=475.8, end=480.24, is_ad=True),
        Segment(id=153, text="Aber wie kommen wir schneller voran?", start=480.32, end=482.24, is_ad=True),
        Segment(id=154, text="Antworten gibt der Handelsblatt Wasserstoffgipfel 2025.", start=482.32, end=486.56, is_ad=True),
        Segment(id=155, text="Am 21. und 22. Mai in Saarbrücken.", start=486.56, end=489.88, is_ad=True),
        Segment(id=156, text="Alle Infos und Tickets auf Handelsblatt-Wasserstoffgipfel.de.", start=489.96, end=494.04, is_ad=True),
        Segment(id=157, text="Mit dem Vorteilskot Wasserstoff 2025 sparen sie 15 Prozent.", start=494.12, end=499.56, is_ad=True),
        Segment(id=158, text="Regular content continues...", start=499.6, end=505.0, is_ad=False),
    ]
    
    # Get ad blocks
    blocks = detector._get_ad_blocks(segments)
    
    # Verify block properties
    assert len(blocks) == 1, "Should identify one continuous ad block"
    block = blocks[0]
    assert len(block) == 10, "Ad block should contain 10 segments"
    assert block[0].id == 148, "Ad block should start with segment 148"
    assert block[-1].id == 157, "Ad block should end with segment 157"
    assert all(seg.is_ad for seg in block), "All segments in block should be marked as ads"

def test_detect_ads_integration(mock_openai):
    """Integration test for the complete ad detection process."""
    config = LLMConfig(model_name="test-model", api_key="test-key")
    detector = AdDetector(config)
    
    # Create a transcript with a known ad block
    segments = [
        Segment(id=147, text="Some content", start=460.0, end=464.0, is_ad=False),
        Segment(id=148, text="Nach einer kurzen Unterbrechung geht es gleich weiter.", start=464.2, end=467.84, is_ad=False),
        Segment(id=149, text="Bleiben Sie dran.", start=467.92, end=469.2, is_ad=False),
        Segment(id=150, text="Die Wasserstoffwirtschaft stoppt, doch die Zeit drängt.", start=469.72, end=472.88, is_ad=False),
        Segment(id=151, text="Wasserstoff kann ein Schlüssel für den Klimaschutz sein", start=472.96, end=475.72, is_ad=False),
        Segment(id=152, text="als Rohstoff für die Industrie, Speicher für Grünestrom oder Erdgasersatz.", start=475.8, end=480.24, is_ad=False),
        Segment(id=153, text="Aber wie kommen wir schneller voran?", start=480.32, end=482.24, is_ad=False),
        Segment(id=154, text="Antworten gibt der Handelsblatt Wasserstoffgipfel 2025.", start=482.32, end=486.56, is_ad=False),
        Segment(id=155, text="Am 21. und 22. Mai in Saarbrücken.", start=486.56, end=489.88, is_ad=False),
        Segment(id=156, text="Alle Infos und Tickets auf Handelsblatt-Wasserstoffgipfel.de.", start=489.96, end=494.04, is_ad=False),
        Segment(id=157, text="Mit dem Vorteilskot Wasserstoff 2025 sparen sie 15 Prozent.", start=494.12, end=499.56, is_ad=False),
        Segment(id=158, text="Regular content continues...", start=499.6, end=505.0, is_ad=False),
    ]
    transcript = Transcript(segments=segments)
    
    # Mock the OpenAI API call to directly mark segments as ads
    def mock_process_chunk(chunk):
        class MockResult:
            def __init__(self, segments, chunk_id):
                self.segments = segments
                self.chunk_id = chunk_id
                self.error = None
        
        for segment in chunk.segments:
            if 148 <= segment.id <= 157:
                segment.is_ad = True
        
        return MockResult(chunk.segments, chunk.chunk_id)
    
    # Patch the _process_chunk method
    with patch.object(AdDetector, '_process_chunk', side_effect=mock_process_chunk):
        result = detector.detect_ads(transcript)
    
    # Verify results
    ad_segments = [seg for seg in result.segments if seg.is_ad]
    assert len(ad_segments) == 10, "Should identify 10 segments as ads"
    assert all(148 <= seg.id <= 157 for seg in ad_segments), "Ad segments should be in range 148-157"

def test_handle_already_processed_file(mock_openai):
    """Test that the ad detector doesn't reprocess already processed files."""
    config = LLMConfig(model_name="test-model", api_key="test-key")
    mock_broker = MagicMock()
    detector = AdDetector(config, message_broker=mock_broker)
    detector.running = True
    
    # Add a file to the processed files set
    test_file_path = "/path/to/file.mp3"
    test_transcript_path = "/path/to/file.mp3.transcript.json"
    detector.processed_files.add(test_file_path)
    
    # Create a message requesting ad detection for the processed file
    message = Message(
        topic=Topics.AD_DETECTION_REQUEST,
        data={
            "file_path": test_file_path,
            "transcript_path": test_transcript_path
        },
        correlation_id="test-correlation-id"
    )
    
    # Handle the message
    detector._handle_ad_detection_request(message)
    
    # Verify that a "already processed" message was published
    mock_broker.publish.assert_called_once()
    published_message = mock_broker.publish.call_args[0][0]
    assert published_message.topic == Topics.AD_DETECTION_COMPLETE
    assert published_message.data["file_path"] == test_file_path
    assert published_message.data["already_processed"] is True
    assert published_message.correlation_id == "test-correlation-id"

def test_handle_in_process_file(mock_openai):
    """Test that the ad detector doesn't reprocess files that are currently being processed."""
    config = LLMConfig(model_name="test-model", api_key="test-key")
    mock_broker = MagicMock()
    detector = AdDetector(config, message_broker=mock_broker)
    detector.running = True
    
    # Add a file to the files in process set
    test_file_path = "/path/to/file.mp3"
    test_transcript_path = "/path/to/file.mp3.transcript.json"
    detector.files_in_process.add(test_file_path)
    
    # Create a message requesting ad detection for the in-process file
    message = Message(
        topic=Topics.AD_DETECTION_REQUEST,
        data={
            "file_path": test_file_path,
            "transcript_path": test_transcript_path
        },
        correlation_id="test-correlation-id"
    )
    
    # Handle the message
    detector._handle_ad_detection_request(message)
    
    # Verify that an "in progress" message was published
    mock_broker.publish.assert_called_once()
    published_message = mock_broker.publish.call_args[0][0]
    assert published_message.topic == Topics.AD_DETECTION_IN_PROGRESS
    assert published_message.data["file_path"] == test_file_path
    assert published_message.correlation_id == "test-correlation-id"

def test_file_lifecycle(mock_openai):
    """Test the complete lifecycle of file processing - from in-process to processed."""
    config = LLMConfig(model_name="test-model", api_key="test-key")
    mock_broker = MagicMock()
    detector = AdDetector(config, message_broker=mock_broker)
    detector.running = True
    
    # Set up test files
    test_file_path = "/path/to/file.mp3"
    test_transcript_path = "/path/to/file.mp3.transcript.json"
    
    # Mock file operations and detect_ads
    with patch("builtins.open", MagicMock()), \
         patch("json.load", return_value={"segments": []}), \
         patch("json.dump"), \
         patch.object(Transcript, "from_dict", return_value=Transcript(segments=[])), \
         patch.object(detector, "detect_ads", return_value=Transcript(segments=[])):
        
        # Create a message requesting ad detection
        message = Message(
            topic=Topics.AD_DETECTION_REQUEST,
            data={
                "file_path": test_file_path,
                "transcript_path": test_transcript_path
            },
            correlation_id="test-correlation-id"
        )
        
        # Check that file is not in any processing state initially
        assert test_file_path not in detector.files_in_process
        assert test_file_path not in detector.processed_files
        
        # Handle the message
        detector._handle_ad_detection_request(message)
        
        # Check that file is now in processed state and not in in-process state
        assert test_file_path not in detector.files_in_process
        assert test_file_path in detector.processed_files
        
        # Verify that a "complete" message was published
        mock_broker.publish.assert_called_once()
        published_message = mock_broker.publish.call_args[0][0]
        assert published_message.topic == Topics.AD_DETECTION_COMPLETE
        assert published_message.data["file_path"] == test_file_path
        assert published_message.correlation_id == "test-correlation-id"

def test_error_handling(mock_openai):
    """Test that errors during processing remove the file from in-process list."""
    config = LLMConfig(model_name="test-model", api_key="test-key")
    mock_broker = MagicMock()
    detector = AdDetector(config, message_broker=mock_broker)
    detector.running = True
    
    # Set up test files
    test_file_path = "/path/to/file.mp3"
    test_transcript_path = "/path/to/file.mp3.transcript.json"
    
    # Mock file operations to raise an exception
    with patch("builtins.open", side_effect=Exception("Test error")):
        
        # Create a message requesting ad detection
        message = Message(
            topic=Topics.AD_DETECTION_REQUEST,
            data={
                "file_path": test_file_path,
                "transcript_path": test_transcript_path
            },
            correlation_id="test-correlation-id"
        )
        
        # Handle the message (this should trigger an error)
        detector._handle_ad_detection_request(message)
        
        # Check that file is removed from in-process state and not in processed state
        assert test_file_path not in detector.files_in_process
        assert test_file_path not in detector.processed_files
        
        # Verify that a "failed" message was published
        mock_broker.publish.assert_called_once()
        published_message = mock_broker.publish.call_args[0][0]
        assert published_message.topic == Topics.AD_DETECTION_FAILED
        assert published_message.data["file_path"] == test_file_path
        assert "Test error" in published_message.data["error"]
        assert published_message.correlation_id == "test-correlation-id" 