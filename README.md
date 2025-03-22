# PodCleaner

A decoupled service-based system for downloading, transcribing, and removing ads from podcasts.

## Features

- Download podcasts from direct URLs or RSS feeds
- Transcribe audio using OpenAI's Whisper
- Detect advertisements using AI
- Remove ads from the podcast
- Decoupled service architecture using MQTT message broker
- Web API for easy integration

## Architecture

PodCleaner uses a decoupled service architecture:

- **Web Server**: Handles API requests
- **Downloader**: Downloads podcast episodes
- **Transcriber**: Transcribes audio to text
- **Ad Detector**: Analyzes transcriptions to identify advertisements
- **Audio Processor**: Processes audio to remove advertisements
- **Message Broker**: Coordinates communication between services (MQTT)

## Requirements

- Python 3.10+
- OpenAI API key
- FFmpeg (for audio processing)

## Setup

### Local Development

1. Clone the repository
2. Create a virtual environment:
   ```
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   venv\Scripts\activate     # Windows
   ```
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Set your OpenAI API key:
   ```
   export OPENAI_API_KEY=your_api_key_here
   ```
5. Run the services:
   ```
   python -m podcleaner.run_services --mqtt-host localhost --mqtt-port 1883
   ```

### Docker Deployment

1. Clone the repository
2. Set your OpenAI API key:
   ```
   export OPENAI_API_KEY=your_api_key_here
   ```
3. Start the services with Docker Compose:
   ```
   docker-compose up -d
   ```

## Usage

### Web API

- Process a podcast: `GET /process?url=URL_TO_PODCAST`
- Process an RSS feed: `GET /rss?url=URL_TO_RSS_FEED`
- Check status of a request: `GET /status?id=REQUEST_ID`

### Examples

Process a podcast:
```
curl "http://localhost:8080/process?url=https://example.com/podcast.mp3"
```

Process an RSS feed:
```
curl "http://localhost:8080/rss?url=https://feeds.example.com/podcast.xml"
```

Check status:
```
curl "http://localhost:8080/status?id=REQUEST_ID"
```

## Scaling

For production deployments, you can scale the worker services independently:

```
docker-compose up -d --scale worker=3
```

## Configuration

Configuration is managed via `config.yaml`:

- LLM settings (model, temperature, etc.)
- Audio processing parameters
- Message broker settings
- Web server configuration

## Testing

PodCleaner includes a comprehensive test suite to ensure reliability and correctness.

### Running Tests

1. Set up a virtual environment:
   ```
   python -m venv test_env
   source test_env/bin/activate  # Linux/Mac
   test_env\Scripts\activate     # Windows
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   pip install pytest pytest-cov
   ```

3. Run all tests:
   ```
   python -m pytest tests/
   ```

4. Run tests with coverage:
   ```
   python -m pytest --cov=podcleaner tests/
   ```

5. Run specific test categories:
   ```
   python -m pytest tests/test_ad_detector.py  # Test ad detection
   python -m pytest tests/test_transcriber.py  # Test transcription
   python -m pytest tests/test_integration.py  # Test end-to-end workflows
   ```

### Continuous Integration

PodCleaner uses GitHub Actions for continuous integration. Tests are automatically run on each push and pull request to the main branch. You can check the `.github/workflows/test-and-build.yml` file for details.

### Test-Driven Development

For contributors, we recommend running tests before pushing any changes:
   ```
   ./run_tests_and_build.sh
   ```

This script will run all tests and, if they pass, build and start the Docker containers.

## Troubleshooting

- If you encounter OpenAI API quota errors, check your API usage and limits
- Ensure the MQTT broker is running before starting other services
- Check the logs for detailed error information 