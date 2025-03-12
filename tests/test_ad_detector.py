import pytest
from unittest.mock import MagicMock, patch
from podcleaner.config import LLMConfig
from podcleaner.models import Segment, Transcript
from podcleaner.services.ad_detector import AdDetector

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
    
    # Mock the OpenAI response
    mock_response = MagicMock()
    mock_response.choices = [
        MagicMock(
            message=MagicMock(
                content='{"segments": [' + 
                        ','.join([
                            f'{{"id": {i}, "ad": {str(148 <= i <= 157).lower()}}}'
                            for i in range(147, 159)
                        ]) + 
                        ']}'
            )
        )
    ]
    mock_openai.return_value.chat.completions.create.return_value = mock_response
    
    # Process the transcript
    result = detector.detect_ads(transcript)
    
    # Verify results
    ad_segments = [seg for seg in result.segments if seg.is_ad]
    assert len(ad_segments) == 10, "Should identify 10 segments as ads"
    assert all(148 <= seg.id <= 157 for seg in ad_segments), "Ad segments should be in range 148-157" 