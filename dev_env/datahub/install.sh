#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$SCRIPT_DIR/../lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/../.env" ]]; then
  error ".env not found at $SCRIPT_DIR/../.env — run from dev_env/ and ensure .env exists."
fi
source "$SCRIPT_DIR/../.env"

# Fix #2: fail-fast — nginx-ingress must have populated DATASPOKE_DEV_INGRESS_IP
# before Kafka's EXTERNAL advertised listener can be configured correctly.
: "${DATASPOKE_DEV_INGRESS_IP:?required — run dev_env/nginx-ingress/install.sh first to populate this in .env}"

NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"

echo ""
echo "=== Installing DataHub ==="
echo ""

# ---------------------------------------------------------------------------
# Verify required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
require_tools kubectl helm
info "kubectl and helm are available."

# ---------------------------------------------------------------------------
# Switch Kubernetes context
# ---------------------------------------------------------------------------
use_context "${DATASPOKE_DEV_KUBE_CLUSTER}"

# ---------------------------------------------------------------------------
# Add / update Helm repo
# ---------------------------------------------------------------------------
info "Adding/updating datahub Helm repository..."
helm_repo_add_if_missing datahub https://helm.datahubproject.io/
helm repo update datahub

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
ensure_namespace "${NS}"

# ---------------------------------------------------------------------------
# Create mysql-secrets (idempotent)
# ---------------------------------------------------------------------------
info "Creating mysql-secrets in namespace '${NS}'..."
kubectl create secret generic mysql-secrets \
  --namespace "${NS}" \
  --from-literal=mysql-root-password="${DATASPOKE_DEV_DATAHUB_MYSQL_ROOT_PASSWORD}" \
  --from-literal=mysql-password="${DATASPOKE_DEV_DATAHUB_MYSQL_PASSWORD}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------------------
# Step 1: Install datahub-prerequisites (no --wait, we gate each component)
# ---------------------------------------------------------------------------
PREREQS_VERSION="${DATASPOKE_DEV_KUBE_DATAHUB_PREREQUISITES_CHART_VERSION:-0.3.0}"
info "Installing datahub-prerequisites (version ${PREREQS_VERSION})..."
helm upgrade --install datahub-prerequisites datahub/datahub-prerequisites \
  --version "${PREREQS_VERSION}" \
  --namespace "${NS}" \
  --values "$SCRIPT_DIR/prerequisites-values.yaml" \
  --set-string "kafka.listeners.advertisedListeners=CLIENT://datahub-prerequisites-kafka-controller-0.datahub-prerequisites-kafka-controller-headless.${NS}.svc.cluster.local:9092\,INTERNAL://datahub-prerequisites-kafka-controller-0.datahub-prerequisites-kafka-controller-headless.${NS}.svc.cluster.local:9094\,EXTERNAL://${DATASPOKE_DEV_INGRESS_IP}:9005" \
  --timeout 5m

# ---------------------------------------------------------------------------
# Create Kafka external listener service (for nginx-ingress TCP passthrough)
# The Bitnami Kafka chart only exposes CLIENT (9092) on its ClusterIP service.
# The EXTERNAL listener (9095) needs a dedicated service.
# ---------------------------------------------------------------------------
info "Creating Kafka external listener service..."
kubectl apply -n "${NS}" -f "$SCRIPT_DIR/kafka-external-svc.yaml"

# ---------------------------------------------------------------------------
# Step 2: Wait for each prerequisite sequentially
# ---------------------------------------------------------------------------
info "Waiting for prerequisites to become ready (one by one)..."

info "[1/3] MySQL..."
wait_for_pod "datahub-prerequisites-mysql-0" "$NS" 180

info "[2/3] OpenSearch..."
wait_for_pod "opensearch-cluster-master-0" "$NS" 300

info "[3/3] Kafka (KRaft controller)..."
wait_for_pod "datahub-prerequisites-kafka-controller-0" "$NS" 300

info "All prerequisites are ready."
kubectl get pods -n "${NS}"

# ---------------------------------------------------------------------------
# Step 3: Install datahub WITHOUT --wait
#   Helm's --wait/--timeout applies to pre-install hooks too, causing
#   timeouts when datahub-system-update (a heavy JVM) takes 5-10 min.
#   Instead, we install without --wait and poll for readiness ourselves.
# ---------------------------------------------------------------------------
DATAHUB_VERSION="${DATASPOKE_DEV_KUBE_DATAHUB_CHART_VERSION:-0.9.10}"
info "Installing datahub (chart ${DATAHUB_VERSION}, app v1.5.0.2) — no --wait, polling manually..."
helm upgrade --install datahub datahub/datahub \
  --version "${DATAHUB_VERSION}" \
  --namespace "${NS}" \
  --values "$SCRIPT_DIR/values.yaml" \
  --set "datahub-frontend.ingress.hosts[0].host=datahub.${DATASPOKE_DEV_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
  --timeout 15m

# ---------------------------------------------------------------------------
# Step 4: Wait for hook jobs to complete
#   Chart 0.8.21+ removed the separate elasticsearch-setup-job and
#   mysql-setup-job. All bootstrap work is now done by system-update.
# ---------------------------------------------------------------------------
info "Waiting for system-update jobs..."

# Heavy job: system-update bootstraps all metadata (5-10 min on dev clusters)
wait_for_job "datahub-system-update" "$NS" 600

# ---------------------------------------------------------------------------
# Step 5: Wait for DataHub service pods
# ---------------------------------------------------------------------------
info "Waiting for DataHub services to become ready..."

info "[1/3] GMS..."
wait_for_pod_by_label "app.kubernetes.io/name=datahub-gms" "$NS" 600

info "[2/3] Frontend..."
wait_for_pod_by_label "app.kubernetes.io/name=datahub-frontend" "$NS" 600

info "[3/3] Actions..."
wait_for_pod_by_label "app.kubernetes.io/name=acryl-datahub-actions" "$NS" 300

# ---------------------------------------------------------------------------
# Step 6: Fix MAE consumer stall (DataHub bug workaround)
#   The embedded MAE consumer in GMS crashes when processing stale MCL
#   messages accumulated from previous runs.  The Spring Kafka error handler
#   throws an exception and shuts down the consumer permanently, leaving
#   timeseries aspects (OperationClass, DatasetProfileClass) unindexed in ES.
#   Fix: reset offsets to latest so there's no stale backlog, then restart GMS.
# ---------------------------------------------------------------------------
KAFKA_POD="datahub-prerequisites-kafka-controller-0"
MAE_GROUP="generic-mae-consumer-job-client"

# Poll 3 times with 5s gaps; only reset if all 3 checks confirm stalled.
# A single check can race with a consumer rebalance and falsely look stalled.
STALLED_COUNT=0
for attempt in 1 2 3; do
  MAE_STATE=$(kubectl exec -n "$NS" "$KAFKA_POD" -- \
    /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:9092 \
    --describe --group "$MAE_GROUP" 2>&1 || true)
  if echo "$MAE_STATE" | grep -q "has no active members"; then
    (( STALLED_COUNT++ ))
  fi
  (( attempt < 3 )) && sleep 5
done

if (( STALLED_COUNT == 3 )); then
  info "MAE consumer is stalled (3/3 checks confirm) — resetting offsets..."
  kubectl exec -n "$NS" "$KAFKA_POD" -- \
    /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:9092 \
    --group "$MAE_GROUP" \
    --reset-offsets --to-latest \
    --topic MetadataChangeLog_Timeseries_v1 \
    --topic MetadataChangeLog_Versioned_v1 \
    --execute

  info "Restarting GMS to pick up clean offsets..."
  kubectl delete pod -n "$NS" -l app.kubernetes.io/name=datahub-gms
  wait_for_pod_by_label "app.kubernetes.io/name=datahub-gms" "$NS" 600
else
  info "MAE consumer is active (stalled count: ${STALLED_COUNT}/3) — no offset reset needed."
fi

# ---------------------------------------------------------------------------
# Apply GMS ingress (custom resource — DataHub chart has no native GMS ingress)
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_DEV_INGRESS_DOMAIN:-}" ]]; then
  info "Applying DataHub GMS ingress..."
  DATAHUB_HOST="datahub.${DATASPOKE_DEV_INGRESS_DOMAIN}"
  sed "s/__DATAHUB_INGRESS_HOST__/${DATAHUB_HOST}/g" \
    "$SCRIPT_DIR/gms-ingress.yaml" | kubectl apply -n "${NS}" -f -
  info "GMS ingress applied: http://${DATAHUB_HOST}/gms/"

  # Belt-and-suspenders: reconcile datahub-frontend ingress host.
  # helm --set on hosts[0].host is correctly rendered, but helm rollback /
  # partial upgrades have been observed to leave the live ingress pointing
  # at the stale default host (breaking /logIn, /api/graphql, and PAT
  # generation below). Force the live host to match.
  LIVE_FE_HOST=$(kubectl get ingress datahub-datahub-frontend -n "${NS}" \
    -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || echo "")
  if [[ -n "$LIVE_FE_HOST" && "$LIVE_FE_HOST" != "$DATAHUB_HOST" ]]; then
    warn "datahub-frontend ingress host '${LIVE_FE_HOST}' drifted from '${DATAHUB_HOST}' — patching."
    kubectl patch ingress datahub-datahub-frontend -n "${NS}" --type=json \
      -p="[{\"op\":\"replace\",\"path\":\"/spec/rules/0/host\",\"value\":\"${DATAHUB_HOST}\"}]"
  fi
else
  warn "DATASPOKE_DEV_INGRESS_DOMAIN not set — skipping GMS ingress."
fi

# ---------------------------------------------------------------------------
# Generate Personal Access Token (PAT) for SDK/CLI access
#   GraphQL requires authentication; the reset script and integration tests
#   need a valid token in .env.  Generate one if missing or stale.
# ---------------------------------------------------------------------------
# Write the in-cluster service addresses to .env. These are deterministic
# Service DNS names — stable regardless of ingress configuration.
# dataspoke-infra/install.sh reads these to PATCH the DataHub peripheral config.
upsert_env_var DATASPOKE_DEV_DATAHUB_GMS_URL \
  "http://datahub-datahub-gms.${NS}.svc.cluster.local:8080" \
  "$SCRIPT_DIR/../.env"
upsert_env_var DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS \
  "datahub-prerequisites-kafka.${NS}.svc.cluster.local:9092" \
  "$SCRIPT_DIR/../.env"
info "DATASPOKE_DEV_DATAHUB_GMS_URL and DATASPOKE_DEV_DATAHUB_KAFKA_BROKERS written to .env."

if [[ -n "${DATASPOKE_DEV_INGRESS_DOMAIN:-}" ]]; then
  DATAHUB_FRONTEND="http://datahub.${DATASPOKE_DEV_INGRESS_DOMAIN}"
  GMS_URL="${DATAHUB_FRONTEND}/gms"

  # Re-read .env to pick up any existing token
  source "$SCRIPT_DIR/../.env"
  EXISTING_TOKEN="${DATASPOKE_DEV_DATAHUB_TOKEN:-}"

  NEED_TOKEN=true
  if [[ -n "$EXISTING_TOKEN" ]]; then
    # Verify the existing token still works (DataHub may have been reinstalled)
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
      -X POST "${GMS_URL}/api/graphql" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer ${EXISTING_TOKEN}" \
      -d '{"query":"{ me { corpUser { username } } }"}' 2>/dev/null || echo "000")
    if [[ "$HTTP_CODE" == "200" ]]; then
      info "Existing DataHub PAT is valid — skipping token generation."
      NEED_TOKEN=false
    else
      info "Existing DataHub PAT is stale (HTTP $HTTP_CODE) — regenerating."
    fi
  fi

  if [[ "$NEED_TOKEN" == "true" ]]; then
    info "Generating DataHub personal access token..."
    COOKIE_FILE=$(mktemp)
    trap 'rm -f "$COOKIE_FILE"' EXIT
    curl -s -X POST "${DATAHUB_FRONTEND}/logIn" \
      -H "Content-Type: application/json" \
      -d '{"username":"datahub","password":"datahub"}' \
      -c "$COOKIE_FILE" -o /dev/null 2>/dev/null

    # GMS marks Ready before all bootstrap MCEs (default platform policies)
    # are ingested.  Poll for the "Generate Personal Access Tokens" privilege
    # until it lights up, otherwise createAccessToken returns 403 even though
    # the user logged in successfully.
    info "  Waiting for createAccessToken privilege to bootstrap..."
    PRIV_TIMEOUT=120
    PRIV_ELAPSED=0
    PRIV_READY=false
    while (( PRIV_ELAPSED < PRIV_TIMEOUT )); do
      HAS_PRIV=$(curl -s -X POST "${DATAHUB_FRONTEND}/api/graphql" \
        -H "Content-Type: application/json" \
        -b "$COOKIE_FILE" \
        -d '{"query":"{ me { platformPrivileges { generatePersonalAccessTokens } } }"}' 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['me']['platformPrivileges']['generatePersonalAccessTokens'])" 2>/dev/null \
        || echo "false")
      if [[ "$HAS_PRIV" == "True" ]]; then
        PRIV_READY=true
        break
      fi
      sleep 5
      (( PRIV_ELAPSED += 5 ))
    done
    if [[ "$PRIV_READY" != "true" ]]; then
      warn "  generatePersonalAccessTokens privilege did not bootstrap within ${PRIV_TIMEOUT}s — attempting anyway."
    fi

    PAT_RESPONSE=$(curl -s -X POST "${DATAHUB_FRONTEND}/api/graphql" \
      -H "Content-Type: application/json" \
      -b "$COOKIE_FILE" \
      -d '{"query":"mutation { createAccessToken(input: { type: PERSONAL, actorUrn: \"urn:li:corpuser:datahub\", duration: NO_EXPIRY, name: \"dev-env-token\" }) { accessToken } }"}' 2>/dev/null)
    rm -f "$COOKIE_FILE"
    trap - EXIT

    NEW_TOKEN=$(echo "$PAT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['createAccessToken']['accessToken'])" 2>/dev/null || echo "")
    if [[ -n "$NEW_TOKEN" ]]; then
      upsert_env_var DATASPOKE_DEV_DATAHUB_TOKEN "${NEW_TOKEN}" "$SCRIPT_DIR/../.env"
      info "DataHub PAT written to .env as DATASPOKE_DEV_DATAHUB_TOKEN."
    else
      warn "Failed to generate DataHub PAT. You may need to create one manually."
      warn "Response: $PAT_RESPONSE"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Print access instructions
# ---------------------------------------------------------------------------
echo ""
info "DataHub installation complete."
kubectl get pods -n "${NS}"
echo ""
if [[ -n "${DATASPOKE_DEV_INGRESS_DOMAIN:-}" ]]; then
  echo "  DataHub UI:  http://datahub.${DATASPOKE_DEV_INGRESS_DOMAIN}/"
  echo "  DataHub GMS: http://datahub.${DATASPOKE_DEV_INGRESS_DOMAIN}/gms/"
fi
echo "  Credentials: datahub / datahub"
echo ""
