#!/usr/bin/env bash
# Install DataHub (prerequisites + main chart) and write connection outputs
# back to helm-charts/.env.dev.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$(cd "$BIN_DIR/.." && pwd)/.env.dev}"
PERIPHERALS_DIR="$(cd "$BIN_DIR/../dev-peripherals" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$BIN_DIR/lib/helpers.sh"

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  error "Env file not found at $ENV_FILE — copy helm-charts/.env.dev.example and edit it."
fi
source "$ENV_FILE"

# Host used by Kafka's EXTERNAL advertised listener and for laptop-side test
# access to the broker. In managed mode this is the ingress LoadBalancer IP
# (which must already be populated by nginx-ingress.sh); in shared mode it is
# 127.0.0.1, reached via `kubectl port-forward` (bin/port-forward.sh), as the
# broker advertises 127.0.0.1:9005 so a forwarded client reconnects correctly.
if [[ "$(ingress_mode)" != "shared" ]]; then
  : "${DATASPOKE_KUBE_INGRESS_IP:?required in managed mode — run bin/dev-peripherals/nginx-ingress.sh first to populate this in .env}"
fi
KAFKA_EXTERNAL_HOST="$(tcp_access_host)"

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
use_context "${DATASPOKE_KUBE_CLUSTER}"

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
  --values "$PERIPHERALS_DIR/datahub/prerequisites-values.yaml" \
  --set-string "kafka.listeners.advertisedListeners=CLIENT://datahub-prerequisites-kafka-controller-0.datahub-prerequisites-kafka-controller-headless.${NS}.svc.cluster.local:9092\,INTERNAL://datahub-prerequisites-kafka-controller-0.datahub-prerequisites-kafka-controller-headless.${NS}.svc.cluster.local:9094\,EXTERNAL://${KAFKA_EXTERNAL_HOST}:9005" \
  --timeout 5m

# ---------------------------------------------------------------------------
# Create Kafka external listener service (for nginx-ingress TCP passthrough)
# ---------------------------------------------------------------------------
info "Creating Kafka external listener service..."
kubectl apply -n "${NS}" -f "$PERIPHERALS_DIR/datahub/kafka-external-svc.yaml"

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
# ---------------------------------------------------------------------------
DATAHUB_VERSION="${DATASPOKE_DEV_KUBE_DATAHUB_CHART_VERSION:-1.0.1}"

oidc_args=()
if [[ -n "${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID:-}" && -n "${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET:-}" ]]; then
  # SSO is configured on the datahub-frontend subchart's oidcAuthentication block,
  # which renders the AUTH_OIDC_* env the Play frontend reads. user_name_claim_regex
  # = "(.*)" keeps the full email as the corpuser id (default "([^@]+)" strips the
  # domain), so it matches DataSpoke-mirrored corpusers (urn:li:corpuser:<email>).
  # The chart derives an https AUTH_OIDC_BASE_URL from the ingress host; dev serves
  # DataHub over plain HTTP, so set oidcBaseUrl to override it (one env value — an
  # extraEnvs duplicate would break the strategic-merge patch on helm upgrade) so the
  # OAuth redirect_uri matches the registered http://datahub.../callback/oidc.
  # provider=google makes the chart set the OIDC discoveryUri automatically.

  # Store the OIDC client secret in a K8s Secret so it never appears in helm
  # release values, kubectl get deploy -o yaml, ps output, or CI logs.
  # Verified against datahub-frontend subchart template (datahub-1.0.1):
  #   oidcAuthentication.clientSecretRef.secretRef  → secretKeyRef.name
  #   oidcAuthentication.clientSecretRef.secretKey  → secretKeyRef.key
  info "Creating datahub-oidc-secret in namespace '${NS}'..."
  kubectl create secret generic datahub-oidc-secret \
    --namespace "${NS}" \
    --from-literal=client-secret="${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET}" \
    --dry-run=client -o yaml | kubectl apply -f -

  oidc_base="http://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}"
  oidc_args+=(
    --set "datahub-frontend.oidcAuthentication.enabled=true"
    --set-string "datahub-frontend.oidcAuthentication.provider=google"
    --set-string "datahub-frontend.oidcAuthentication.clientId=${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID}"
    --set-string "datahub-frontend.oidcAuthentication.clientSecretRef.secretRef=datahub-oidc-secret"
    --set-string "datahub-frontend.oidcAuthentication.clientSecretRef.secretKey=client-secret"
    --set-string "datahub-frontend.oidcAuthentication.oidcBaseUrl=${oidc_base}"
    --set-string "datahub-frontend.oidcAuthentication.user_name_claim=email"
    --set-string "datahub-frontend.oidcAuthentication.user_name_claim_regex=(.*)"
  )
  info "Google OIDC enabled on DataHub (client secret stored in datahub-oidc-secret)"
elif [[ -n "${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID:-}" || -n "${DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_SECRET:-}" ]]; then
  warn "Google OIDC partially configured (only one of CLIENT_ID/CLIENT_SECRET set); both required — skipping OIDC"
else
  info "Google OIDC disabled on DataHub (no DATASPOKE_DEV_GOOGLE_OAUTH_CLIENT_ID/SECRET in .env)"
fi

info "Installing datahub (chart ${DATAHUB_VERSION}, app v1.6.0) — no --wait, polling manually..."
helm upgrade --install datahub datahub/datahub \
  --version "${DATAHUB_VERSION}" \
  --namespace "${NS}" \
  --values "$PERIPHERALS_DIR/datahub/values.yaml" \
  --set "datahub-frontend.ingress.hosts[0].host=datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN:-dev.dataspoke.example.com}" \
  ${oidc_args[@]+"${oidc_args[@]}"} \
  --timeout 15m

# ---------------------------------------------------------------------------
# Step 4: Wait for hook jobs to complete
# ---------------------------------------------------------------------------
info "Waiting for system-update jobs..."
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
# ---------------------------------------------------------------------------
KAFKA_POD="datahub-prerequisites-kafka-controller-0"
MAE_GROUP="generic-mae-consumer-job-client"

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
# Apply GMS ingress
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_KUBE_INGRESS_DOMAIN:-}" ]]; then
  info "Applying DataHub GMS ingress..."
  DATAHUB_HOST="datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN}"
  sed "s/__DATAHUB_INGRESS_HOST__/${DATAHUB_HOST}/g" \
    "$PERIPHERALS_DIR/datahub/gms-ingress.yaml" | kubectl apply -n "${NS}" -f -
  info "GMS ingress applied: http://${DATAHUB_HOST}/gms/"

  # Reconcile datahub-frontend ingress host to avoid stale defaults.
  LIVE_FE_HOST=$(kubectl get ingress datahub-datahub-frontend -n "${NS}" \
    -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || echo "")
  if [[ -n "$LIVE_FE_HOST" && "$LIVE_FE_HOST" != "$DATAHUB_HOST" ]]; then
    warn "datahub-frontend ingress host '${LIVE_FE_HOST}' drifted from '${DATAHUB_HOST}' — patching."
    kubectl patch ingress datahub-datahub-frontend -n "${NS}" --type=json \
      -p="[{\"op\":\"replace\",\"path\":\"/spec/rules/0/host\",\"value\":\"${DATAHUB_HOST}\"}]"
  fi
else
  warn "DATASPOKE_KUBE_INGRESS_DOMAIN not set — skipping GMS ingress."
fi

# ---------------------------------------------------------------------------
# Write laptop-reachable (ingress) service addresses to .env
#
# Tests, CLI scripts, and SDK calls all run on the developer's laptop and
# must reach DataHub via the nginx-ingress; the in-cluster service DNS is
# not resolvable from outside the cluster. The API itself reads DataHub
# connection details from the peripheral_config DB (seeded with the
# in-cluster URL by post-install/seed-peripheral-config.sh), not from .env.
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_KUBE_INGRESS_DOMAIN:-}" ]]; then
  upsert_env_var DATASPOKE_TEST_DATAHUB_GMS_URL \
    "http://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN}/gms" \
    "$ENV_FILE"
  upsert_env_var DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS \
    "${KAFKA_EXTERNAL_HOST}:9005" \
    "$ENV_FILE"
  info "DATASPOKE_TEST_DATAHUB_GMS_URL and DATASPOKE_TEST_DATAHUB_KAFKA_BROKERS written to .env."
else
  warn "DATASPOKE_KUBE_INGRESS_DOMAIN not set — skipping DataHub .env addresses."
fi

# ---------------------------------------------------------------------------
# Generate Personal Access Token (PAT) for SDK/CLI access
# ---------------------------------------------------------------------------
if [[ -n "${DATASPOKE_KUBE_INGRESS_DOMAIN:-}" ]]; then
  GMS_URL="http://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN}/gms"

  # Re-read .env to pick up any existing token
  source "$ENV_FILE"
  EXISTING_TOKEN="${DATASPOKE_TEST_DATAHUB_TOKEN:-}"

  NEED_TOKEN=true
  if [[ -n "$EXISTING_TOKEN" ]]; then
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

    # The system client id is the DataHub default identifier (config
    # DATAHUB_SYSTEM_CLIENT_ID, default __datahub_system); it is not a Secret.
    # Only the system_client_secret lives in datahub-auth-secrets.
    SYS_ID="${DATAHUB_SYSTEM_CLIENT_ID:-__datahub_system}"
    SYS_SECRET=$(kubectl get secret datahub-auth-secrets -n "${NS}" \
      -o jsonpath='{.data.system_client_secret}' 2>/dev/null \
      | base64 -d 2>/dev/null || echo "")

    if [[ -z "$SYS_SECRET" ]]; then
      error "datahub-auth-secrets in namespace '${NS}' is missing or lacks system_client_secret — cannot mint PAT."
    fi

    # Obtain a GMS session token for the datahub user via system-auth.
    # The Authorization header carries credentials in plaintext (<id>:<secret>);
    # DataHub's DataHubSystemAuthenticator splits on ':' after stripping "Basic ".
    # userId is sent in the JSON body (AuthServiceController reads it from there).
    # Retry to tolerate GMS still initialising after the readiness gates above.
    info "  Obtaining GMS session token for datahub user..."
    MINT_TIMEOUT=120
    MINT_ELAPSED=0
    SESSION_TOKEN=""
    while (( MINT_ELAPSED < MINT_TIMEOUT )); do
      RESP=$(curl -s -X POST \
        "${GMS_URL}/auth/generateSessionTokenForUser?userId=datahub" \
        -H "Authorization: Basic ${SYS_ID}:${SYS_SECRET}" \
        -H "Content-Type: application/json" \
        -d '{"userId":"datahub"}' \
        2>/dev/null || echo "")
      SESSION_TOKEN=$(printf '%s' "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['accessToken'] if isinstance(d, dict) else d)" 2>/dev/null || echo "")
      [[ -n "$SESSION_TOKEN" ]] && break
      sleep 10
      (( MINT_ELAPSED += 10 ))
    done

    if [[ -z "$SESSION_TOKEN" ]]; then
      error "Failed to obtain a GMS session token after ${MINT_TIMEOUT}s — cannot mint PAT."
    fi

    # Mint the PAT. Immediately after a fresh DataHub boot the DataHubAuthorizer's
    # policy cache may not yet hold the default policy that grants the root datahub
    # user GENERATE_PERSONAL_ACCESS_TOKENS — the policy index in OpenSearch lags
    # pod-readiness — so createAccessToken returns a transient 403 UNAUTHORIZED even
    # though the session token authenticates fine. Retry until the privilege
    # resolves, refreshing the session token in case it expires during the wait.
    PAT_TIMEOUT=180
    PAT_ELAPSED=0
    NEW_TOKEN=""
    PAT_RESPONSE=""
    while :; do
      PAT_RESPONSE=$(curl -s -X POST "${GMS_URL}/api/graphql" \
        -H "Authorization: Bearer ${SESSION_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{"query":"mutation { createAccessToken(input: { type: PERSONAL, actorUrn: \"urn:li:corpuser:datahub\", duration: NO_EXPIRY, name: \"dev-env-token\" }) { accessToken } }"}' 2>/dev/null || echo "")
      NEW_TOKEN=$(echo "$PAT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['createAccessToken']['accessToken'])" 2>/dev/null || echo "")
      [[ -n "$NEW_TOKEN" ]] && break
      (( PAT_ELAPSED >= PAT_TIMEOUT )) && break
      info "  PAT not yet authorized (policy cache warming) — retrying in 10s..."
      sleep 10
      (( PAT_ELAPSED += 10 ))
      # Refresh the session token; it may have expired during the wait.
      RESP=$(curl -s -X POST \
        "${GMS_URL}/auth/generateSessionTokenForUser?userId=datahub" \
        -H "Authorization: Basic ${SYS_ID}:${SYS_SECRET}" \
        -H "Content-Type: application/json" \
        -d '{"userId":"datahub"}' \
        2>/dev/null || echo "")
      FRESH_TOKEN=$(printf '%s' "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['accessToken'] if isinstance(d, dict) else d)" 2>/dev/null || echo "")
      [[ -n "$FRESH_TOKEN" ]] && SESSION_TOKEN="$FRESH_TOKEN"
    done

    if [[ -n "$NEW_TOKEN" ]]; then
      upsert_env_var DATASPOKE_TEST_DATAHUB_TOKEN "${NEW_TOKEN}" "$ENV_FILE"
      info "DataHub PAT written to .env as DATASPOKE_TEST_DATAHUB_TOKEN."
    else
      # Extract only the errors[] array — never log the raw response which may
      # contain a partial token, user URNs, or platform-privilege metadata.
      ERRORS=$(echo "$PAT_RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    errs = data.get('errors', [])
    print(json.dumps(errs, indent=2) if errs else '<no errors[] field; response shape unexpected>')
except Exception as e:
    print(f'<unparseable response: {e}>')
")
      warn "DataHub GraphQL errors: $ERRORS"
      error "Failed to generate DataHub PAT — cannot proceed without DATASPOKE_TEST_DATAHUB_TOKEN."
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
if [[ -n "${DATASPOKE_KUBE_INGRESS_DOMAIN:-}" ]]; then
  echo "  DataHub UI:  http://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN}/"
  echo "  DataHub GMS: http://datahub.${DATASPOKE_KUBE_INGRESS_DOMAIN}/gms/"
fi
echo "  Credentials: datahub / datahub"
echo ""
