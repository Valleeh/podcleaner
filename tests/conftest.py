"""Common fixtures for testing."""

import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture
def mock_mqtt_broker():
    """Mock the MQTT message broker."""
    mock_broker = MagicMock()
    mock_broker.subscribe = MagicMock()
    mock_broker.publish = MagicMock()
    mock_broker.start = MagicMock()
    mock_broker.stop = MagicMock()
    return mock_broker

@pytest.fixture
def mock_openai():
    """Mock the OpenAI client."""
    with patch('openai.Client') as mock:
        yield mock 