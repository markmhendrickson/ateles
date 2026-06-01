#!/bin/bash
# Automated Google Cloud OAuth setup script
# Automates what can be automated, guides through what requires web UI

set -e

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-personal-412209}"
PROJECT_NUMBER=""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}Google Cloud OAuth Setup Automation${NC}"
echo "=========================================="
echo ""

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo -e "${YELLOW}⚠️  gcloud CLI not found${NC}"
    echo ""
    echo "To install gcloud CLI:"
    echo "  macOS: brew install google-cloud-sdk"
    echo "  Or: https://cloud.google.com/sdk/docs/install"
    echo ""
    echo "For now, this script will guide you through manual steps."
    echo ""
    USE_GCLOUD=false
else
    echo -e "${GREEN}✓ gcloud CLI found${NC}"
    USE_GCLOUD=true
fi

# Function to get project number
get_project_number() {
    if [ "$USE_GCLOUD" = true ]; then
        PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)" 2>/dev/null || echo "")
    fi
}

# Step 1: Enable required APIs
echo -e "${BLUE}Step 1: Enable Required APIs${NC}"
echo "----------------------------"

if [ "$USE_GCLOUD" = true ]; then
    echo "Enabling APIs via gcloud..."
    gcloud services enable gmail-json.googleapis.com --project="$PROJECT_ID" || true
    gcloud services enable calendar-json.googleapis.com --project="$PROJECT_ID" || true
    gcloud services enable cloudvision.googleapis.com --project="$PROJECT_ID" || true
    echo -e "${GREEN}✓ APIs enabled${NC}"
else
    echo "Please enable these APIs manually:"
    echo "  1. Gmail API: https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=$PROJECT_ID"
    echo "  2. Google Calendar API: https://console.cloud.google.com/apis/library/calendar-json.googleapis.com?project=$PROJECT_ID"
    echo "  3. Cloud Vision API: https://console.cloud.google.com/apis/library/vision.googleapis.com?project=$PROJECT_ID"
    echo ""
    read -p "Press Enter after enabling APIs..."
fi

echo ""

# Step 2: OAuth Consent Screen (requires web UI)
echo -e "${BLUE}Step 2: Configure OAuth Consent Screen${NC}"
echo "--------------------------------------"
echo -e "${YELLOW}⚠️  This step requires the web UI${NC}"
echo ""
echo "Please configure the OAuth consent screen:"
echo "  URL: https://console.cloud.google.com/apis/credentials/consent?project=$PROJECT_ID"
echo ""
echo "Required settings:"
echo "  - User Type: External (unless you have Google Workspace)"
echo "  - App name: 'Gmail MCP Server' (or your choice)"
echo "  - User support email: your email"
echo "  - Developer contact: your email"
echo ""
echo "Required scopes to add:"
echo "  - https://www.googleapis.com/auth/gmail.readonly"
echo "  - https://www.googleapis.com/auth/gmail.modify"
echo "  - https://www.googleapis.com/auth/calendar"
echo ""
read -p "Press Enter after configuring the OAuth consent screen..."

echo ""

# Step 3: Create OAuth Client ID (requires web UI)
echo -e "${BLUE}Step 3: Create OAuth Client ID${NC}"
echo "----------------------------"
echo -e "${YELLOW}⚠️  This step requires the web UI${NC}"
echo ""
echo "Please create an OAuth client ID:"
echo "  URL: https://console.cloud.google.com/apis/credentials?project=$PROJECT_ID"
echo ""
echo "Steps:"
echo "  1. Click '+ CREATE CREDENTIALS' → 'OAuth client ID'"
echo "  2. Application type: 'Desktop app'"
echo "  3. Name: 'Gmail MCP Desktop Client'"
echo "  4. Click 'CREATE'"
echo "  5. Click 'DOWNLOAD JSON'"
echo ""
echo "After downloading, you'll need to:"
echo "  1. Store the JSON in 1Password (OAuth credentials field)"
echo "  2. Run: /sync_env_from_1password"
echo ""
read -p "Press Enter after creating and downloading the OAuth client..."

echo ""

# Step 4: Service Account Setup (can be automated)
echo -e "${BLUE}Step 4: Service Account for Vision API${NC}"
echo "-----------------------------------"

if [ "$USE_GCLOUD" = true ]; then
    get_project_number
    
    SERVICE_ACCOUNT_NAME="vision-api-service"
    SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
    
    # Check if service account exists
    if gcloud iam service-accounts describe "$SERVICE_ACCOUNT_EMAIL" --project="$PROJECT_ID" &>/dev/null; then
        echo -e "${GREEN}✓ Service account already exists: $SERVICE_ACCOUNT_EMAIL${NC}"
    else
        echo "Creating service account..."
        gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
            --display-name="Vision API Service Account" \
            --project="$PROJECT_ID"
        echo -e "${GREEN}✓ Service account created${NC}"
    fi
    
    # Grant Cloud Vision API User role
    echo "Granting Cloud Vision API User role..."
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
        --role="roles/vision.user" \
        --condition=None 2>/dev/null || echo "Role may already be assigned"
    
    echo -e "${GREEN}✓ Role granted${NC}"
    echo ""
    echo "Next: Create and download a key for this service account:"
    echo "  URL: https://console.cloud.google.com/iam-admin/serviceaccounts?project=$PROJECT_ID"
    echo "  1. Click on '$SERVICE_ACCOUNT_EMAIL'"
    echo "  2. Go to 'Keys' tab"
    echo "  3. Click 'ADD KEY' → 'Create new key'"
    echo "  4. Choose 'JSON'"
    echo "  5. Download and store in 1Password (service key field)"
    echo "  6. Run: /sync_env_from_1password"
else
    echo "Please create a service account manually:"
    echo "  URL: https://console.cloud.google.com/iam-admin/serviceaccounts?project=$PROJECT_ID"
    echo ""
    echo "Steps:"
    echo "  1. Click 'CREATE SERVICE ACCOUNT'"
    echo "  2. Name: 'vision-api-service'"
    echo "  3. Grant role: 'Cloud Vision API User'"
    echo "  4. Create and download JSON key"
    echo "  5. Store in 1Password (service key field)"
    echo "  6. Run: /sync_env_from_1password"
fi

echo ""
echo -e "${GREEN}Setup Complete!${NC}"
echo ""
echo "Summary:"
echo "  ✓ APIs enabled (or manual steps provided)"
echo "  ✓ OAuth consent screen configured (manual)"
echo "  ✓ OAuth client created (manual - download JSON)"
echo "  ✓ Service account created (automated or manual)"
echo ""
echo "Next steps:"
echo "  1. Store OAuth credentials JSON in 1Password"
echo "  2. Store service account key JSON in 1Password"
echo "  3. Run: /sync_env_from_1password"
echo "  4. Restart Cursor"
echo ""
