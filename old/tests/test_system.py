"""System tests for the podcleaner application.

These tests validate the complete system functionality by interacting
with the web API and verifying the end-to-end processing pipeline.
"""

import os
import time
import pytest
import requests
import json
import tempfile
from urllib.parse import urljoin
import shutil
import subprocess
import signal
import threading
from concurrent.futures import ThreadPoolExecutor

# Default test configuration
DEFAULT_CONFIG = {
    "base_url": "http://localhost:8080",
    "test_podcast_url": "https://traffic.libsyn.com/secure/tripod/TriPod_Episode_1.mp3",
    "timeout": 300,  # Maximum time to wait for processing (seconds)
    "poll_interval": 10,  # How often to check status (seconds)
    "rss_feed_url": "https://feeds.simplecast.com/54nAGcIl",
    "concurrent_requests": 2  # Number of concurrent requests for load testing
}

def load_test_config():
    """Load test configuration from file or use defaults."""
    config_path = os.environ.get("PODCLEANER_TEST_CONFIG", "tests/system_test_config.json")
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                # Merge with defaults for any missing keys
                return {**DEFAULT_CONFIG, **config}
        except Exception as e:
            print(f"Error loading test config: {e}")
    
    return DEFAULT_CONFIG

# Load test configuration
TEST_CONFIG = load_test_config()

@pytest.fixture(scope="module")
def system_ready():
    """Check if the system is up and running."""
    print(f"\nChecking if the system is ready at {TEST_CONFIG['base_url']}...")
    
    try:
        # Test the health endpoint first
        health_url = urljoin(TEST_CONFIG["base_url"], "/health")
        max_retries = 5
        retry_delay = 5
        
        for attempt in range(max_retries):
            try:
                response = requests.get(health_url, timeout=10)
                if response.status_code == 200:
                    print(f"System is ready! Health check successful on attempt {attempt + 1}")
                    print(f"Health response: {response.json()}")
                    return True
                else:
                    print(f"Health check returned status code {response.status_code} on attempt {attempt + 1}")
            except requests.RequestException as e:
                print(f"Error connecting to system on attempt {attempt + 1}: {str(e)}")
            
            if attempt < max_retries - 1:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
        
        print("WARNING: System health check failed. Tests may fail if the system is not running.")
        # Don't fail the test setup, let individual tests decide what to do
        return False
    
    except Exception as e:
        print(f"Unexpected error during system check: {str(e)}")
        # Don't fail the test setup, let individual tests decide what to do
        return False

def test_system_health(system_ready):
    """Test the system health endpoint."""
    if not system_ready:
        pytest.skip("System is not ready, skipping test")
    
    base_url = TEST_CONFIG["base_url"]
    
    # Check the health endpoint
    health_url = urljoin(base_url, "/health")
    try:
        response = requests.get(health_url)
        
        # Verify health response
        assert response.status_code == 200
        health_data = response.json()
        
        # Verify health response contains service statuses
        assert "services" in health_data
        
        # Print service statuses for debugging
        print("\nService statuses:")
        for service in health_data["services"]:
            print(f"  {service['name']}: {service['status']}")
        
        # At least some services should be up
        assert any(service["status"] == "up" for service in health_data["services"]), "No services are up"
        
    except requests.RequestException as e:
        pytest.fail(f"Failed to connect to health endpoint: {str(e)}")

def test_process_podcast(system_ready):
    """Test processing a podcast through the web API."""
    if not system_ready:
        pytest.skip("System is not ready, skipping test")
    
    base_url = TEST_CONFIG["base_url"]
    test_url = TEST_CONFIG["test_podcast_url"]
    
    print(f"\nSubmitting podcast for processing: {test_url}")
    
    # Submit a podcast for processing
    process_url = urljoin(base_url, "/process")
    try:
        response = requests.get(
            process_url,
            params={"url": test_url}
        )
        
        # Check initial response
        assert response.status_code == 200
        result = response.json()
        print(f"Initial response: {result}")
        assert "request_id" in result
        request_id = result["request_id"]
        
        # Poll the status until processing is complete or timeout
        print(f"Polling for status of request ID: {request_id}")
        start_time = time.time()
        status_url = urljoin(base_url, "/status")
        processing_complete = False
        
        while time.time() - start_time < TEST_CONFIG["timeout"]:
            try:
                status_response = requests.get(
                    status_url,
                    params={"request_id": request_id}
                )
                
                assert status_response.status_code == 200
                status_result = status_response.json()
                print(f"Status update: {status_result}")
                
                if status_result.get("status") == "completed":
                    processing_complete = True
                    break
                elif status_result.get("status") == "failed":
                    assert False, f"Processing failed: {status_result.get('error')}"
                
                # Wait before polling again
                print(f"Waiting {TEST_CONFIG['poll_interval']} seconds before next poll...")
                time.sleep(TEST_CONFIG["poll_interval"])
            except requests.RequestException as e:
                print(f"Error checking status: {str(e)}")
                time.sleep(TEST_CONFIG["poll_interval"])
        
        # Assert that processing completed
        assert processing_complete, "Processing timed out"
        
        # Get the clean podcast URL
        clean_url = status_result.get("clean_url")
        assert clean_url, "Clean URL not found in response"
        print(f"Clean podcast URL: {clean_url}")
        
        # Download the clean podcast to verify it exists
        try:
            clean_podcast_response = requests.get(clean_url)
            assert clean_podcast_response.status_code == 200
            assert len(clean_podcast_response.content) > 0, "Clean podcast file is empty"
            print(f"Successfully downloaded cleaned podcast ({len(clean_podcast_response.content)} bytes)")
        except requests.RequestException as e:
            pytest.fail(f"Failed to download cleaned podcast: {str(e)}")
            
    except requests.RequestException as e:
        pytest.fail(f"Failed to submit podcast for processing: {str(e)}")

# Additional tests can be added as needed
def test_invalid_url_handling(system_ready):
    """Test handling of invalid URLs."""
    if not system_ready:
        pytest.skip("System is not ready, skipping test")
    
    base_url = TEST_CONFIG["base_url"]
    invalid_url = "https://example.com/nonexistent-podcast.mp3"
    
    print(f"\nSubmitting invalid URL for processing: {invalid_url}")
    
    # Submit an invalid URL for processing
    process_url = urljoin(base_url, "/process")
    try:
        response = requests.get(
            process_url,
            params={"url": invalid_url}
        )
        
        # Should still get a 200 response with a request_id
        assert response.status_code == 200
        result = response.json()
        print(f"Initial response: {result}")
        assert "request_id" in result
        request_id = result["request_id"]
        
        # Poll the status until error is reported or timeout
        print(f"Polling for status of request ID: {request_id}")
        start_time = time.time()
        status_url = urljoin(base_url, "/status")
        error_reported = False
        
        while time.time() - start_time < TEST_CONFIG["timeout"]:
            try:
                status_response = requests.get(
                    status_url,
                    params={"request_id": request_id}
                )
                
                assert status_response.status_code == 200
                status_result = status_response.json()
                print(f"Status update: {status_result}")
                
                if status_result.get("status") == "failed":
                    error_reported = True
                    assert "error" in status_result, "Error details not provided"
                    print(f"Error correctly reported: {status_result.get('error')}")
                    break
                elif status_result.get("status") == "completed":
                    assert False, "Processing should have failed but completed instead"
                
                # Wait before polling again
                print(f"Waiting {TEST_CONFIG['poll_interval']} seconds before next poll...")
                time.sleep(TEST_CONFIG["poll_interval"])
            except requests.RequestException as e:
                print(f"Error checking status: {str(e)}")
                time.sleep(TEST_CONFIG["poll_interval"])
        
        # Assert that an error was reported
        assert error_reported, "Error was not reported for invalid URL"
            
    except requests.RequestException as e:
        pytest.fail(f"Failed to submit invalid URL for processing: {str(e)}")

if __name__ == "__main__":
    # Allow running a single test directly
    system_ready_result = system_ready()
    if system_ready_result:
        print("Running test_process_podcast...")
        test_process_podcast(system_ready_result)
    else:
        print("System is not ready, cannot run tests directly")
    