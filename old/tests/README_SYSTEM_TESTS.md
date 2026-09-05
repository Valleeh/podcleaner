# PodCleaner System Tests

This directory contains system tests for the PodCleaner application. These tests validate the end-to-end functionality by making real HTTP requests to the web API and verifying that the complete processing pipeline works correctly.

## Prerequisites

- Docker and Docker Compose installed
- Python 3.8+ with pytest and requests
- The PodCleaner application and its dependencies

## Running the Tests

To run the system tests:

```bash
# From the project root directory
pytest tests/test_system.py -v
```

These tests will:
1. Start the PodCleaner system using Docker Compose
2. Run a series of end-to-end tests against the running system
3. Shut down the Docker containers when tests are complete

## Test Configuration

You can customize the system tests by modifying the `tests/system_test_config.json` file. Alternatively, you can specify a different configuration file using the `PODCLEANER_TEST_CONFIG` environment variable:

```bash
PODCLEANER_TEST_CONFIG=/path/to/your/config.json pytest tests/test_system.py -v
```

### Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `base_url` | Base URL of the PodCleaner web API | `http://localhost:8081` |
| `test_podcast_url` | URL of a podcast to use for testing | MIT OpenCourseWare lecture |
| `rss_feed_url` | URL of an RSS feed to use for testing | Vergecast feed |
| `timeout` | Maximum time (seconds) to wait for processing | 120 |
| `poll_interval` | How often (seconds) to check status | 2 |
| `concurrent_requests` | Number of requests for concurrent testing | 3 |
| `additional_test_urls` | Additional podcast URLs for testing | Two MIT lectures |

## Available Tests

1. `test_process_podcast` - Tests basic podcast processing
2. `test_process_rss_feed` - Tests RSS feed processing
3. `test_invalid_url_handling` - Tests error handling with invalid URLs
4. `test_system_health` - Tests the system health endpoint
5. `test_concurrent_processing` - Tests concurrent processing of multiple podcasts

## Extending the Tests

To add new system tests:

1. Add new test functions to `tests/test_system.py`
2. Make sure to use the `system_setup` fixture
3. Follow the pattern of existing tests:
   - Make HTTP requests to the API
   - Assert expected responses
   - Poll for status when necessary
   - Verify outputs

## Troubleshooting

If tests fail, check:
1. Docker logs: `docker-compose logs`
2. System health: `curl http://localhost:8081/health`
3. Test configuration in `system_test_config.json`
4. Network connectivity to test podcast URLs
5. Docker Compose environment variables 