"""System tests for the transcriber service."""

import os
import subprocess
import threading
import time
import json
import pytest
import requests
import tempfile
import shutil

# Test configuration
BASE_URL = "http://localhost:8080"
TEST_AUDIO_FILE = "test_audio.mp3"

@pytest.fixture
def create_test_audio():
    """Create a test audio file if it doesn't exist."""
    if not os.path.exists(TEST_AUDIO_FILE):
        # Create a 3-second silent MP3 file
        subprocess.run([
            "ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", 
            "-t", "3", "-q:a", "9", "-acodec", "libmp3lame", TEST_AUDIO_FILE
        ], check=True)
    
    yield TEST_AUDIO_FILE
    
    # Clean up
    if os.path.exists(TEST_AUDIO_FILE):
        os.unlink(TEST_AUDIO_FILE)

def test_transcriber_error_logs(create_test_audio):
    """Test that transcriber errors are properly logged in Docker."""
    # Skip if Docker is not available
    try:
        subprocess.run(["docker", "--version"], check=True, capture_output=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        pytest.skip("Docker not available")
    
    # Check if our containers are running
    result = subprocess.run(
        ["docker-compose", "ps", "-q", "podcleaner_transcriber_1"], 
        capture_output=True, text=True
    )
    if not result.stdout.strip():
        pytest.skip("Docker containers not running")
    
    # Upload our test audio file to minio
    # We'll do this by copying to the minio container's data directory
    subprocess.run([
        "docker", "cp", create_test_audio, 
        "podcleaner_minio_1:/data/podcleaner/podcasts/test_audio.mp3"
    ], check=True)

    # Send a process request to the web server
    test_url = "http://minio:9000/podcleaner/podcasts/test_audio.mp3"
    response = requests.get(f"{BASE_URL}/process", params={"url": test_url})
    assert response.status_code == 202, f"Expected status code 202, got {response.status_code}"
    
    # Wait for the transcriber to process the request and log the error
    time.sleep(5)
    
    # Check the transcriber logs for the error
    logs = subprocess.run(
        ["docker-compose", "logs", "podcleaner_transcriber_1"],
        capture_output=True, text=True
    ).stdout
    
    # Assert that the transcriber error is logged
    assert "module 'whisper' has no attribute 'load_model'" in logs, \
        "Transcriber error not found in logs"
    assert "transcription_failed" in logs, \
        "Transcription failed message not found in logs"

if __name__ == "__main__":
    pytest.main(["-xvs", __file__]) 