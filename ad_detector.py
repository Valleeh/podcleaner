from openai import OpenAI
import json
import logging
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client pointing to the LM Studio server
client = OpenAI(
    base_url="http://192.168.178.185:1234/v1",
    api_key="lm-studio"
)

# Define the JSON Schema for the response
ad_schema = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "ad": {"type": "boolean"}
                },
                "required": ["id", "ad"]
            }
        }
    },
    "required": ["segments"]
}
def create_schema(ids: list):
    """
    Create a JSON schema for the segments array using the provided list of IDs.
    
    Each segment must be an object with:
      - an "id" field that is fixed to the given ID (using "const")
      - an "ad" field that is a boolean.
    
    The "segments" array must contain exactly one segment per provided ID.
    """
    items_schemas = []
    
    for id_val in ids:
        # Try converting the id to an integer; if it fails, leave it as a string.
        try:
            const_value = int(id_val)
        except ValueError:
            const_value = id_val
        
        item_schema = {
            "type": "object",
            "properties": {
                "id": {"const": const_value},
                "ad": {"type": "boolean"}
            },
            "required": ["id", "ad"],
            "additionalProperties": False
        }
        items_schemas.append(item_schema)
    
    schema = {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": items_schemas,        # Tuple validation: each item has its own schema.
                "minItems": len(ids),          # Exactly as many items as provided IDs.
                "maxItems": len(ids)
            }
        },
        "required": ["segments"],
        "additionalProperties": False
    }
    
    return schema

# Example usage:
# ids = ["0", "1", "2", "3"]
# ad_schema = create_schema(ids)

# import json
# print(json.dumps(ad_schema, indent=2))

def process_json_file(filepath, chunk_size=200):
    """Process the transcript file and detect advertisements"""
    # logger.info(f"Opening file: {filepath}")
    try:
        with open(filepath, 'r') as file:
            data = json.load(file)
        
        if 'transcription' not in data:
            logger.error("JSON file does not contain 'transcription' key")
            return None
            
        transcription = data['transcription']
        # logger.info(f"Loaded {len(transcription)} transcript segments")
        
        results = []
        chunk_id = 0
        
        for i in range(0, len(transcription), chunk_size):
            chunk = transcription[i:i + chunk_size]
            # Create list of tuples with (id, text) for each segment
            chunk_data = [(segment['segment_id'], segment['text']) for segment in chunk]
            # logger.debug(f"Processing chunk {chunk_id} with {len(chunk_data)} segments")
            
            # Format the segments for the prompt
            segments_text = "\n".join([f"ID: {seg_id} Text: {text}" for seg_id, text in chunk_data])
            ids = [seg_id for seg_id , _ in chunk_data] 
            # Create the messages for this chunk
            messages = [
                {"role": "system", "content": """You are an advertisement detection system. Your task is to identify advertisements in transcript segments.
IMPORTANT: You must return a classification for EVERY segment provided in the exact schema format."""},
                {
                    "role": "user",
                    "content": f"""Analyze these transcript segments and identify which ones are advertisements.

Segments to analyze:
{segments_text}

Return JSON matching this exact structure:
{{
    "segments": [
        {{"id": <segment_id>, "ad": true/false}},
        {{"id": <segment_id>, "ad": true/false}},
        ...
    ]
}}

IMPORTANT:
- Include ALL segment IDs from the input
- Maintain the original IDs exactly
- Only return the JSON object with no additional text"""
                }
            ]

            # try:
            # Request classification from the model
            logger.info(create_schema(ids))
            response = client.chat.completions.create(
                model="deepseek-r1-distill-qwen-14b",
                messages=messages,
                temperature=0.1,
                # response_format={"type": "json_schema", "schema": ad_schema}
            )
            
            # Parse the response
            response_text = response.choices[0].message.content
            logger.info(f"Repsonse: {response_text} ")
            chunk_results = json.loads(response_text)
            
            # Validate structure
            if not isinstance(chunk_results.get('segments'), list):
                logger.error(f"Invalid response structure in chunk {chunk_id}")
                continue
                
            # Map results to original IDs
            valid_results = []
            for res, (seg_id, _) in zip(chunk_results['segments'], chunk_data):
                if res['id'] == seg_id:
                    valid_results.append(res)
                else:
                    logger.warning(f"ID mismatch in chunk {chunk_id}: Expected {seg_id}, got {res['id']}")
                    valid_results.append({"id": seg_id, "ad": res.get('ad', False)})
            
            results.extend(valid_results)
            # logger.info(f"Processed chunk {chunk_id} with {len(valid_results)} valid segments")
                
            # except Exception as e:
            #     logger.error(f"Error processing chunk {chunk_id}: {str(e)}")
            #     # Fallback: Create default non-ad entries
            #     results.extend([{"id": seg_id, "ad": False} for seg_id, _ in chunk_data])
            
            chunk_id += 1
        
        # Final validation
        results.sort(key=lambda x: x['id'])
        expected_ids = {seg['segment_id'] for seg in transcription}
        received_ids = {seg['id'] for seg in results}
        
        if missing := expected_ids - received_ids:
            logger.warning(f"Adding missing segments: {len(missing)}")
            results.extend([{"id": mid, "ad": False} for mid in missing])
        
        return sorted(results, key=lambda x: x['id'])
        
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        return None

def main():
    input_file = 'output_with_ids.json'
    logger.info(f"Starting processing of file: {input_file}")
    with open('ad_detection_results.json', 'w') as f:
        pass
    results = process_json_file(input_file)
    
    if results:
        # Add schema information to the output
        output = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://example.com/ad-array.schema.json",
            "title": "Advertisement Segments Array",
            "segments": results
        }
        
        # Save with timestamp to keep multiple results
        with open('ad_detection_results.json', 'a') as f:
            f.write('\n')  # Add newline between entries
            json.dump(output, f, indent=2)
        
        ad_count = sum(1 for r in results if r['ad'])
        logger.info(f"Processing complete. Found {ad_count} advertisement segments out of {len(results)} total segments.")
    else:
        logger.error("Processing failed - no results generated")

if __name__ == "__main__":
    main() 