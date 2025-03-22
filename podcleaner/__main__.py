"""Command-line interface for the PodCleaner package."""

import sys
import argparse
from typing import List
from .config import load_config
from .logging import configure_logging, get_logger
from .orchestrator import PodcastCleaner
from .services import (
    InMemoryMessageBroker,
    WebServer,
    PodcastDownloader
)

logger = get_logger(__name__)

def parse_args(args: List[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Download and clean advertisements from podcasts."
    )
    
    # Create subparsers for different modes
    subparsers = parser.add_subparsers(dest="mode", help="Operation mode")
    
    # Process parser (default mode)
    process_parser = subparsers.add_parser("process", help="Process a podcast")
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
    
    # Server mode parser
    server_parser = subparsers.add_parser("server", help="Run the web server")
    server_parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to configuration file"
    )
    
    server_parser.add_argument(
        "--host",
        default="localhost",
        help="Host to bind the server to"
    )
    
    server_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to bind the server to"
    )
    
    # Debug flag for both modes
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
    
    # If no mode is specified, default to process
    if not parsed_args.mode:
        parsed_args.mode = "process"
    
    return parsed_args

def main(args: List[str] = None) -> int:
    """Main entry point for the command-line interface."""
    if args is None:
        args = sys.argv[1:]
    
    try:
        # Parse arguments
        parsed_args = parse_args(args)
        
        # Load configuration
        config = load_config(parsed_args.config)
        
        # Configure logging
        if parsed_args.debug:
            config.log_level = "DEBUG"
        configure_logging(config.log_level)
        
        # Handle different modes
        if parsed_args.mode == "process":
            # Use the monolithic orchestrator
            cleaner = PodcastCleaner(config)
            output_file = cleaner.process_podcast(
                url=parsed_args.url,
                output_file=parsed_args.output,
                keep_intermediate=parsed_args.keep_intermediate
            )
            
            logger.info("processing_complete", output=output_file)
            return 0
        
        elif parsed_args.mode == "server":
            # Run the web server with in-memory broker
            broker = InMemoryMessageBroker()
            broker.start()
            
            # Create and start the web server
            web_server = WebServer(
                host=parsed_args.host,
                port=parsed_args.port,
                message_broker=broker
            )
            web_server.start()
            
            # Create and start the downloader service
            downloader = PodcastDownloader(config.audio, broker)
            downloader.start()
            
            logger.info("server_started", host=parsed_args.host, port=parsed_args.port)
            
            # Keep the main thread alive
            try:
                while True:
                    import time
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("server_stopping")
                web_server.stop()
                downloader.stop()
                broker.stop()
                return 0
        
    except KeyboardInterrupt:
        logger.warning("processing_interrupted")
        return 130
        
    except Exception as e:
        logger.error("processing_failed", error=str(e))
        return 1

if __name__ == "__main__":
    sys.exit(main()) 