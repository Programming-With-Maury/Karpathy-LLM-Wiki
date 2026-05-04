#!/bin/bash
# Auto-commit script for the LLM Wiki

# Exit immediately if a command exits with a non-zero status
set -e

# Change to the root of the workspace
cd "$(dirname "$0")/.."

# Check if there are any changes
if [[ -z $(git status -s) ]]; then
  echo "No changes to commit."
  exit 0
fi

# Stage all changes
git add .

# Create a timestamp
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

# Commit
git commit -m "chore: auto-update wiki [$TIMESTAMP]"

echo "Wiki changes successfully committed!"
