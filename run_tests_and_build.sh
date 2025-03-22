#!/bin/bash

set -e  # Exit on any error

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Running tests before building and deploying...${NC}"

# Check for pytest
if ! command -v pytest &> /dev/null; then
    echo -e "${RED}pytest not found. Please install it with: pip install pytest${NC}"
    exit 1
fi

# Run tests
echo -e "${YELLOW}Running unit tests...${NC}"
pytest -xvs tests/

# If tests pass, build and deploy
if [ $? -eq 0 ]; then
    echo -e "${GREEN}All tests passed! Building Docker containers...${NC}"
    
    # Build Docker containers
    docker-compose build
    
    echo -e "${GREEN}Build complete! Starting containers...${NC}"
    
    # Start containers
    docker-compose up -d
    
    echo -e "${GREEN}Deployment complete!${NC}"
else
    echo -e "${RED}Tests failed. Fix issues before building.${NC}"
    exit 1
fi 