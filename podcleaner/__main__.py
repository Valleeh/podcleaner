"""Command-line interface for the PodCleaner package."""

import sys
import argparse
from typing import List
from .config import load_config
from .logging import configure_logging, get_logger
from .orchestrator import PodcastCleaner

logger = get_logger(__name__)

def parse_args(args: List[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Download and clean advertisements from podcasts."
    )
    
    parser.add_argument(
        "url",
        help="URL of the podcast to process"
    )
    
    parser.add_argument(
        "-o", "--output",
        help="Path to save the processed audio file"
    )
    
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to configuration file"
    )
    
    parser.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="Keep intermediate files after processing"
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    
    return parser.parse_args(args)

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
        
        # Process podcast
        cleaner = PodcastCleaner(config)
        output_file = cleaner.process_podcast(
            url=parsed_args.url,
            output_file=parsed_args.output,
            keep_intermediate=parsed_args.keep_intermediate
        )
        
        logger.info("processing_complete", output=output_file)
        return 0
        
    except KeyboardInterrupt:
        logger.warning("processing_interrupted")
        return 130
        
    except Exception as e:
        logger.error("processing_failed", error=str(e))
        return 1

if __name__ == "__main__":
    sys.exit(main()) 