#!/bin/bash
# Quick setup script to use same Google Cloud project as Gmail

set -e

echo "Setting up Vision API with same project as Gmail..."
echo "Project: personal-412209"
echo ""

# Set the project
gcloud config set project personal-412209

# Enable Vision API
echo "Enabling Vision API..."
gcloud services enable vision.googleapis.com

# Set up application default credentials
echo ""
echo "Setting up application default credentials..."
echo "This will open a browser for authentication..."
gcloud auth application-default login

echo ""
echo "✅ Setup complete!"
echo ""
echo "You can now run:"
echo "  python scripts/extract_pdf_ocr_vision.py <pdf_path>"
echo ""
echo "Example:"
echo "  python scripts/extract_pdf_ocr_vision.py \"data/attachments/asana_tasks/1211323698652155/description/Carta de pagament expedient 20250295973.pdf\""

