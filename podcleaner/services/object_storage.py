"""Object storage service for podcast files and data."""

import os
import shutil
import io
import abc
from typing import List, Optional, BinaryIO, Dict, Any, Union, Tuple
from ..config import ObjectStorageConfig
from ..logging import get_logger

logger = get_logger(__name__)

class ObjectStorageError(Exception):
    """Base exception for object storage errors."""
    pass

class ObjectStorageAdapter(abc.ABC):
    """Abstract base class for object storage adapters."""
    
    @abc.abstractmethod
    def upload(self, source: Union[str, bytes, BinaryIO], key: str) -> str:
        """
        Upload an object to storage.
        
        Args:
            source: File path, bytes data, or file-like object to upload
            key: Storage key (path) for the uploaded object
            
        Returns:
            str: The full URL or path to the stored object
            
        Raises:
            ObjectStorageError: If upload fails
        """
        pass
    
    @abc.abstractmethod
    def download(self, key: str, destination: Optional[str] = None) -> Union[bytes, str]:
        """
        Download an object from storage.
        
        Args:
            key: Storage key (path) of the object to download
            destination: Optional file path to save the downloaded object
                         If None, returns the object data as bytes
                         
        Returns:
            Union[bytes, str]: The downloaded data as bytes if destination is None,
                              or the path where the file was saved
                              
        Raises:
            ObjectStorageError: If download fails
        """
        pass
    
    @abc.abstractmethod
    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        """
        List objects in storage with the given prefix.
        
        Args:
            prefix: Storage key prefix to filter objects
            
        Returns:
            List[Dict[str, Any]]: List of object metadata dictionaries
                               Each dict contains at least 'key' and 'size'
                               
        Raises:
            ObjectStorageError: If listing fails
        """
        pass
    
    @abc.abstractmethod
    def delete(self, key: str) -> bool:
        """
        Delete an object from storage.
        
        Args:
            key: Storage key (path) of the object to delete
            
        Returns:
            bool: True if deleted successfully
            
        Raises:
            ObjectStorageError: If deletion fails
        """
        pass
    
    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """
        Check if an object exists in storage.
        
        Args:
            key: Storage key (path) to check
            
        Returns:
            bool: True if the object exists
        """
        pass
    
    @abc.abstractmethod
    def get_public_url(self, key: str, expires_in: int = 3600) -> str:
        """
        Get a public (pre-signed) URL for the object.
        
        Args:
            key: Storage key (path) of the object
            expires_in: Expiration time in seconds
            
        Returns:
            str: Public URL for the object
        """
        pass

class LocalStorageAdapter(ObjectStorageAdapter):
    """Adapter for local file system storage."""
    
    def __init__(self, config: ObjectStorageConfig):
        """Initialize the local storage adapter."""
        self.storage_path = config.local_storage_path
        os.makedirs(self.storage_path, exist_ok=True)
        logger.info("local_storage_initialized", path=self.storage_path)
    
    def _get_file_path(self, key: str) -> str:
        """Get the full file path for a key."""
        # Ensure the key doesn't have leading / to avoid absolute paths
        key = key.lstrip('/')
        
        # Create the directory structure if needed
        path = os.path.join(self.storage_path, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        return path
    
    def upload(self, source: Union[str, bytes, BinaryIO], key: str) -> str:
        """Upload a file to local storage."""
        try:
            destination = self._get_file_path(key)
            
            if isinstance(source, str) and os.path.exists(source):
                # Source is a file path
                shutil.copy2(source, destination)
            elif isinstance(source, bytes):
                # Source is bytes data
                with open(destination, 'wb') as f:
                    f.write(source)
            elif hasattr(source, 'read'):
                # Source is a file-like object
                with open(destination, 'wb') as f:
                    shutil.copyfileobj(source, f)
            else:
                raise ValueError(f"Unsupported source type: {type(source)}")
            
            logger.info("file_uploaded", key=key, path=destination)
            return destination
        except Exception as e:
            logger.error("upload_failed", key=key, error=str(e))
            raise ObjectStorageError(f"Failed to upload {key}: {str(e)}")
    
    def download(self, key: str, destination: Optional[str] = None) -> Union[bytes, str]:
        """Download a file from local storage."""
        try:
            source_path = self._get_file_path(key)
            
            if not os.path.exists(source_path):
                raise FileNotFoundError(f"File {key} not found at {source_path}")
            
            if destination:
                # Copy to destination
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy2(source_path, destination)
                logger.info("file_downloaded", key=key, path=destination)
                return destination
            else:
                # Return file content as bytes
                with open(source_path, 'rb') as f:
                    data = f.read()
                logger.info("file_downloaded_to_memory", key=key, size=len(data))
                return data
        except Exception as e:
            logger.error("download_failed", key=key, error=str(e))
            raise ObjectStorageError(f"Failed to download {key}: {str(e)}")
    
    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        """List files in local storage with the given prefix."""
        try:
            prefix_path = os.path.join(self.storage_path, prefix.lstrip('/'))
            result = []
            
            # If prefix is empty, start from storage path
            if not prefix:
                base_path = self.storage_path
            else:
                base_path = prefix_path
                
                # Check if the specified prefix exists
                if not os.path.exists(base_path):
                    return []
            
            # Walk through the directory structure
            for root, _, files in os.walk(base_path):
                for file in files:
                    full_path = os.path.join(root, file)
                    # Get the key by making the path relative to storage_path
                    rel_path = os.path.relpath(full_path, self.storage_path)
                    # Use forward slashes for consistency across platforms
                    key = rel_path.replace(os.path.sep, '/')
                    
                    result.append({
                        'key': key,
                        'size': os.path.getsize(full_path),
                        'last_modified': os.path.getmtime(full_path)
                    })
            
            logger.info("files_listed", prefix=prefix, count=len(result))
            return result
        except Exception as e:
            logger.error("list_objects_failed", prefix=prefix, error=str(e))
            raise ObjectStorageError(f"Failed to list objects with prefix {prefix}: {str(e)}")
    
    def delete(self, key: str) -> bool:
        """Delete a file from local storage."""
        try:
            file_path = self._get_file_path(key)
            
            if not os.path.exists(file_path):
                logger.warning("file_not_found", key=key, path=file_path)
                return False
            
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
            
            logger.info("file_deleted", key=key)
            return True
        except Exception as e:
            logger.error("delete_failed", key=key, error=str(e))
            raise ObjectStorageError(f"Failed to delete {key}: {str(e)}")
    
    def exists(self, key: str) -> bool:
        """Check if a file exists in local storage."""
        file_path = self._get_file_path(key)
        exists = os.path.exists(file_path)
        logger.debug("file_exists_check", key=key, exists=exists)
        return exists
    
    def get_public_url(self, key: str, expires_in: int = 3600) -> str:
        """Get a file:// URL for the object (not truly public)."""
        file_path = self._get_file_path(key)
        url = f"file://{os.path.abspath(file_path)}"
        logger.debug("public_url_generated", key=key, url=url)
        return url

try:
    import boto3
    from botocore.exceptions import ClientError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

class S3StorageAdapter(ObjectStorageAdapter):
    """Adapter for AWS S3 or S3-compatible storage."""
    
    def __init__(self, config: ObjectStorageConfig):
        """Initialize the S3 storage adapter."""
        if not BOTO3_AVAILABLE:
            raise ImportError("boto3 is required for S3 storage. Install it with 'pip install boto3'.")
        
        self.bucket_name = config.bucket_name
        session_kwargs = {}
        
        if config.region:
            session_kwargs['region_name'] = config.region
        
        if config.access_key and config.secret_key:
            session_kwargs['aws_access_key_id'] = config.access_key
            session_kwargs['aws_secret_access_key'] = config.secret_key
        
        client_kwargs = {}
        if config.endpoint_url:
            client_kwargs['endpoint_url'] = config.endpoint_url
        
        # Create session and client
        self.session = boto3.session.Session(**session_kwargs)
        self.s3_client = self.session.client('s3', **client_kwargs)
        
        # Ensure the bucket exists
        try:
            self.s3_client.head_bucket(Bucket=self.bucket_name)
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code')
            if error_code == '404':
                # Bucket doesn't exist, create it
                try:
                    logger.info("creating_bucket", bucket=self.bucket_name)
                    if config.region and config.region != 'us-east-1':
                        self.s3_client.create_bucket(
                            Bucket=self.bucket_name,
                            CreateBucketConfiguration={'LocationConstraint': config.region}
                        )
                    else:
                        self.s3_client.create_bucket(Bucket=self.bucket_name)
                except Exception as creation_error:
                    logger.error("bucket_creation_failed", bucket=self.bucket_name, error=str(creation_error))
                    raise ObjectStorageError(f"Failed to create bucket {self.bucket_name}: {str(creation_error)}")
            else:
                logger.error("bucket_check_failed", bucket=self.bucket_name, error=str(e))
                raise ObjectStorageError(f"Failed to access bucket {self.bucket_name}: {str(e)}")
        
        logger.info("s3_storage_initialized", bucket=self.bucket_name)
    
    def upload(self, source: Union[str, bytes, BinaryIO], key: str) -> str:
        """Upload an object to S3."""
        try:
            # Ensure the key doesn't have leading /
            key = key.lstrip('/')
            
            if isinstance(source, str) and os.path.exists(source):
                # Source is a file path
                self.s3_client.upload_file(source, self.bucket_name, key)
            elif isinstance(source, bytes):
                # Source is bytes data
                self.s3_client.put_object(Bucket=self.bucket_name, Key=key, Body=source)
            elif hasattr(source, 'read'):
                # Source is a file-like object
                self.s3_client.upload_fileobj(source, self.bucket_name, key)
            else:
                raise ValueError(f"Unsupported source type: {type(source)}")
            
            logger.info("object_uploaded", key=key, bucket=self.bucket_name)
            return f"s3://{self.bucket_name}/{key}"
        except Exception as e:
            logger.error("upload_failed", key=key, bucket=self.bucket_name, error=str(e))
            raise ObjectStorageError(f"Failed to upload {key}: {str(e)}")
    
    def download(self, key: str, destination: Optional[str] = None) -> Union[bytes, str]:
        """Download an object from S3."""
        try:
            # Ensure the key doesn't have leading /
            key = key.lstrip('/')
            
            if destination:
                # Download to file
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                self.s3_client.download_file(self.bucket_name, key, destination)
                logger.info("object_downloaded", key=key, bucket=self.bucket_name, path=destination)
                return destination
            else:
                # Download to memory
                response = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
                data = response['Body'].read()
                logger.info("object_downloaded_to_memory", key=key, bucket=self.bucket_name, size=len(data))
                return data
        except Exception as e:
            logger.error("download_failed", key=key, bucket=self.bucket_name, error=str(e))
            raise ObjectStorageError(f"Failed to download {key}: {str(e)}")
    
    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        """List objects in S3 with the given prefix."""
        try:
            # Ensure the prefix doesn't have leading /
            prefix = prefix.lstrip('/')
            
            paginator = self.s3_client.get_paginator('list_objects_v2')
            result = []
            
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                if 'Contents' in page:
                    for obj in page['Contents']:
                        result.append({
                            'key': obj['Key'],
                            'size': obj['Size'],
                            'last_modified': obj['LastModified'].timestamp()
                        })
            
            logger.info("objects_listed", prefix=prefix, bucket=self.bucket_name, count=len(result))
            return result
        except Exception as e:
            logger.error("list_objects_failed", prefix=prefix, bucket=self.bucket_name, error=str(e))
            raise ObjectStorageError(f"Failed to list objects with prefix {prefix}: {str(e)}")
    
    def delete(self, key: str) -> bool:
        """Delete an object from S3."""
        try:
            # Ensure the key doesn't have leading /
            key = key.lstrip('/')
            
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=key)
            logger.info("object_deleted", key=key, bucket=self.bucket_name)
            return True
        except Exception as e:
            logger.error("delete_failed", key=key, bucket=self.bucket_name, error=str(e))
            raise ObjectStorageError(f"Failed to delete {key}: {str(e)}")
    
    def exists(self, key: str) -> bool:
        """Check if an object exists in S3."""
        try:
            # Ensure the key doesn't have leading /
            key = key.lstrip('/')
            
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            logger.debug("object_exists", key=key, bucket=self.bucket_name)
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                logger.debug("object_not_found", key=key, bucket=self.bucket_name)
                return False
            else:
                logger.error("exists_check_failed", key=key, bucket=self.bucket_name, error=str(e))
                raise ObjectStorageError(f"Failed to check if {key} exists: {str(e)}")
    
    def get_public_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a pre-signed URL for the object."""
        try:
            # Ensure the key doesn't have leading /
            key = key.lstrip('/')
            
            url = self.s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket_name, 'Key': key},
                ExpiresIn=expires_in
            )
            logger.debug("presigned_url_generated", key=key, bucket=self.bucket_name, expires_in=expires_in)
            return url
        except Exception as e:
            logger.error("presigned_url_generation_failed", key=key, bucket=self.bucket_name, error=str(e))
            raise ObjectStorageError(f"Failed to generate presigned URL for {key}: {str(e)}")

class ObjectStorage:
    """Object storage service supporting multiple backends."""
    
    def __init__(self, config: ObjectStorageConfig):
        """Initialize the object storage service."""
        self.config = config
        
        # Initialize the appropriate storage adapter
        if config.provider == "local":
            self.adapter = LocalStorageAdapter(config)
        elif config.provider in ["s3", "minio"]:
            self.adapter = S3StorageAdapter(config)
        else:
            raise ValueError(f"Unsupported storage provider: {config.provider}")
        
        logger.info("object_storage_initialized", provider=config.provider)
    
    def upload(self, source: Union[str, bytes, BinaryIO], key: str) -> str:
        """Upload an object to storage."""
        return self.adapter.upload(source, key)
    
    def download(self, key: str, destination: Optional[str] = None) -> Union[bytes, str]:
        """Download an object from storage."""
        return self.adapter.download(key, destination)
    
    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        """List objects in storage with the given prefix."""
        return self.adapter.list_objects(prefix)
    
    def delete(self, key: str) -> bool:
        """Delete an object from storage."""
        return self.adapter.delete(key)
    
    def exists(self, key: str) -> bool:
        """Check if an object exists in storage."""
        return self.adapter.exists(key)
    
    def get_public_url(self, key: str, expires_in: int = 3600) -> str:
        """Get a public (pre-signed) URL for the object."""
        return self.adapter.get_public_url(key, expires_in)
    
    def generate_key(self, original_path: str) -> str:
        """
        Generate a storage key from an original path.
        
        Args:
            original_path: Original file path or identifier
            
        Returns:
            str: Storage key
        """
        # If original_path is a local file path, extract just the filename
        basename = os.path.basename(original_path)
        
        # Use the podcast's folder structure: podcasts/original/filename
        key = f"podcasts/original/{basename}"
        return key 