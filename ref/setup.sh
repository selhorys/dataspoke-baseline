#!/usr/bin/env bash
set -euo pipefail

# Reference Material Setup Script
# Downloads external source code for AI assistant reference
#
# Usage:
#   ./setup.sh           # Download all reference materials
#   ./setup.sh datahub   # Download only DataHub source
#   ./setup.sh --clean   # Remove all downloaded content

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GITHUB_DIR="${SCRIPT_DIR}/github"

# Load DataHub version from dev_env/.env
DEV_ENV_FILE="${SCRIPT_DIR}/../dev_env/.env"
if [[ ! -f "${DEV_ENV_FILE}" ]]; then
    echo "❌ Error: dev_env/.env not found at ${DEV_ENV_FILE}"
    exit 1
fi

source "${DEV_ENV_FILE}"
DATAHUB_VERSION="v1.5.0.2"
DATAHUB_REPO="https://github.com/datahub-project/datahub.git"
DATAHUB_DIR="${GITHUB_DIR}/datahub"

# Color output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}ℹ${NC}  $1"
}

log_success() {
    echo -e "${GREEN}✓${NC}  $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC}  $1"
}

log_error() {
    echo -e "${RED}✗${NC}  $1"
}

# Clean all downloaded content
clean_all() {
    log_info "Cleaning all reference materials..."
    if [[ -d "${GITHUB_DIR}" ]]; then
        rm -rf "${GITHUB_DIR}"
        log_success "Removed ${GITHUB_DIR}"
    else
        log_warn "Nothing to clean (${GITHUB_DIR} does not exist)"
    fi
}

# Download DataHub source code
setup_datahub() {
    log_info "Setting up DataHub reference (version ${DATAHUB_VERSION})..."

    mkdir -p "${GITHUB_DIR}"

    if [[ -d "${DATAHUB_DIR}" ]]; then
        log_warn "DataHub directory already exists: ${DATAHUB_DIR}"
        read -p "   Remove and re-clone? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -rf "${DATAHUB_DIR}"
        else
            log_info "Skipping DataHub setup"
            return 0
        fi
    fi

    log_info "Cloning DataHub repository (this may take 2-5 minutes)..."
    git clone --depth 1 --branch "${DATAHUB_VERSION}" "${DATAHUB_REPO}" "${DATAHUB_DIR}"

    # Optional: Remove large directories not needed for AI reference
    log_info "Cleaning up unnecessary files for AI reference..."
    cd "${DATAHUB_DIR}"

    # Remove build artifacts, test data, and large binaries
    rm -rf .git              # Git history not needed
    rm -rf docker/           # Docker images not needed (we use Helm)
    rm -rf docs-website/     # Documentation site build (keep markdown docs)

    log_success "DataHub source code ready at ${DATAHUB_DIR}"
    log_info "Version: ${DATAHUB_VERSION}"
    log_info "Key directories for reference:"
    echo "   - metadata-models/      # Entity schemas (PDL/Avro)"
    echo "   - metadata-service/     # GMS backend (Java/Spring)"
    echo "   - datahub-web-react/    # Frontend (TypeScript/React)"
    echo "   - metadata-ingestion/   # Python SDK and ingestion framework"
    echo "   - datahub-graphql-core/ # GraphQL API schemas"
}

# Main setup logic
setup_all() {
    log_info "Setting up all reference materials..."
    setup_datahub
    log_success "All reference materials downloaded"
}

# Parse command line arguments
case "${1:-}" in
    datahub)
        setup_datahub
        ;;
    --all)
        setup_all
        ;;
    --clean)
        clean_all
        ;;
    "")
        setup_all
        ;;
    *)
        echo "Usage: $0 [datahub|--all|--clean]"
        echo ""
        echo "Options:"
        echo "  (no args)    Download all reference materials (default)"
        echo "  datahub      Download only DataHub source code"
        echo "  --all        Download all reference materials (explicit)"
        echo "  --clean      Remove all downloaded content"
        exit 1
        ;;
esac
