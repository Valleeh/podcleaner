"""Tests for the MQTT message broker."""

import pytest
from unittest.mock import MagicMock, patch

from podcleaner.services.message_broker import MQTTMessageBroker, Message, Topics

@pytest.fixture
def mock_mqtt_client():
    with patch('paho.mqtt.client.Client') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance
        yield mock_instance

def test_mqtt_broker_init():
    """Test MQTT broker initialization with correct parameters."""
    broker = MQTTMessageBroker(
        broker_host="test-host",
        broker_port=1883,
        username="test-user",
        password="test-password",
        client_id="test-client"
    )
    
    assert broker.broker_host == "test-host"
    assert broker.broker_port == 1883
    assert broker.username == "test-user"
    assert broker.password == "test-password"
    assert broker.client_id == "test-client"

def test_mqtt_broker_start(mock_mqtt_client):
    """Test MQTT broker start method connects to the broker."""
    broker = MQTTMessageBroker(
        broker_host="test-host",
        broker_port=1883,
        client_id="test-client"
    )
    
    broker.start()
    
    mock_mqtt_client.username_pw_set.assert_not_called()
    mock_mqtt_client.connect.assert_called_once_with("test-host", 1883, 60)
    mock_mqtt_client.loop_start.assert_called_once()

def test_mqtt_broker_start_with_auth(mock_mqtt_client):
    """Test MQTT broker start method with authentication."""
    broker = MQTTMessageBroker(
        broker_host="test-host",
        broker_port=1883,
        username="test-user",
        password="test-password",
        client_id="test-client"
    )
    
    broker.start()
    
    mock_mqtt_client.username_pw_set.assert_called_once_with("test-user", "test-password")
    mock_mqtt_client.connect.assert_called_once_with("test-host", 1883, 60)
    mock_mqtt_client.loop_start.assert_called_once()

def test_mqtt_broker_stop(mock_mqtt_client):
    """Test MQTT broker stop method disconnects from the broker."""
    broker = MQTTMessageBroker(
        broker_host="test-host",
        broker_port=1883,
        client_id="test-client"
    )
    
    broker.stop()
    
    mock_mqtt_client.loop_stop.assert_called_once()
    mock_mqtt_client.disconnect.assert_called_once()

def test_mqtt_broker_publish(mock_mqtt_client):
    """Test MQTT broker publish method."""
    broker = MQTTMessageBroker(
        broker_host="test-host",
        broker_port=1883,
        client_id="test-client"
    )
    
    message = Message(
        topic=Topics.TRANSCRIBE_REQUEST,
        data={"file_path": "test.mp3"},
        correlation_id="test-id"
    )
    
    broker.publish(message)
    
    mock_mqtt_client.publish.assert_called_once() 