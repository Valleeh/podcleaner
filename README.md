# PodCleaner

PodCleaner is an automated system designed to download podcast episodes, transcribe them, detect advertisements, and create clean versions without ads.

## Features

- Download podcasts from RSS feeds
- Transcribe audio using Whisper (local or OpenAI API)
- Detect advertisements in transcripts
- Generate clean versions of podcast episodes with ads removed
- Web interface for managing podcast episodes and viewing transcripts
- Object storage support for files (local filesystem, S3, MinIO)

## Architecture

PodCleaner is built using a microservices architecture with the following components:

- **Message Broker**: Handles communication between services
- **Web Server**: Provides REST API and web interface
- **Downloader**: Downloads podcast episodes from RSS feeds
- **Transcriber**: Transcribes audio to text
- **Ad Detector**: Identifies advertisements in transcripts
- **Audio Processor**: Removes ads from audio files
- **Object Storage**: Stores podcast files, transcripts, and processed audio

The services communicate through a message broker using a publish-subscribe pattern.

## Setup

1. Clone the repository:
   ```
   git clone https://github.com/username/podcleaner.git
   cd podcleaner
   ```

2. Create and activate a virtual environment:
   ```
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Configure the application by editing `config.yaml` and `secrets.json`.

5. Run the services:
   ```
   # Start the web service
   python -m podcleaner service --service web

   # Start the transcriber service
   python -m podcleaner service --service transcriber

   # Start the ad-detector service
   python -m podcleaner service --service ad-detector

   # Start the audio-processor service
   python -m podcleaner service --service audio-processor

   # Start the downloader service
   python -m podcleaner service --service downloader
   ```

## Docker Deployment

You can also run PodCleaner using Docker:

```
docker-compose up -d
```

The docker-compose file includes:
- All PodCleaner services
- MQTT message broker
- MinIO object storage (with automatic bucket creation)

## Configuration

##### Object Storage Configuration

PodCleaner supports multiple object storage options:

1. **Local File System**:
   ```yaml
   object_storage:
     provider: "local"
     local:
       base_path: "storage"
   ```

2. **Amazon S3**:
   ```yaml
   object_storage:
     provider: "s3"
     bucket_name: "podcleaner"
     s3:
       region: "us-east-1"
       access_key: "your-access-key"  # Or use environment variables
       secret_key: "your-secret-key"  # Or use environment variables
   ```

3. **MinIO**:
   ```yaml
   object_storage:
     provider: "minio"
     bucket_name: "podcleaner"
     s3:
       endpoint_url: "http://minio:9000"
       access_key: "minioadmin"
       secret_key: "minioadmin"
   ```

Environment variables can be used to configure all settings. See `config.yaml` for available options.

## License

This project is licensed under the MIT License - see the LICENSE file for details. 