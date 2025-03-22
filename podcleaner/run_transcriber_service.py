"""Run the transcriber service for PodCleaner."""

import argparse
import os
import signal
import sys
import time

from .config import load_config
from .logging import configure_logging, get_logger
from .services.message_broker import MQTTMessageBroker
from .services.transcriber import Transcriber

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run the transcriber service for PodCleaner")
    
    # Message broker settings
    parser.add_argument("--mqtt-host", default=None, help="MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, default=None, help="MQTT broker port")
    parser.add_argument("--mqtt-username", default=None, help="MQTT broker username")
    parser.add_argument("--mqtt-password", default=None, help="MQTT broker password")
    
    # Transcriber settings
    parser.add_argument("--model", default=None, help="Whisper model to use (tiny, base, small, medium, large)")
    
    return parser.parse_args()

def main():
    """Run the transcriber service."""
    # Parse command line arguments
    args = parse_args()
    
    # Load configuration
    config = load_config()
    
    # Configure logging
    configure_logging(log_level=config.log_level)
    logger = get_logger(__name__)
    
    # Override configuration with command line arguments
    if args.mqtt_host:
        config.message_broker.mqtt.host = args.mqtt_host
    if args.mqtt_port:
        config.message_broker.mqtt.port = args.mqtt_port
    if args.mqtt_username:
        config.message_broker.mqtt.username = args.mqtt_username
    if args.mqtt_password:
        config.message_broker.mqtt.password = args.mqtt_password
    
    # Create message broker
    logger.info("using_mqtt_broker", 
                host=config.message_broker.mqtt.host, 
                port=config.message_broker.mqtt.port)
    
    message_broker = MQTTMessageBroker(
        broker_host=config.message_broker.mqtt.host,
        broker_port=config.message_broker.mqtt.port,
        username=config.message_broker.mqtt.username,
        password=config.message_broker.mqtt.password,
        client_id="podcleaner-transcriber"
    )
    
    # Create and start services
    logger.info("starting_transcriber_service")
    
    # Initialize and start message broker
    message_broker.start()
    logger.info("mqtt_broker_started",
               broker=f"{config.message_broker.mqtt.host}:{config.message_broker.mqtt.port}")
    
    # Initialize and start transcriber
    whisper_model = args.model or "base"
    transcriber = Transcriber(
        model_name=whisper_model,
        message_broker=message_broker
    )
    transcriber.start()
    logger.info("transcriber_started", model=whisper_model)
    
    # Set up signal handlers
    def signal_handler(sig, frame):
        logger.info("shutdown_signal_received", signal=sig)
        transcriber.stop()
        message_broker.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Keep the main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
        transcriber.stop()
        message_broker.stop()

if __name__ == "__main__":
    main() 