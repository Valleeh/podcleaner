#!/usr/bin/env python3
"""
Worker module that runs the podcleaner worker services (downloader, transcriber, ad_detector, audio_processor)
without the web server. This is useful for scaling the worker components separately from the web interface.
"""

import argparse
import os
import signal
import time
from typing import Optional

import structlog

from podcleaner.config import load_config, Config
from podcleaner.services.message_broker import MQTTMessageBroker
from podcleaner.services.downloader import Downloader
from podcleaner.services.transcriber import Transcriber
from podcleaner.services.ad_detector import AdDetector
from podcleaner.services.audio_processor import AudioProcessor

logger = structlog.get_logger()

def parse_args():
    parser = argparse.ArgumentParser(description="Run PodCleaner worker services")
    parser.add_argument("--mqtt-host", help="MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, help="MQTT broker port")
    parser.add_argument("--mqtt-username", help="MQTT broker username")
    parser.add_argument("--mqtt-password", help="MQTT broker password")
    parser.add_argument("--mqtt-client-id", help="MQTT client ID")
    parser.add_argument("--config", help="Path to config file")
    return parser.parse_args()

def main():
    args = parse_args()
    
    config_path = args.config if args.config else "config.yaml"
    config = load_config(config_path)
    
    # Override config with command line arguments if provided
    if args.mqtt_host:
        config.message_broker.mqtt.host = args.mqtt_host
    if args.mqtt_port:
        config.message_broker.mqtt.port = args.mqtt_port
    if args.mqtt_username:
        config.message_broker.mqtt.username = args.mqtt_username
    if args.mqtt_password:
        config.message_broker.mqtt.password = args.mqtt_password
    if args.mqtt_client_id:
        config.message_broker.mqtt.client_id = args.mqtt_client_id
    
    # Create message broker
    broker = MQTTMessageBroker(
        host=config.message_broker.mqtt.host,
        port=config.message_broker.mqtt.port,
        username=config.message_broker.mqtt.username,
        password=config.message_broker.mqtt.password,
        client_id=config.message_broker.mqtt.client_id,
    )
    
    # Start services
    downloader = Downloader(broker, config)
    transcriber = Transcriber(broker, config)
    ad_detector = AdDetector(broker, config)
    audio_processor = AudioProcessor(broker, config)
    
    logger.info("starting_worker_services", 
                mqtt_host=config.message_broker.mqtt.host, 
                mqtt_port=config.message_broker.mqtt.port)
    
    # Handle shutdown
    shutdown_requested = False
    
    def signal_handler(sig, frame):
        nonlocal shutdown_requested
        if not shutdown_requested:
            logger.info("shutdown_requested")
            shutdown_requested = True
            
            # Stop services in reverse order
            audio_processor.stop()
            ad_detector.stop()
            transcriber.stop()
            downloader.stop()
            broker.stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Keep the main thread alive
    while not shutdown_requested:
        time.sleep(1)

if __name__ == "__main__":
    main() 