#!/bin/bash
# Set OpenAI API key from 1Password to .env file
# This script reads the key securely and writes it without exposing it

set -e

PROJECT_ROOT="/Users/markmhendrickson/Projects/personal"
ENV_FILE="${PROJECT_ROOT}/.env"
OP_ITEM="ChatGPT / OpenAI"
OP_FIELD="neotoma API key (development)"

# Check if op CLI is available
if ! command -v op &> /dev/null; then
    echo "Error: 1Password CLI (op) not found"
    echo "Install it with: brew install --cask 1password-cli"
    exit 1
fi

# Check if signed in
if ! op account list &> /dev/null; then
    echo "Error: Not signed in to 1Password"
    echo "Sign in with: eval \$(op signin)"
    exit 1
fi

# Read API key from 1Password (without printing it)
echo "Reading API key from 1Password..."
API_KEY=$(op read "op://Private/${OP_ITEM}/${OP_FIELD}" 2>/dev/null)

if [ -z "$API_KEY" ]; then
    echo "Error: Could not read API key from 1Password"
    echo "Item: ${OP_ITEM}"
    echo "Field: ${OP_FIELD}"
    exit 1
fi

# Update .env file
if [ -f "$ENV_FILE" ]; then
    # Remove existing OPENAI_API_KEY line if present
    grep -v "^OPENAI_API_KEY=" "$ENV_FILE" > "${ENV_FILE}.tmp" || true
    mv "${ENV_FILE}.tmp" "$ENV_FILE"
fi

# Append new API key
echo "OPENAI_API_KEY=${API_KEY}" >> "$ENV_FILE"

echo "✓ OpenAI API key set in .env file"
echo "  Item: ${OP_ITEM}"
echo "  Field: ${OP_FIELD}"

