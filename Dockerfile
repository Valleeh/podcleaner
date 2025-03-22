FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir whisper-openai

# Copy the application
COPY . .

# Create directories for data persistence
RUN mkdir -p /app/podcasts /app/debug_output

# Expose ports
EXPOSE 8080

# Environment variables
ENV PYTHONUNBUFFERED=1

# Create entrypoint script
RUN echo '#!/bin/bash\n\
# Load environment variables from secrets.json\n\
if [ -f /app/secrets.json ]; then\n\
    export OPENAI_API_KEY=$(cat /app/secrets.json | grep -o "\"OPENAI_API_KEY\": *\"[^\"]*\"" | cut -d "\"" -f 4)\n\
    export MQTT_HOST=$(cat /app/secrets.json | grep -o "\"MQTT_HOST\": *\"[^\"]*\"" | cut -d "\"" -f 4)\n\
    export MQTT_PORT=$(cat /app/secrets.json | grep -o "\"MQTT_PORT\": *[0-9]*" | cut -d ":" -f 2 | tr -d " ")\n\
    export MQTT_USERNAME=$(cat /app/secrets.json | grep -o "\"MQTT_USERNAME\": *\"[^\"]*\"" | cut -d "\"" -f 4)\n\
    export MQTT_PASSWORD=$(cat /app/secrets.json | grep -o "\"MQTT_PASSWORD\": *\"[^\"]*\"" | cut -d "\"" -f 4)\n\
fi\n\
\n\
# Use environment variables or secrets.json values\n\
MQTT_HOST=${MQTT_HOST:-"localhost"}\n\
MQTT_PORT=${MQTT_PORT:-1883}\n\
MQTT_USERNAME=${MQTT_USERNAME:-null}\n\
MQTT_PASSWORD=${MQTT_PASSWORD:-null}\n\
\n\
if [ "$1" = "web" ]; then\n\
    python -m podcleaner.run_services --mqtt-host ${MQTT_HOST} --mqtt-port ${MQTT_PORT} --mqtt-username ${MQTT_USERNAME} --mqtt-password ${MQTT_PASSWORD} --web-host 0.0.0.0 --web-port ${WEB_PORT:-8080}\n\
elif [ "$1" = "worker" ]; then\n\
    python -m podcleaner.run_worker --mqtt-host ${MQTT_HOST} --mqtt-port ${MQTT_PORT} --mqtt-username ${MQTT_USERNAME} --mqtt-password ${MQTT_PASSWORD}\n\
else\n\
    echo "Usage: $0 [web|worker]"\n\
    exit 1\n\
fi' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["web"] 