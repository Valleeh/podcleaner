from openai import OpenAI
import json
import logging
from datetime import datetime

def load_api_key(secrets_file="secrets.json"):
    with open(secrets_file) as f:
        secrets = json.load(f)
    return secrets["OPENAI_API_KEY"]

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
llm_model = "gpt-4o-mini"
client = OpenAI(api_key=load_api_key())

def create_schema(ids: list, generate_schema: bool = False):
    """
    Create a JSON schema for the segments array.
    
    If generate_schema is False (default), return a simple fixed schema.
    Otherwise, return a more detailed schema that fixes each ID.
    """
    if not generate_schema:
        return {
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

    items_schemas = []
    for id_val in ids:
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
                "items": items_schemas,
                "minItems": len(ids),
                "maxItems": len(ids)
            }
        },
        "required": ["segments"],
        "additionalProperties": False
    }
    
    return schema

def load_transcription(filepath: str) -> list:
    """
    Load the JSON file and return the list of transcript segments.
    Raises a ValueError if the expected key is missing.
    """
    with open(filepath, 'r') as file:
        data = json.load(file)
    if 'transcription' not in data:
        logger.error("JSON file does not contain 'transcription' key")
        raise ValueError("Missing 'transcription' key in JSON")
    return data['transcription']

def create_chunks(transcription: list, chunk_size: int) -> list:
    """
    Split the transcript list into chunks.
    
    Each chunk is represented as a list of tuples: (segment_id, text).
    """
    chunks = []
    for i in range(0, len(transcription), chunk_size):
        chunk = transcription[i:i + chunk_size]
        # Create a list of (segment_id, text) tuples for the current chunk.
        chunk_data = [(segment['segment_id'], segment['text']) for segment in chunk]
        chunks.append(chunk_data)
    return chunks

def build_prompt(chunk_data: list) -> tuple:
    """
    Build the text prompt from a chunk and return the prompt messages and expected segment IDs.
    """
    segments_text = "\n".join([f"ID: {seg_id} Text: {text}" for seg_id, text in chunk_data])
    ids = [seg_id for seg_id, _ in chunk_data]
    ids_str = ", ".join(str(x) for x in ids)
    
    messages = [
        {
            "role": "user",
            "content": (
                "Review the entire transcript as a whole, cluster the content into topics, and then determine the likelihood that each topic is ad or sponsored content.\n"
                "For each cluster, justify extensively but briefly your decision.\n"
                "For non-ad segments that appear in between ad segments, justify extensively why you are not classifying them as ads.\n"
                "However, if a segment contains clear sponsor or advertisement language, evaluate it individually and classify it as an ad—even if it is in a mixed cluster.\n"
                "At the end, provide a classification for each segment: only mark segments as ad if you are confident that they belong to an advertisement cluster or contain clear ad language. Single segments with obvious ad content should be marked as ad if they are not clearly part of a non-ad cluster.\n"
                "Your overall task is to classify each transcript segment as ad or not ad.\n"
                f"Segments to analyze:\n{segments_text}\n\n"
                "Return JSON matching this exact structure:\n"
                "{\n"
                '    "segments": [\n'
                '        {"id": <segment_id>, "ad": true/false},\n'
                '        {"id": <segment_id>, "ad": true/false},\n'
                "        ...\n"
                "    ]\n"
                "}\n\n"
                "IMPORTANT:\n"
                f"- Include ALL segment IDs from the input. The IDs provided are: {ids_str}.\n"
                "- Maintain the original IDs exactly.\n"
                "- Only return the JSON object with no additional text.\n\n"
                "Reinforce: Your output MUST contain exactly " + str(len(ids)) + " segment entries corresponding to the IDs provided. If any segment is missing, include it with a default value of false.\n\n"
                "Iterative Refinement: Before finalizing your output, double-check that every provided segment ID is present. "
                "If any segment ID is missing, adjust your response to ensure that no segment is omitted."
            )
        }
    ]
    return messages, ids

def process_chunk(chunk_data: list, chunk_id: int, max_attempts: int = 3) -> list:
    """
    Process a single chunk of transcript segments.
    
    This function builds the prompt for the chunk, sends it to the model,
    validates the JSON response, and returns a list of results for the chunk.
    If any segments are missing from the response, it retries up to max_attempts.
    """
    attempts = 0
    valid_results = None
    
    while attempts < max_attempts:
        messages, ids = build_prompt(chunk_data)
        logger.info("Chunk %d: Sending prompt with schema: %s", chunk_id, create_schema(ids))
        response = client.chat.completions.create(
            model=llm_model,
            messages=messages,
            temperature=0.1,
        )
        response_text = response.choices[0].message.content
        logger.info("Chunk %d, attempt %d: %s", chunk_id, attempts + 1, response_text)
        
        try:
            chunk_results = json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error("Chunk %d: JSON decoding error: %s", chunk_id, e)
            chunk_results = {}
        
        if not isinstance(chunk_results.get('segments'), list):
            logger.error("Chunk %d: Invalid response structure; 'segments' is not a list", chunk_id)
            chunk_results['segments'] = []
        
        response_segments = chunk_results['segments']
        valid_results = []
        missing_ids = []
        
        for seg_id, _ in chunk_data:
            matching_segment = next((seg for seg in response_segments if seg.get('id') == seg_id), None)
            if matching_segment is None:
                missing_ids.append(seg_id)
                valid_results.append({"id": seg_id, "ad": False})
            else:
                valid_results.append(matching_segment)
        
        if not missing_ids:
            break
        else:
            logger.warning("Chunk %d: Missing segments for IDs %s. Retrying...", chunk_id, missing_ids)
            attempts += 1
    
    # Final check: fill in any still-missing segments.
    for index, result in enumerate(valid_results):
        if result is None:
            seg_id = chunk_data[index][0]
            logger.warning("Chunk %d: Segment with id %s still missing after %d attempts. Defaulting to ad: False.", chunk_id, seg_id, max_attempts)
            valid_results[index] = {"id": seg_id, "ad": False}
    
    return valid_results

def process_json_file(filepath: str, chunk_size: int = 800) -> list:
    """
    Process the entire transcript file:
      - Load the transcript.
      - Create chunks of transcript segments.
      - Process each chunk for advertisement detection.
      - Aggregate and sort the results.
    
    Returns a sorted list of result objects.
    """
    try:
        transcription = load_transcription(filepath)
        chunks = create_chunks(transcription, chunk_size)
        logger.info("Processing %d chunks from %d segments", len(chunks), len(transcription))
        
        results = []
        for chunk_id, chunk_data in enumerate(chunks):
            chunk_results = process_chunk(chunk_data, chunk_id)
            results.extend(chunk_results)
        
        # Final validation: sort results and ensure all segment IDs are present.
        results.sort(key=lambda x: x['id'])
        expected_ids = {segment['segment_id'] for segment in transcription}
        received_ids = {segment['id'] for segment in results}
        if missing := expected_ids - received_ids:
            logger.warning("Missing segments for IDs: %s. Adding default entries.", missing)
            results.extend([{"id": mid, "ad": False} for mid in missing])
        
        return sorted(results, key=lambda x: x['id'])
    
    except Exception as e:
        logger.error("Error processing file: %s", e)
        return None

def add_segment_ids(input_file: str, output_file: str = None) -> bool:
    """
    Add unique IDs to each segment in the transcript file.
    
    If output_file is not provided, a timestamped filename is generated.
    """
    logger.info("Reading file: %s", input_file)
    try:
        with open(input_file, 'r') as f:
            data = json.load(f)
        
        if 'transcription' not in data:
            logger.error("JSON file does not contain 'transcription' key")
            return False
        
        transcription = data['transcription']
        logger.info("Processing %d segments", len(transcription))
        
        # Add segment IDs to each segment.
        for i, segment in enumerate(transcription):
            segment['segment_id'] = i
        
        # Determine the output filename.
        if output_file is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f'transcript_with_ids_{timestamp}.json'
        
        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2)
        
        logger.info("Added IDs to %d segments. Saved to: %s", len(transcription), output_file)
        return True
        
    except FileNotFoundError:
        logger.error("File not found: %s", input_file)
        return False
    except json.JSONDecodeError:
        logger.error("Invalid JSON in file: %s", input_file)
        return False
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        return False

def main():
    # Set your file names as needed.
    raw_file = 'transcript.json'
    output_file = 'output_with_ids.json'
    
    # First, add segment IDs to the transcript.
    if not add_segment_ids(raw_file, output_file):
        logger.error("Failed to add segment IDs. Exiting.")
        return
    
    logger.info("Starting advertisement detection processing of file: %s", output_file)
    results = process_json_file(output_file, chunk_size=800)
    
    if results:
        # Load the updated transcript.
        with open(output_file, 'r') as f:
            data = json.load(f)
        
        # Map the classification results by segment ID.
        result_map = {item['id']: item['ad'] for item in results}
        
        # Update each segment with its ad classification.
        for segment in data.get("transcription", []):
            seg_id = segment.get("segment_id")
            segment["ad"] = result_map.get(seg_id, False)
        
        # Save the updated transcript back to output_file.
        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2)
        
        # Also, write a separate file with schema metadata.
        ad_detection_output = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://example.com/ad-array.schema.json",
            "title": "Advertisement Segments Array",
            "segments": results
        }
        with open('ad_detection_results.json', 'w') as f:
            json.dump(ad_detection_output, f, indent=2)
        
        ad_count = sum(1 for r in results if r['ad'])
        logger.info("Processing complete. Found %d advertisement segments out of %d total segments.", ad_count, len(results))
    else:
        logger.error("Processing failed - no results generated.")

if __name__ == "__main__":
    main()
