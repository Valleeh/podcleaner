"""Run the web service for PodCleaner."""

import argparse
import os
import signal
import sys
import time

from .config import load_config
from .logging import configure_logging, get_logger
from .services.message_broker import MQTTMessageBroker
from .services.web_server import WebServer
from .services.downloader import PodcastDownloader

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run the web service for PodCleaner")
    
    # Message broker settings
    parser.add_argument("--mqtt-host", default=None, help="MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, default=None, help="MQTT broker port")
    parser.add_argument("--mqtt-username", default=None, help="MQTT broker username")
    parser.add_argument("--mqtt-password", default=None, help="MQTT broker password")
    
    # Web server settings
    parser.add_argument("--web-host", default=None, help="Web server host")
    parser.add_argument("--web-port", type=int, default=None, help="Web server port")
    
    return parser.parse_args()

def main():
    """Run the web service."""
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
    if args.web_host:
        config.web_server.host = args.web_host
    if args.web_port:
        config.web_server.port = args.web_port
    
    # Create message broker
    logger.info("using_mqtt_broker", 
                host=config.message_broker.mqtt.host, 
                port=config.message_broker.mqtt.port)
    
    message_broker = MQTTMessageBroker(
        broker_host=config.message_broker.mqtt.host,
        broker_port=config.message_broker.mqtt.port,
        username=config.message_broker.mqtt.username,
        password=config.message_broker.mqtt.password,
        client_id="podcleaner-web"
    )
    
    # Create and start services
    logger.info("starting_web_service")
    
    # Initialize and start message broker
    message_broker.start()
    logger.info("mqtt_broker_started",
               broker=f"{config.message_broker.mqtt.host}:{config.message_broker.mqtt.port}")
    
    # Initialize and start web server
    web_server = WebServer(
        host=config.web_server.host,
        port=config.web_server.port,
        message_broker=message_broker,
        use_https=config.web_server.use_https
    )
    web_server.start()
    logger.info("web_server_started",
               host=config.web_server.host,
               port=config.web_server.port)
    
    # Initialize downloader service
    downloader = PodcastDownloader(
        config=config.audio,
        message_broker=message_broker
    )
    downloader.start()
    logger.info("downloader_started")
    
    # Set up signal handlers
    def signal_handler(sig, frame):
        logger.info("shutdown_signal_received", signal=sig)
        web_server.stop()
        downloader.stop()
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
        web_server.stop()
        downloader.stop()
        message_broker.stop()

if __name__ == "__main__":
    main() 