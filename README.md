# PodCleaner

A microservices-based application for automatically cleaning podcasts by removing advertisements.

## Features

- Download podcasts from URLs
- Transcribe audio using OpenAI's Whisper model
- Detect advertisements using LLM-based analysis
- Remove identified ads from the audio
- Intelligent file tracking to prevent duplicate processing
- Microservices architecture for scalability
- MQTT-based communication between services
- Docker support for easy deployment
- Web API for submitting podcast processing requests

## Architecture

The application follows a microservices architecture with the following components:

1. **Web Service**: Provides a REST API for submitting podcast processing requests.
2. **Transcriber Service**: Transcribes audio files using OpenAI's Whisper.
3. **Ad Detector Service**: Identifies advertisements in transcripts using LLM-based analysis.
4. **Audio Processor Service**: Removes identified advertisements from the audio.
5. **MQTT Message Broker**: Facilitates communication between services.

The workflow is as follows:

1. A URL is submitted to the web service.
2. The podcast is downloaded and a message is sent to the transcriber service.
3. The audio is transcribed and a message is sent to the ad detector service.
4. Advertisements are detected and a message is sent to the audio processor service.
5. Ads are removed from the audio and the clean file is made available for download.

## Requirements

- Python 3.10 or higher
- FFmpeg (for audio processing)
- Docker and Docker Compose (for containerized deployment)
- External MQTT broker (e.g., Mosquitto)

## Setup

### Local Development

1. Clone the repository:
   ```
   git clone https://github.com/yourusername/podcleaner.git
   cd podcleaner
   ```

2. Create a virtual environment:
   ```
   python -m venv test_env
   source test_env/bin/activate  # On Windows: test_env\Scripts\activate
   ```

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Install FFmpeg:
   - Ubuntu/Debian: `sudo apt-get install ffmpeg`
   - macOS (using Homebrew): `brew install ffmpeg`
   - Windows: Download from [ffmpeg.org](https://ffmpeg.org/download.html)

5. Configure the application by creating a `.env` file with the following settings:
   ```
   OPENAI_API_KEY=your_openai_api_key
   MQTT_HOST=localhost
   MQTT_PORT=1883
   ```

6. Run the application using the unified service runner:
   ```
   # To run all services
   python -m podcleaner.run_service --all
   
   # To run specific services
   python -m podcleaner.run_service --web --transcriber
   ```

### Docker Deployment

1. Build and start the services:
   ```
   docker-compose up -d
   ```

2. Access the web API at http://localhost:8080

## Testing

The project includes a comprehensive test suite to ensure functionality and prevent regressions.

1. Create and activate a test environment:
   ```
   python -m venv test_env
   source test_env/bin/activate  # On Windows: test_env\Scripts\activate
   pip install -r requirements.txt
   ```

2. Run all tests (excluding MQTT broker tests that require a live broker):
   ```
   python -m pytest -k "not test_mqtt_broker"
   ```

3. Run specific test modules:
   ```
   python -m pytest tests/test_service_modules.py
   python -m pytest tests/test_transcriber.py
   ```

4. Run tests with coverage:
   ```
   python -m pytest --cov=podcleaner
   ```

The tests include unit tests for individual components and integration tests to verify the interaction between different services. The duplicate file processing prevention mechanism is also thoroughly tested.

## Web API Usage

The web API provides the following endpoints:

- `POST /podcasts`: Submit a podcast URL for processing
  ```
  curl -X POST http://localhost:8080/podcasts -H "Content-Type: application/json" -d '{"url": "https://example.com/podcast.mp3"}'
  ```

- `GET /podcasts/{id}`: Check the status of a podcast processing request
  ```
  curl http://localhost:8080/podcasts/12345
  ```

- `GET /podcasts/{id}/download`: Download the processed podcast
  ```
  curl -o clean_podcast.mp3 http://localhost:8080/podcasts/12345/download
  ```

## Scaling

Each service can be scaled independently based on resource requirements:

- Transcription is CPU-intensive, so scale the transcriber service for higher throughput.
- Ad detection is memory-intensive, so scale the ad detector service as needed.
- Web and audio processor services are less resource-intensive but can also be scaled.

To scale services using Docker:

```
docker-compose up -d --scale transcriber=3 --scale ad-detector=2
```

## Configuration

Configuration options can be set using environment variables or a `.env` file:

- `LOG_LEVEL`: Logging level (default: INFO)
- `MQTT_HOST`: MQTT broker host
- `MQTT_PORT`: MQTT broker port
- `MQTT_USERNAME`: MQTT broker username (optional)
- `MQTT_PASSWORD`: MQTT broker password (optional)
- `OPENAI_API_KEY`: OpenAI API key for Whisper and LLM
- `AUDIO_MIN_DURATION`: Minimum duration (in seconds) for ad segments (default: 1.0)
- `AUDIO_MAX_GAP`: Maximum gap (in seconds) between ad segments to merge (default: 0.5)

## Troubleshooting

- **Services not connecting to MQTT**: Ensure the MQTT broker is running and accessible.
- **Audio processing errors**: Verify FFmpeg is installed and in the system PATH.
- **Transcription errors**: Check the OpenAI API key and ensure the model is available.
- **Files not being processed**: Check the `debug_output` directory for tracking processed files.

For more information, see the logs of each service. 