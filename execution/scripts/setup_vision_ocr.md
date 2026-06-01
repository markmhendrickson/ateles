# Google Cloud Vision API Setup for PDF OCR

## Quick Setup

### 1. Enable Vision API

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Select your project (or create one)
3. Navigate to [APIs & Services > Library](https://console.cloud.google.com/apis/library)
4. Search for "Cloud Vision API"
5. Click "Enable"

### 2. Create Service Account

1. Go to [APIs & Services > Credentials](https://console.cloud.google.com/apis/credentials)
2. Click "Create Credentials" > "Service Account"
3. Name it (e.g., "vision-ocr-service")
4. Click "Create and Continue"
5. Skip role assignment (or add "Cloud Vision API User")
6. Click "Done"

### 3. Download JSON Key

1. Click on the service account you just created
2. Go to "Keys" tab
3. Click "Add Key" > "Create new key"
4. Choose "JSON"
5. Download the file
6. Save it securely (e.g., `~/.gcp/vision-ocr-key.json`)

### 4. Install Dependencies

```bash
# Install Python packages
pip install google-cloud-vision pdf2image pillow

# Install poppler (required for pdf2image)
# macOS:
brew install poppler

# Linux (Ubuntu/Debian):
sudo apt-get install poppler-utils

# Linux (Fedora):
sudo dnf install poppler-utils
```

### 5. Set Credentials

**Option A: Environment Variable (Recommended)**
```bash
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.gcp/vision-ocr-key.json"
```

**Option B: Use gcloud CLI**
```bash
gcloud auth application-default login
```

**Option C: Pass via script flag**
```bash
python scripts/extract_pdf_ocr_vision.py <pdf_path> --credentials ~/.gcp/vision-ocr-key.json
```

## Usage

```bash
# Basic usage
python scripts/extract_pdf_ocr_vision.py "data/attachments/asana_tasks/1211323698652155/description/Carta de pagament expedient 20250295973.pdf"

# With custom output
python scripts/extract_pdf_ocr_vision.py <pdf_path> --output output.txt

# With credentials file
python scripts/extract_pdf_ocr_vision.py <pdf_path> --credentials ~/.gcp/vision-ocr-key.json
```

## Pricing

- **Free tier:** First 1,000 requests/month
- **After free tier:** $1.50 per 1,000 pages
- **This PDF:** 1 page = $0.0015 (essentially free with free tier)

## Security Note

**DO NOT commit the JSON key file to git.** Add to `.gitignore`:
```
*.json
!package.json
!tsconfig.json
.gcp/
```

## Alternative: Use Existing Gmail Credentials

If you already have Google Cloud credentials set up for Gmail, you can reuse the same project:

1. Just enable Vision API in the same project
2. Use the same service account or create a new one
3. Set `GOOGLE_APPLICATION_CREDENTIALS` to your existing key file

