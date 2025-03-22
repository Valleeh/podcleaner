#!/usr/bin/env python3
"""Universal service runner for PodCleaner microservices.

This script can start any of the PodCleaner microservices:
- Web server
- Transcriber
- Ad detector
- Audio processor
- Downloader

Usage:
  python -m podcleaner.run_service --service [service_name]
"""

import argparse
import os
import signal
import sys
import time
import threading

from .config import load_config
from .logging import configure_logging, get_logger
from .services.message_broker import MQTTMessageBroker
from .services.ad_detector import AdDetector
from .services.transcriber import Transcriber
from .services.audio_processor import AudioProcessor
from .services.downloader import PodcastDownloader
from .services.web_server import WebServer

logger = get_logger(__name__)

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run a PodCleaner microservice")
    
    # Service selection
    parser.add_argument(
        "--service", "-s",
        required=True,
        choices=["web", "transcriber", "ad-detector", "audio-processor", "downloader", "all"],
        help="Service to run"
    )
    
    # Message broker settings
    parser.add_argument("--mqtt-host", default=None, help="MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, default=None, help="MQTT broker port")
    parser.add_argument("--mqtt-username", default=None, help="MQTT broker username")
    parser.add_argument("--mqtt-password", default=None, help="MQTT broker password")
    
    # Web server settings
    parser.add_argument("--web-host", default=None, help="Web server host")
    parser.add_argument("--web-port", type=int, default=None, help="Web server port")
    
    # Service-specific settings
    parser.add_argument("--model-name", default=None, help="Transcription model name")
    
    # Configuration
    parser.add_argument("--config", default=None, help="Path to configuration file")
    parser.add_argument("--log-level", default=None, help="Log level (DEBUG, INFO, WARNING, ERROR)")
    
    return parser.parse_args()

def main():
    """Run the specified microservice."""
    # Parse command line arguments
    args = parse_args()
    
    # Load configuration
    config_path = args.config if args.config else None
    config = load_config(config_path)
    
    # Configure logging
    log_level = args.log_level or config.log_level
    configure_logging(log_level=log_level)
    
    # Override configuration with command line arguments for MQTT
    if args.mqtt_host:
        config.message_broker.mqtt.host = args.mqtt_host
    if args.mqtt_port:
        config.message_broker.mqtt.port = args.mqtt_port
    if args.mqtt_username:
        config.message_broker.mqtt.username = args.mqtt_username
    if args.mqtt_password:
        config.message_broker.mqtt.password = args.mqtt_password
    
    # Override web server settings if provided
    if args.web_host:
        config.web.host = args.web_host
    if args.web_port:
        config.web.port = args.web_port
    
    # Log the service being started
    service_name = args.service
    logger.info(f"starting_{service_name}_service")
    
    # Create a list to hold services for shutdown handling
    services = []
    
    # Create message broker
    logger.info("using_mqtt_broker", 
                host=config.message_broker.mqtt.host, 
                port=config.message_broker.mqtt.port)
    
    message_broker = MQTTMessageBroker(
        broker_host=config.message_broker.mqtt.host,
        broker_port=config.message_broker.mqtt.port,
        username=config.message_broker.mqtt.username,
        password=config.message_broker.mqtt.password,
        client_id=f"podcleaner-{service_name}"
    )
    
    # Initialize and start message broker
    message_broker.start()
    services.append(message_broker)
    logger.info("mqtt_broker_started",
               broker=f"{config.message_broker.mqtt.host}:{config.message_broker.mqtt.port}")
    
    # Initialize and start the requested service
    if service_name == "web" or service_name == "all":
        web_server = WebServer(
            host=config.web.host,
            port=config.web.port,
            message_broker=message_broker
        )
        web_server.start()
        services.append(web_server)
        logger.info("web_server_started", host=config.web.host, port=config.web.port)
    
    if service_name == "transcriber" or service_name == "all":
        model_name = args.model_name or "base"
        transcriber = Transcriber(
            message_broker=message_broker,
            model_name=model_name
        )
        transcriber.start()
        services.append(transcriber)
        logger.info("transcriber_started", model=model_name)
    
    if service_name == "ad-detector" or service_name == "all":
        ad_detector = AdDetector(
            config=config.llm,
            message_broker=message_broker
        )
        ad_detector.start()
        services.append(ad_detector)
        logger.info("ad_detector_started")
    
    if service_name == "audio-processor" or service_name == "all":
        audio_processor = AudioProcessor(
            config=config.audio,
            message_broker=message_broker
        )
        audio_processor.start()
        services.append(audio_processor)
        logger.info("audio_processor_started")
    
    if service_name == "downloader" or service_name == "all":
        downloader = PodcastDownloader(
            config=config.audio,
            message_broker=message_broker
        )
        downloader.start()
        services.append(downloader)
        logger.info("downloader_started")
    
    # Set up signal handlers
    def signal_handler(sig, frame):
        logger.info("shutdown_signal_received", signal=sig)
        # Stop services in reverse order
        for service in reversed(services):
            try:
                service.stop()
                logger.info(f"{service.__class__.__name__}_stopped")
            except Exception as e:
                logger.error(f"{service.__class__.__name__}_stop_error", error=str(e))
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Keep the main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
        signal_handler(None, None)

if __name__ == "__main__":
    main() 