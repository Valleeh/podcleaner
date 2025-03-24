"""Configuration management for the PodCleaner package."""

import os
import json
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Any, Literal
import yaml
from dotenv import load_dotenv

def load_api_key(secrets_file="secrets.json"):
    """Load API key from secrets file."""
    try:
        with open(secrets_file) as f:
            secrets = json.load(f)
        return secrets["OPENAI_API_KEY"]
    except Exception as e:
        raise ValueError(f"Failed to load API key from {secrets_file}: {str(e)}")

@dataclass
class LLMConfig:
    """Configuration for the LLM service."""
    model_name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    chunk_size: int = 600
    max_attempts: int = 3
    temperature: float = 0.1

    def validate(self):
        """Validate the configuration."""
        if not self.model_name:
            raise ValueError("Model name is required")
        # Load API key from secrets if not provided
        if not self.api_key:
            self.api_key = load_api_key()

@dataclass
class AudioConfig:
    """Configuration for audio processing."""
    min_duration: float = 5.0
    max_gap: float = 20.0
    download_dir: str = "podcasts"

@dataclass
class MQTTConfig:
    """Configuration for MQTT broker."""
    host: str = "localhost"
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    client_id: Optional[str] = None

@dataclass
class MessageBrokerConfig:
    """Configuration for message broker."""
    type: str = "in_memory"  # Options: "mqtt", "in_memory"
    mqtt: MQTTConfig = None
    
    def __post_init__(self):
        """Initialize default MQTT config if not provided."""
        if self.mqtt is None:
            self.mqtt = MQTTConfig()
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MessageBrokerConfig':
        """Create MessageBrokerConfig from dictionary."""
        mqtt_data = data.get("mqtt", {})
        mqtt_config = MQTTConfig(**mqtt_data) if mqtt_data else None
        
        return cls(
            type=data.get("type", "in_memory"),
            mqtt=mqtt_config
        )

@dataclass
class WebServerConfig:
    """Configuration for web server."""
    host: str = "localhost"
    port: int = 8080
    use_https: bool = False
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WebServerConfig':
        """Create WebServerConfig from dictionary."""
        return cls(
            host=data.get("host", "localhost"),
            port=data.get("port", 8080),
            use_https=data.get("use_https", False)
        )

@dataclass
class ObjectStorageConfig:
    """Configuration for object storage."""
    provider: str = "local"  # Options: "local", "s3", "gcs", "azure", "minio"
    
    # Common settings
    bucket_name: str = "podcleaner"
    region: Optional[str] = None
    
    # Endpoint for non-AWS providers like MinIO or localstack
    endpoint_url: Optional[str] = None
    
    # Authentication
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    
    # Local storage settings (used if provider is "local")
    local_storage_path: str = "podcasts"
    
    # Connection settings
    connect_timeout: int = 5
    read_timeout: int = 30
    max_retries: int = 3
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ObjectStorageConfig':
        """Create ObjectStorageConfig from dictionary."""
        if not data:
            return cls()
            
        return cls(
            provider=data.get("provider", "local"),
            bucket_name=data.get("bucket_name", "podcleaner"),
            region=data.get("region"),
            endpoint_url=data.get("endpoint_url"),
            access_key=data.get("access_key"),
            secret_key=data.get("secret_key"),
            local_storage_path=data.get("local_storage_path", "podcasts"),
            connect_timeout=data.get("connect_timeout", 5),
            read_timeout=data.get("read_timeout", 30),
            max_retries=data.get("max_retries", 3)
        )

@dataclass
class Config:
    """Main configuration class."""
    llm: LLMConfig
    audio: AudioConfig
    log_level: str = "INFO"
    message_broker: MessageBrokerConfig = None
    web_server: WebServerConfig = None
    object_storage: ObjectStorageConfig = None

    def __post_init__(self):
        """Initialize default configs if not provided."""
        if self.message_broker is None:
            self.message_broker = MessageBrokerConfig()
        if self.web_server is None:
            self.web_server = WebServerConfig()
        if self.object_storage is None:
            self.object_storage = ObjectStorageConfig()

    def validate(self):
        """Validate the configuration."""
        self.llm.validate()

def load_config(path=None):
    """Load the configuration file and substitute environment variables."""
    if path is None:
        path = "config.yaml"
    with open(path, 'r') as f:
        content = f.read()
    # Substitute environment variables in the content
    content = os.path.expandvars(content)
    config_data = yaml.safe_load(content)
    # Load environment variables
    load_dotenv()
    
    # Load LLM config
    llm_config_data = config_data.get("llm", {})
    llm_config = LLMConfig(
        model_name=llm_config_data.get("model_name", "gpt-3.5-turbo"),
        api_key=llm_config_data.get("api_key") or os.environ.get("OPENAI_API_KEY"),
        base_url=llm_config_data.get("base_url") or os.environ.get("OPENAI_API_BASE"),
        chunk_size=llm_config_data.get("chunk_size", 600),
        max_attempts=llm_config_data.get("max_attempts", 3),
        temperature=llm_config_data.get("temperature", 0.1)
    )
    
    # Load audio config
    audio_config_data = config_data.get("audio", {})
    audio_config = AudioConfig(
        min_duration=audio_config_data.get("min_duration", 5.0),
        max_gap=audio_config_data.get("max_gap", 20.0),
        download_dir=audio_config_data.get("download_dir", "podcasts")
    )
    
    # Load message broker config
    message_broker_config = MessageBrokerConfig.from_dict(
        config_data.get("message_broker", {})
    )
    
    # Load web server config
    web_server_config = WebServerConfig.from_dict(
        config_data.get("web_server", {})
    )
    
    # Load object storage config
    object_storage_config = ObjectStorageConfig.from_dict(
        config_data.get("object_storage", {})
    )
    
    # Create the config
    config = Config(
        llm=llm_config,
        audio=audio_config,
        log_level=config_data.get("log_level", "INFO"),
        message_broker=message_broker_config,
        web_server=web_server_config,
        object_storage=object_storage_config
    )
    
    return config 