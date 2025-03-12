"""Configuration management for the PodCleaner package."""

import os
import json
import requests
from dataclasses import dataclass
from typing import Optional
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
class Config:
    """Main configuration class."""
    llm: LLMConfig
    audio: AudioConfig
    log_level: str = "INFO"

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
        "log_level": "INFO"
    }
    
    # Override with file configuration if exists
    if os.path.exists(config_path):
        with open(config_path) as f:
            file_config = yaml.safe_load(f)
            if file_config:
                config_dict.update(file_config)
    
    config = Config(
        llm=LLMConfig(**config_dict["llm"]),
        audio=AudioConfig(**config_dict["audio"]),
        log_level=config_dict["log_level"]
    )
    
    # Validate configuration
    config.validate()
    
    return config 