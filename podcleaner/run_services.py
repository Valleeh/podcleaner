"""Script to run PodCleaner services."""

import argparse
import signal
import sys
import time
from typing import List
import logging

from .config import load_config, Config
from .logging import configure_logging, get_logger
from .services import (
    PodcastDownloader,
    Transcriber,
    AdDetector,
    AudioProcessor,
    InMemoryMessageBroker,
    MQTTMessageBroker,
    WebServer
)

logger = get_logger(__name__)

def parse_args(args: List[str] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run PodCleaner services"
    )
    
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to configuration file"
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    
    parser.add_argument(
        "--mqtt-host",
        default="localhost",
        help="MQTT broker host (default: localhost)"
    )
    
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=1883,
        help="MQTT broker port (default: 1883)"
    )
    
    parser.add_argument(
        "--mqtt-username",
        help="MQTT broker username"
    )
    
    parser.add_argument(
        "--mqtt-password",
        help="MQTT broker password"
    )
    
    parser.add_argument(
        "--web-host",
        default="localhost",
        help="Web server host (default: localhost)"
    )
    
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Web server port (default: 8080)"
    )
    
    parser.add_argument(
        "--services",
        nargs="+",
        choices=["all", "web", "downloader", "transcriber", "ad_detector", "audio_processor"],
        default=["all"],
        help="Services to run (default: all)"
    )
    
    parser.add_argument(
        "--in-memory-broker",
        action="store_true",
        help="Use in-memory message broker instead of MQTT"
    )
    
    return parser.parse_args(args)

def run_services():
    """Run PodCleaner services."""
    args = parse_args()
    
    # Load configuration
    config = load_config(args.config)
    
    # Configure logging
    if args.debug:
        config.log_level = "DEBUG"
    configure_logging(config.log_level)
    
    # Create message broker
    if args.in_memory_broker:
        logger.info("using_in_memory_broker")
        message_broker = InMemoryMessageBroker()
    else:
        logger.info("using_mqtt_broker", host=args.mqtt_host, port=args.mqtt_port)
        message_broker = MQTTMessageBroker(
            broker_host=args.mqtt_host,
            broker_port=args.mqtt_port,
            username=args.mqtt_username,
            password=args.mqtt_password
        )
    
    services = []
    services_to_run = args.services
    run_all = "all" in services_to_run
    
    # Start message broker
    message_broker.start()
    services.append(message_broker)
    
    # Create and start services
    if run_all or "web" in services_to_run:
        web_server = WebServer(
            host=args.web_host,
            port=args.web_port,
            message_broker=message_broker
        )
        web_server.start()
        services.append(web_server)
        logger.info("web_server_started", host=args.web_host, port=args.web_port)
    
    if run_all or "downloader" in services_to_run:
        downloader = PodcastDownloader(config.audio, message_broker)
        downloader.start()
        services.append(downloader)
        logger.info("downloader_started")
    
    if run_all or "transcriber" in services_to_run:
        transcriber = Transcriber(message_broker)
        transcriber.start()
        services.append(transcriber)
        logger.info("transcriber_started")
    
    if run_all or "ad_detector" in services_to_run:
        ad_detector = AdDetector(config.llm, message_broker)
        ad_detector.start()
        services.append(ad_detector)
        logger.info("ad_detector_started")
    
    if run_all or "audio_processor" in services_to_run:
        audio_processor = AudioProcessor(config.audio, message_broker)
        audio_processor.start()
        services.append(audio_processor)
        logger.info("audio_processor_started")
    
    logger.info("all_services_started")
    
    # Handle shutdown
    def signal_handler(sig, frame):
        logger.info("shutdown_requested")
        for service in reversed(services):
            try:
                service.stop()
            except Exception as e:
                logger.error("service_stop_error", service=service.__class__.__name__, error=str(e))
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Keep the main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(None, None)

if __name__ == "__main__":
    run_services() 