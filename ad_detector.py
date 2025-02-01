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

# Initialize OpenAI client pointing to the LM Studio server
# llm_model = "deepseek-r1-distill-qwen-14b"
llm_model = "gpt-4o-mini"
client = OpenAI(
#     base_url="http://192.168.178.185:1234/v1",
    api_key=load_api_key()
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

def create_chunk_matrix(transcription: list, chunk_size: int) -> list:
    """
    Split the transcript list into chunks, each represented as a list of (segment_id, text) tuples.
    """
    chunk_matrix = []
    for i in range(0, len(transcription), chunk_size):
        chunk = transcription[i:i + chunk_size]
        # Extract (segment_id, text) tuples for each segment in this chunk.
        chunk_data = [(segment['segment_id'], segment['text']) for segment in chunk]
        chunk_matrix.append(chunk_data)
    return chunk_matrix

def build_prompt(chunk_data: list) -> tuple:
    """
    Build the text prompt and return both the messages to send to the model
    and the list of segment IDs for the current chunk.

    This version incorporates additional instructions:
      - Act as a multilingual advertisement detecting API.
      - Review the transcript as a whole, cluster into topics, and evaluate the likelihood
        of each topic (and therefore the segments) being ad/sponsored content.
      - For clusters/topics considered as ads, justify extensively but briefly.
      - For non-ad segments that are in between ad segments, justify extensively why they are not classified as ad.
      - Reinforce that every segment must be included in the output.
      - Use iterative refinement to ensure no segment is missing.
    """
    # Build a text block for the transcript segments.
    segments_text = "\n".join([f"ID: {seg_id} Text: {text}" for seg_id, text in chunk_data])
    # Collect the expected segment IDs.
    ids = [seg_id for seg_id, _ in chunk_data]
    # Create a comma-separated list of IDs for explicit instruction.
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

def process_single_chunk(chunk_data: list, chunk_id: int, max_attempts: int = 3) -> list:
    """
    Process a single chunk of transcript segments.
    Builds the prompt, sends it to the model, parses the response,
    and returns a list of validated results.
    
    If any segment IDs are missing from the response, the prompt is re-sent,
    up to max_attempts times. If after all attempts segments are still missing,
    missing segments are filled with a default classification (ad: False).
    """
    attempts = 0
    valid_results = None

    while attempts < max_attempts:
        # Build prompt and get expected IDs.
        messages, ids = build_prompt(chunk_data)
        logger.info(create_schema(ids))
        # Request classification from the model.
        response = client.chat.completions.create(
            model=llm_model,
            messages=messages,
            temperature=0.1,
        )
        response_text = response.choices[0].message.content
        logger.info(f"Response for chunk {chunk_id}, attempt {attempts + 1}: {response_text}")
        
        try:
            chunk_results = json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error in chunk {chunk_id}: {e}")
            chunk_results = {}

        # Validate that the response contains a list of segments.
        if not isinstance(chunk_results.get('segments'), list):
            logger.error(f"Invalid response structure in chunk {chunk_id} on attempt {attempts + 1}")
            chunk_results = {'segments': []}

        response_segments = chunk_results['segments']
        valid_results = []
        missing_ids = []

        # Instead of zipping, iterate over the expected segment IDs.
        for seg_id, _ in chunk_data:
            matching_segment = next((seg for seg in response_segments if seg.get('id') == seg_id), None)
            if matching_segment is None:
                missing_ids.append(seg_id)
                valid_results.append(None)  # Placeholder for missing segment.
            else:
                valid_results.append(matching_segment)
        
        # If no segments are missing, we can exit the loop.
        if not missing_ids:
            break
        else:
            logger.warning(f"Chunk {chunk_id}: Missing segments {missing_ids} on attempt {attempts + 1}. Repeating prompt.")
            attempts += 1

    # After max_attempts, fill in any missing segments with default values.
    for index, result in enumerate(valid_results):
        if result is None:
            seg_id = chunk_data[index][0]
            logger.warning(f"Chunk {chunk_id}: Segment with id {seg_id} still missing after {max_attempts} attempts. Defaulting to ad: False.")
            valid_results[index] = {"id": seg_id, "ad": False}

    return valid_results

def process_json_file(filepath: str, chunk_size: int = 800) -> list:
    """
    Process the transcript file:
      - Loads the transcript
      - Splits it into chunks (stored in a matrix)
      - Processes each chunk via the advertisement detection system
      - Performs final validation on the aggregated results.
    
    Returns a sorted list of result objects.
    """
    try:
        transcription = load_transcription(filepath)
        chunk_matrix = create_chunk_matrix(transcription, chunk_size)
        
        results = []
        for chunk_id, chunk_data in enumerate(chunk_matrix):
            chunk_results = process_single_chunk(chunk_data, chunk_id)
            results.extend(chunk_results)
        
        # Final validation: sort results and add missing segments if necessary.
        results.sort(key=lambda x: x['id'])
        expected_ids = {segment['segment_id'] for segment in transcription}
        received_ids = {segment['id'] for segment in results}
        
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