"""Message broker service for decoupled communication between PodCleaner components."""

import json
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional
from ..logging import get_logger

logger = get_logger(__name__)

class Message:
    """A message that can be sent through the message broker."""
    
    def __init__(self, 
                 topic: str, 
                 data: Any, 
                 message_id: Optional[str] = None,
                 correlation_id: Optional[str] = None):
        """Initialize a message with topic and data."""
        self.topic = topic
        self.data = data
        self.message_id = message_id or str(uuid.uuid4())
        self.correlation_id = correlation_id
    
    def to_dict(self) -> dict:
        """Convert message to dictionary format."""
        return {
            "topic": self.topic,
            "data": self.data,
            "message_id": self.message_id,
            "correlation_id": self.correlation_id
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Message':
        """Create message from dictionary format."""
        return cls(
            topic=data["topic"],
            data=data["data"],
            message_id=data["message_id"],
            correlation_id=data["correlation_id"]
        )

class MessageBroker(ABC):
    """Abstract base class for message brokers."""
    
    @abstractmethod
    def publish(self, message: Message) -> None:
        """Publish a message to a topic."""
        pass
    
    @abstractmethod
    def subscribe(self, topic: str, callback: Callable[[Message], None]) -> None:
        """Subscribe to a topic with a callback function."""
        pass
    
    @abstractmethod
    def start(self) -> None:
        """Start the message broker."""
        pass
    
    @abstractmethod
    def stop(self) -> None:
        """Stop the message broker."""
        pass

class InMemoryMessageBroker(MessageBroker):
    """Simple in-memory message broker for development and testing."""
    
    def __init__(self):
        """Initialize the in-memory message broker."""
        self.subscribers: Dict[str, List[Callable[[Message], None]]] = {}
        self.running = False
    
    def publish(self, message: Message) -> None:
        """Publish a message to a topic."""
        if not self.running:
            logger.warning("message_broker_not_running", topic=message.topic)
            return
        
        logger.debug("publishing_message", topic=message.topic, message_id=message.message_id)
        if message.topic in self.subscribers:
            for callback in self.subscribers[message.topic]:
                try:
                    callback(message)
                except Exception as e:
                    logger.error("subscriber_callback_error", topic=message.topic, error=str(e))
    
    def subscribe(self, topic: str, callback: Callable[[Message], None]) -> None:
        """Subscribe to a topic with a callback function."""
        if topic not in self.subscribers:
            self.subscribers[topic] = []
        self.subscribers[topic].append(callback)
        logger.debug("subscribed_to_topic", topic=topic)
    
    def start(self) -> None:
        """Start the message broker."""
        self.running = True
        logger.info("message_broker_started", broker_type="in_memory")
    
    def stop(self) -> None:
        """Stop the message broker."""
        self.running = False
        logger.info("message_broker_stopped", broker_type="in_memory")

class MQTTMessageBroker(MessageBroker):
    """MQTT-based message broker for production use."""
    
    def __init__(self, 
                 broker_host: str = "localhost", 
                 broker_port: int = 1883,
                 client_id: Optional[str] = None,
                 username: Optional[str] = None,
                 password: Optional[str] = None):
        """Initialize the MQTT message broker."""
        try:
            import paho.mqtt.client as mqtt
            self.mqtt = mqtt
        except ImportError:
            logger.error("mqtt_import_error", message="paho-mqtt not installed. Run 'pip install paho-mqtt'")
            raise
        
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id or f"podcleaner-{uuid.uuid4()}"
        self.username = username
        self.password = password
        self.client = self.mqtt.Client(client_id=self.client_id)
        
        if username and password:
            self.client.username_pw_set(username, password)
        
        self.callbacks: Dict[str, List[Callable[[Message], None]]] = {}
        self.running = False
        
        # Set up callbacks
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback for when the client connects to the broker."""
        if rc == 0:
            logger.info("mqtt_connected", broker=f"{self.broker_host}:{self.broker_port}")
            
            # Resubscribe to all topics - use a copy of the keys to avoid RuntimeError
            for topic in list(self.callbacks.keys()):
                client.subscribe(topic)
                logger.debug("mqtt_resubscribed", topic=topic)
        else:
            logger.error("mqtt_connection_failed", rc=rc)
    
    def _on_message(self, client, userdata, msg):
        """Callback for when a message is received."""
        try:
            payload = json.loads(msg.payload.decode())
            message = Message.from_dict(payload)
            
            logger.debug("mqtt_message_received", topic=msg.topic, message_id=message.message_id)
            
            if msg.topic in self.callbacks:
                for callback in self.callbacks[msg.topic]:
                    try:
                        callback(message)
                    except Exception as e:
                        logger.error("mqtt_callback_error", 
                                    topic=msg.topic, 
                                    message_id=message.message_id,
                                    error=str(e))
        except json.JSONDecodeError:
            logger.error("mqtt_json_decode_error", topic=msg.topic)
        except Exception as e:
            logger.error("mqtt_message_processing_error", topic=msg.topic, error=str(e))
    
    def _on_disconnect(self, client, userdata, rc):
        """Callback for when the client disconnects from the broker."""
        if rc != 0:
            logger.warning("mqtt_unexpected_disconnect", rc=rc)
            # Try to reconnect if running
            if self.running:
                try:
                    self.client.reconnect()
                except:
                    logger.error("mqtt_reconnect_failed")
    
    def publish(self, message: Message) -> None:
        """Publish a message to a topic."""
        if not self.running:
            logger.warning("mqtt_broker_not_running", topic=message.topic)
            return
        
        try:
            payload = json.dumps(message.to_dict())
            self.client.publish(message.topic, payload)
            logger.debug("mqtt_message_published", topic=message.topic, message_id=message.message_id)
        except Exception as e:
            logger.error("mqtt_publish_error", topic=message.topic, error=str(e))
    
    def subscribe(self, topic: str, callback: Callable[[Message], None]) -> None:
        """Subscribe to a topic with a callback function."""
        if topic not in self.callbacks:
            self.callbacks[topic] = []
            
            # Only subscribe if running
            if self.running:
                self.client.subscribe(topic)
                
        self.callbacks[topic].append(callback)
        logger.debug("mqtt_subscribed_to_topic", topic=topic)
    
    def start(self) -> None:
        """Start the message broker."""
        try:
            self.client.connect(self.broker_host, self.broker_port)
            
            # Subscribe to all topics
            for topic in self.callbacks:
                self.client.subscribe(topic)
            
            self.client.loop_start()
            self.running = True
            logger.info("mqtt_broker_started", broker=f"{self.broker_host}:{self.broker_port}")
        except Exception as e:
            logger.error("mqtt_broker_start_failed", error=str(e))
            raise
    
    def stop(self) -> None:
        """Stop the message broker."""
        if self.running:
            self.client.loop_stop()
            self.client.disconnect()
            self.running = False
            logger.info("mqtt_broker_stopped")

# Topics used in the system
class Topics:
    """Standard topics used in the PodCleaner system."""
    DOWNLOAD_REQUEST = "podcast.download.request"
    DOWNLOAD_COMPLETE = "podcast.download.complete"
    DOWNLOAD_FAILED = "podcast.download.failed"
    
    TRANSCRIBE_REQUEST = "podcast.transcribe.request"
    TRANSCRIBE_COMPLETE = "podcast.transcribe.complete"
    TRANSCRIBE_FAILED = "podcast.transcribe.failed"
    
    AD_DETECTION_REQUEST = "podcast.ad_detection.request"
    AD_DETECTION_COMPLETE = "podcast.ad_detection.complete"
    AD_DETECTION_FAILED = "podcast.ad_detection.failed"
    
    AUDIO_PROCESSING_REQUEST = "podcast.audio_processing.request"
    AUDIO_PROCESSING_COMPLETE = "podcast.audio_processing.complete" 
    AUDIO_PROCESSING_FAILED = "podcast.audio_processing.failed"
    
    RSS_DOWNLOAD_REQUEST = "podcast.rss.download.request"
    RSS_DOWNLOAD_COMPLETE = "podcast.rss.download.complete"
    RSS_DOWNLOAD_FAILED = "podcast.rss.download.failed"
    
    # Server-related topics
    API_DOWNLOAD_REQUEST = "api.download.request"
    API_RSS_REQUEST = "api.rss.request"
    API_STATUS_UPDATE = "api.status.update" 