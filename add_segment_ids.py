import json
import logging
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def add_segment_ids(input_file, output_file=None):
    """
    Add unique IDs to each segment in the transcript file.
    """
    logger.info(f"Reading file: {input_file}")
    try:
        with open(input_file, 'r') as f:
            data = json.load(f)
        
        if 'transcription' not in data:
            logger.error("JSON file does not contain 'transcription' key")
            return False
            
        transcription = data['transcription']
        logger.info(f"Processing {len(transcription)} segments")
        
        # Add IDs to each segment
        for i, segment in enumerate(transcription):
            segment['segment_id'] = i
            
        # Generate output filename with timestamp if not provided
        if output_file is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f'transcript_with_ids_{timestamp}.json'
        
        # Save the modified data
        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2)
            
        logger.info(f"Added IDs to {len(transcription)} segments")
        logger.info(f"Saved to: {output_file}")
        return True
        
    except FileNotFoundError:
        logger.error(f"File not found: {input_file}")
        return False
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON in file: {input_file}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return False

def main():
    input_file = 'output.json'
    output_file = 'output_with_ids.json'
    
    logger.info("Starting segment ID addition process")
    success = add_segment_ids(input_file, output_file)
    
    if success:
        logger.info("Successfully added segment IDs")
    else:
        logger.error("Failed to add segment IDs")

if __name__ == "__main__":
    main() 