"""Configuration management for the PodCleaner package."""

import os
import json
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Any
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
class Config:
    """Main configuration class."""
    llm: LLMConfig
    audio: AudioConfig
    log_level: str = "INFO"
    message_broker: MessageBrokerConfig = None
    web_server: WebServerConfig = None

    def __post_init__(self):
        """Initialize default configs if not provided."""
        if self.message_broker is None:
            self.message_broker = MessageBrokerConfig()
        if self.web_server is None:
            self.web_server = WebServerConfig()

    def validate(self):
        """Validate the configuration."""
        self.llm.validate()

def load_config(config_path: str = "config.yaml") -> Config:
    """Load configuration from file and environment."""
    load_dotenv()
    
    # Default configuration
    config_dict = {
        "llm": {
            "model_name": "gpt-4o-mini",
            "chunk_size": 600,
            "max_attempts": 3,
            "temperature": 0.1
        },
        "audio": {
            "min_duration": 5.0,
            "max_gap": 20.0,
            "download_dir": "podcasts"
        },
        "message_broker": {
            "type": "in_memory",
            "mqtt": {
                "host": "localhost",
                "port": 1883
            }
        },
        "web_server": {
            "host": "localhost",
            "port": 8080,
            "use_https": False
        },
        "log_level": "INFO"
    }
    
    # Override with file configuration if exists
    if os.path.exists(config_path):
        with open(config_path) as f:
            file_config = yaml.safe_load(f)
            if file_config:
                # Merge nested dictionaries
                for key, value in file_config.items():
                    if isinstance(value, dict) and key in config_dict and isinstance(config_dict[key], dict):
                        config_dict[key].update(value)
                    else:
                        config_dict[key] = value
    
    # Create configuration objects
    message_broker_config = MessageBrokerConfig.from_dict(config_dict.get("message_broker", {}))
    web_server_config = WebServerConfig.from_dict(config_dict.get("web_server", {}))
    
    config = Config(
        llm=LLMConfig(**config_dict["llm"]),
        audio=AudioConfig(**config_dict["audio"]),
        log_level=config_dict["log_level"],
        message_broker=message_broker_config,
        web_server=web_server_config
    )
    
    # Validate configuration
    config.validate()
    
    return config 