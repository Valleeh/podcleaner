"""Tests for the object storage service."""

import os
import pytest
import tempfile
import shutil
from unittest.mock import patch, MagicMock
from podcleaner.config import ObjectStorageConfig
from podcleaner.services.object_storage import (
    ObjectStorage, LocalStorageAdapter, 
    S3StorageAdapter, ObjectStorageError
)

@pytest.fixture
def temp_dir():
    """Create a temporary directory for testing."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)

@pytest.fixture
def local_storage_config(temp_dir):
    """Create a local storage configuration."""
    return ObjectStorageConfig(
        provider="local",
        local_storage_path=temp_dir
    )

@pytest.fixture
def s3_storage_config():
    """Create an S3 storage configuration."""
    return ObjectStorageConfig(
        provider="s3",
        bucket_name="test-bucket",
        region="us-east-1",
        access_key="test-key",
        secret_key="test-secret"
    )

@pytest.fixture
def local_storage(local_storage_config):
    """Create a local storage adapter."""
    return LocalStorageAdapter(local_storage_config)

def test_local_storage_upload_file(local_storage, temp_dir):
    """Test uploading a file to local storage."""
    # Create a test file
    test_file = os.path.join(temp_dir, "test.txt")
    with open(test_file, "w") as f:
        f.write("test content")
    
    # Upload the file
    key = "test/test.txt"
    path = local_storage.upload(test_file, key)
    
    # Check that the file was uploaded
    assert os.path.exists(os.path.join(temp_dir, key))
    assert os.path.isfile(path)
    
    # Verify the content
    with open(path, "r") as f:
        content = f.read()
        assert content == "test content"

def test_local_storage_upload_bytes(local_storage, temp_dir):
    """Test uploading bytes to local storage."""
    # Upload bytes
    key = "test/bytes.txt"
    data = b"test bytes"
    path = local_storage.upload(data, key)
    
    # Check that the file was created
    assert os.path.exists(os.path.join(temp_dir, key))
    assert os.path.isfile(path)
    
    # Verify the content
    with open(path, "rb") as f:
        content = f.read()
        assert content == data

def test_local_storage_download_to_file(local_storage, temp_dir):
    """Test downloading a file from local storage."""
    # Create a test file in storage
    key = "test/download.txt"
    storage_path = os.path.join(temp_dir, key)
    os.makedirs(os.path.dirname(storage_path), exist_ok=True)
    with open(storage_path, "w") as f:
        f.write("download test")
    
    # Download to a file
    output_path = os.path.join(temp_dir, "output.txt")
    result = local_storage.download(key, output_path)
    
    # Check the result
    assert result == output_path
    assert os.path.exists(output_path)
    
    # Verify the content
    with open(output_path, "r") as f:
        content = f.read()
        assert content == "download test"

def test_local_storage_download_to_memory(local_storage, temp_dir):
    """Test downloading a file to memory."""
    # Create a test file in storage
    key = "test/memory.txt"
    storage_path = os.path.join(temp_dir, key)
    os.makedirs(os.path.dirname(storage_path), exist_ok=True)
    with open(storage_path, "w") as f:
        f.write("memory test")
    
    # Download to memory
    result = local_storage.download(key)
    
    # Check the result
    assert isinstance(result, bytes)
    assert result == b"memory test"

def test_local_storage_list_objects(local_storage, temp_dir):
    """Test listing objects in local storage."""
    # Create test files
    files = {
        "test/file1.txt": "content 1",
        "test/file2.txt": "content 2",
        "other/file3.txt": "content 3"
    }
    
    for key, content in files.items():
        path = os.path.join(temp_dir, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    
    # List all objects
    all_objects = local_storage.list_objects()
    assert len(all_objects) == 3
    
    # List objects with prefix
    test_objects = local_storage.list_objects("test")
    assert len(test_objects) == 2
    
    # Check that the keys are correct
    keys = [obj["key"] for obj in test_objects]
    assert "test/file1.txt" in keys
    assert "test/file2.txt" in keys

def test_local_storage_delete(local_storage, temp_dir):
    """Test deleting an object from local storage."""
    # Create a test file
    key = "test/delete.txt"
    path = os.path.join(temp_dir, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("delete me")
    
    # Delete the file
    result = local_storage.delete(key)
    
    # Check the result
    assert result is True
    assert not os.path.exists(path)

def test_local_storage_exists(local_storage, temp_dir):
    """Test checking if an object exists in local storage."""
    # Create a test file
    key = "test/exists.txt"
    path = os.path.join(temp_dir, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("I exist")
    
    # Check if the file exists
    assert local_storage.exists(key) is True
    assert local_storage.exists("non-existent.txt") is False

def test_local_storage_get_public_url(local_storage, temp_dir):
    """Test getting a public URL for a local file."""
    # Create a test file
    key = "test/url.txt"
    path = os.path.join(temp_dir, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("url test")
    
    # Get the URL
    url = local_storage.get_public_url(key)
    
    # Check the URL
    assert url.startswith("file://")
    assert os.path.abspath(path) in url

@pytest.mark.skipif(not hasattr(pytest, "boto3_mock"), reason="boto3 mocking not available")
def test_s3_storage_init():
    """Test initializing S3 storage."""
    with patch("podcleaner.services.object_storage.boto3") as mock_boto3:
        # Mock the session and client
        mock_session = MagicMock()
        mock_client = MagicMock()
        mock_boto3.session.Session.return_value = mock_session
        mock_session.client.return_value = mock_client
        
        # Mock the head_bucket call to succeed
        mock_client.head_bucket.return_value = {}
        
        # Create the storage adapter
        config = ObjectStorageConfig(
            provider="s3",
            bucket_name="test-bucket",
            region="us-east-1",
            access_key="test-key",
            secret_key="test-secret"
        )
        
        storage = S3StorageAdapter(config)
        
        # Check that the client was created
        mock_boto3.session.Session.assert_called_once_with(
            region_name="us-east-1",
            aws_access_key_id="test-key",
            aws_secret_access_key="test-secret"
        )
        mock_session.client.assert_called_once_with("s3")
        
        # Check that the bucket was verified
        mock_client.head_bucket.assert_called_once_with(Bucket="test-bucket")

def test_object_storage_factory(local_storage_config, s3_storage_config):
    """Test the ObjectStorage factory class."""
    # Test with local storage
    with patch("podcleaner.services.object_storage.LocalStorageAdapter") as mock_local:
        mock_local.return_value = MagicMock()
        storage = ObjectStorage(local_storage_config)
        mock_local.assert_called_once()
        
        # Test calling methods
        storage.upload("test", "key")
        storage.adapter.upload.assert_called_once_with("test", "key")
    
    # Test with S3 storage
    with patch("podcleaner.services.object_storage.S3StorageAdapter") as mock_s3:
        mock_s3.return_value = MagicMock()
        storage = ObjectStorage(s3_storage_config)
        mock_s3.assert_called_once()
        
        # Test calling methods
        storage.download("key")
        storage.adapter.download.assert_called_once_with("key", None)
    
    # Test with unsupported provider
    with pytest.raises(ValueError):
        storage = ObjectStorage(ObjectStorageConfig(provider="unknown"))

def test_object_storage_generate_key():
    """Test generating a storage key."""
    storage = ObjectStorage(ObjectStorageConfig())
    
    # Test with a filename
    key = storage.generate_key("test.mp3")
    assert key == "podcasts/original/test.mp3"
    
    # Test with a path
    key = storage.generate_key("/path/to/test.mp3")
    assert key == "podcasts/original/test.mp3" 