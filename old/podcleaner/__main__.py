"""Command-line interface for the PodCleaner package."""

import sys
import argparse
from typing import List
import os
import signal
import time
import importlib

from .config import load_config
from .logging import configure_logging, get_logger
from .services.message_broker import MQTTMessageBroker
from .services.ad_detector import AdDetector
from .services.transcriber import Transcriber
from .services.audio_processor import AudioProcessor
from .services.downloader import PodcastDownloader
from .services.web_server import WebServer

logger = get_logger(__name__)

def parse_args(args: List[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="PodCleaner - Download and clean advertisements from podcasts."
    )
    
    # Create subparsers for different modes
    subparsers = parser.add_subparsers(dest="mode", help="Operation mode")
    
    # Process mode parser
    process_parser = subparsers.add_parser("process", help="Process a podcast URL")
    process_parser.add_argument(
        "url",
        help="URL of the podcast to process"
    )
    
    process_parser.add_argument(
        "-o", "--output",
        help="Path to save the processed audio file"
    )
    
    process_parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to configuration file"
    )
    
    process_parser.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="Keep intermediate files after processing"
    )
    
    # Service mode parser
    service_parser = subparsers.add_parser("service", help="Run a microservice")
    service_parser.add_argument(
        "--service", "-s",
        required=True,
        choices=["web", "transcriber", "ad-detector", "audio-processor", "downloader", "all"],
        help="Service to run"
    )
    
    service_parser.add_argument(
        "--mqtt-host",
        default=None,
        help="MQTT broker host"
    )
    
    service_parser.add_argument(
        "--mqtt-port",
        type=int,
        default=None,
        help="MQTT broker port"
    )
    
    service_parser.add_argument(
        "--web-host",
        default=None,
        help="Web server host (for web service)"
    )
    
    service_parser.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="Web server port (for web service)"
    )
    
    service_parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to configuration file"
    )
    
    # Debug flag for all modes
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    
    # If no arguments are provided, show help and exit
    if not args:
        parser.print_help()
        sys.exit(1)
    
    parsed_args = parser.parse_args(args)
    
    # If no mode is specified, default to help
    if not parsed_args.mode:
        parser.print_help()
        sys.exit(1)
    
    return parsed_args

def main(args: List[str] = None) -> int:
    """Main entry point for the command-line interface."""
    if args is None:
        args = sys.argv[1:]
    
    try:
        # Parse arguments
        parsed_args = parse_args(args)
        
        # Load configuration
        config = load_config(parsed_args.config if hasattr(parsed_args, 'config') else None)
        
        # Configure logging
        if parsed_args.debug:
            config.log_level = "DEBUG"
        configure_logging(config.log_level)
        
        # Handle different modes
        if parsed_args.mode == "process":
            # Create a message broker for processing a single URL
            broker = MQTTMessageBroker(
                broker_host=config.message_broker.mqtt.host,
                broker_port=config.message_broker.mqtt.port,
                username=config.message_broker.mqtt.username,
                password=config.message_broker.mqtt.password,
                client_id="podcleaner-cli"
            )
            
            # Start the broker
            broker.start()
            
            # Create the downloader service to process the URL
            downloader = PodcastDownloader(config=config.audio, message_broker=broker)
            downloader.start()
            
            # Process the URL and wait for completion
            from .services.message_broker import Message, Topics
            
            # Create a flag to track completion
            processing_complete = False
            output_file = None
            
            # Define callback handlers
            def handle_audio_complete(message):
                nonlocal processing_complete, output_file
                logger.info("processing_complete", 
                           input=message.data.get("input_path", "unknown"),
                           output=message.data.get("output_path", "unknown"))
                output_file = message.data.get("output_path")
                processing_complete = True
            
            def handle_failure(message):
                nonlocal processing_complete
                logger.error("processing_failed",
                            error=message.data.get("error", "unknown error"))
                processing_complete = True
            
            # Subscribe to completion and failure topics
            broker.subscribe(Topics.AUDIO_PROCESSING_COMPLETE, handle_audio_complete)
            broker.subscribe(Topics.DOWNLOAD_FAILED, handle_failure)
            broker.subscribe(Topics.TRANSCRIBE_FAILED, handle_failure)
            broker.subscribe(Topics.AD_DETECTION_FAILED, handle_failure)
            broker.subscribe(Topics.AUDIO_PROCESSING_FAILED, handle_failure)
            
            # Submit the URL for processing
            broker.publish(Message(
                topic=Topics.DOWNLOAD_REQUEST,
                data={"url": parsed_args.url}
            ))
            
            logger.info("processing_started", url=parsed_args.url)
            
            # Wait for processing to complete
            try:
                while not processing_complete:
                    time.sleep(1)
            finally:
                # Clean up
                downloader.stop()
                broker.stop()
            
            return 0 if output_file else 1
        
        elif parsed_args.mode == "service":
            # This mode uses the service runner functionality
            run_service = importlib.import_module('.run_service', package='podcleaner')
            run_service_main = getattr(run_service, 'main')
            
            # Create mock args for run_service
            service_args = ["--service", parsed_args.service]
            
            # Add MQTT options if provided
            if parsed_args.mqtt_host:
                service_args.extend(["--mqtt-host", parsed_args.mqtt_host])
            if parsed_args.mqtt_port:
                service_args.extend(["--mqtt-port", str(parsed_args.mqtt_port)])
            
            # Add web options if provided
            if parsed_args.web_host:
                service_args.extend(["--web-host", parsed_args.web_host])
            if parsed_args.web_port:
                service_args.extend(["--web-port", str(parsed_args.web_port)])
            
            # Add config if provided
            if hasattr(parsed_args, 'config') and parsed_args.config:
                service_args.extend(["--config", parsed_args.config])
            
            # Add log level if debug is enabled
            if parsed_args.debug:
                service_args.extend(["--log-level", "DEBUG"])
            
            # Run the service
            sys.argv = [sys.argv[0]] + service_args
            return run_service_main()
        
    except KeyboardInterrupt:
        logger.warning("processing_interrupted")
        return 130
        
    except Exception as e:
        logger.error("processing_failed", error=str(e))
        return 1

if __name__ == "__main__":
    sys.exit(main()) 