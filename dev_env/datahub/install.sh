#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# shellcheck source=../lib/helpers.sh
source "$SCRIPT_DIR/../lib/helpers.sh"

wait_for_pod() {
  local name="$1" ns="$2" timeout_secs="$3"
  info "  Waiting for pod $name to be Ready (up to ${timeout_secs}s)..."
  local elapsed=0
  while (( elapsed < timeout_secs )); do
    # kubectl wait fails instantly if pod is in CrashLoopBackOff, so we
    # poll manually to tolerate transient restarts during startup.
    local ready
    ready=$(kubectl get "pod/$name" -n "$ns" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
    if [[ "$ready" == "True" ]]; then
      info "  Pod $name is Ready."
      return 0
    fi
    if (( elapsed % 30 == 0 && elapsed > 0 )); then
      local phase restarts
      phase=$(kubectl get "pod/$name" -n "$ns" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
      restarts=$(kubectl get "pod/$name" -n "$ns" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo "?")
      info "  [$name] ${elapsed}s — phase=$phase restarts=$restarts"
    fi
    sleep 10
    (( elapsed += 10 ))
  done
  error "Pod $name not ready after ${timeout_secs}s"
}

wait_for_job() {
  local name="$1" ns="$2" timeout_secs="$3"
  info "  Waiting for job $name to complete (up to ${timeout_secs}s)..."
  local elapsed=0
  while (( elapsed < timeout_secs )); do
    local phase
    phase=$(kubectl get pod -l "job-name=$name" -n "$ns" \
      -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "Pending")
    if [[ "$phase" == "Succeeded" ]]; then
      info "  Job $name completed."
      return 0
    elif [[ "$phase" == "Failed" ]]; then
      error "Job $name failed. Check logs: kubectl logs -l job-name=$name -n $ns"
    fi
    # Print progress every 30s
    if (( elapsed % 30 == 0 && elapsed > 0 )); then
      local tail
      tail=$(kubectl logs -l "job-name=$name" -n "$ns" --tail=1 2>/dev/null || echo "...")
      info "  [$name] ${elapsed}s elapsed — ${tail}"
    fi
    sleep 10
    (( elapsed += 10 ))
  done
  error "Job $name timed out after ${timeout_secs}s"
}

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/../.env" ]]; then
  error ".env not found at $SCRIPT_DIR/../.env — run from dev_env/ and ensure .env exists."
fi
source "$SCRIPT_DIR/../.env"

NS="${DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE}"

echo ""
echo "=== Installing DataHub ==="
echo ""

# ---------------------------------------------------------------------------
# Verify required tools
# ---------------------------------------------------------------------------
info "Checking required tools..."
command -v kubectl >/dev/null 2>&1 || error "kubectl is not installed or not in PATH."
command -v helm    >/dev/null 2>&1 || error "helm is not installed or not in PATH."
info "kubectl and helm are available."

# ---------------------------------------------------------------------------
# Switch Kubernetes context
# ---------------------------------------------------------------------------
info "Switching to Kubernetes context: ${DATASPOKE_DEV_KUBE_CLUSTER}"
kubectl config use-context "${DATASPOKE_DEV_KUBE_CLUSTER}"

# ---------------------------------------------------------------------------
# Add / update Helm repo
# ---------------------------------------------------------------------------
info "Adding/updating datahub Helm repository..."
if helm repo list 2>/dev/null | grep -q "^datahub"; then
  info "Helm repo 'datahub' already added — updating."
  helm repo update datahub
else
  helm repo add datahub https://helm.datahubproject.io/
  helm repo update datahub
fi

# ---------------------------------------------------------------------------
# Ensure namespace exists
# ---------------------------------------------------------------------------
if kubectl get namespace "${NS}" >/dev/null 2>&1; then
  info "Namespace '${NS}' already exists."
else
  info "Creating namespace '${NS}'..."
  kubectl create namespace "${NS}"
fi

# ---------------------------------------------------------------------------
# Create mysql-secrets (idempotent)
# ---------------------------------------------------------------------------
info "Creating mysql-secrets in namespace '${NS}'..."
kubectl create secret generic mysql-secrets \
  --namespace "${NS}" \
  --from-literal=mysql-root-password="${DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_ROOT_PASSWORD}" \
  --from-literal=mysql-password="${DATASPOKE_DEV_KUBE_DATAHUB_MYSQL_PASSWORD}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------------------
# Step 1: Install datahub-prerequisites (no --wait, we gate each component)
# ---------------------------------------------------------------------------
PREREQS_VERSION="${DATASPOKE_DEV_KUBE_DATAHUB_PREREQUISITES_CHART_VERSION:-0.2.1}"
info "Installing datahub-prerequisites (version ${PREREQS_VERSION})..."
helm upgrade --install datahub-prerequisites datahub/datahub-prerequisites \
  --version "${PREREQS_VERSION}" \
  --namespace "${NS}" \
  --values "$SCRIPT_DIR/prerequisites-values.yaml" \
  --set-string "kafka.listeners.advertisedListeners=CLIENT://datahub-prerequisites-kafka-broker-0.datahub-prerequisites-kafka-broker-headless.${NS}.svc.cluster.local:9092\,INTERNAL://datahub-prerequisites-kafka-broker-0.datahub-prerequisites-kafka-broker-headless.${NS}.svc.cluster.local:9094\,EXTERNAL://${DATASPOKE_DEV_INGRESS_IP:-localhost}:9005" \
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

info "[1/4] MySQL..."
wait_for_pod "datahub-prerequisites-mysql-0" "$NS" 180

info "[2/4] Elasticsearch..."
wait_for_pod "elasticsearch-master-0" "$NS" 300

info "[3/4] ZooKeeper..."
wait_for_pod "datahub-prerequisites-zookeeper-0" "$NS" 180

info "[4/4] Kafka..."
wait_for_pod "datahub-prerequisites-kafka-broker-0" "$NS" 300

info "All prerequisites are ready."
kubectl get pods -n "${NS}"

# ---------------------------------------------------------------------------
# Step 3: Install datahub WITHOUT --wait
#   Helm's --wait/--timeout applies to pre-install hooks too, causing
#   timeouts when datahub-system-update (a heavy JVM) takes 5-10 min.
#   Instead, we install without --wait and poll for readiness ourselves.
# ---------------------------------------------------------------------------
DATAHUB_VERSION="${DATASPOKE_DEV_KUBE_DATAHUB_CHART_VERSION:-0.8.21}"
info "Installing datahub (version ${DATAHUB_VERSION}) — no --wait, polling manually..."
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

wait_for_pod_by_label() {
  local label="$1" ns="$2" timeout_secs="$3"
  local name
  name=$(kubectl get pod -l "$label" -n "$ns" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [[ -z "$name" ]]; then
    info "  No pod found with label $label yet, waiting..."
    local waited=0
    while (( waited < timeout_secs )); do
      sleep 10; (( waited += 10 ))
      name=$(kubectl get pod -l "$label" -n "$ns" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
      [[ -n "$name" ]] && break
    done
    [[ -z "$name" ]] && error "No pod found for label $label after ${timeout_secs}s"
  fi
  wait_for_pod "$name" "$ns" "$timeout_secs"
}

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
KAFKA_POD="datahub-prerequisites-kafka-broker-0"
MAE_GROUP="generic-mae-consumer-job-client"

# Check if the MAE consumer has active members (healthy) or is stalled
MAE_STATE=$(kubectl exec -n "$NS" "$KAFKA_POD" -- \
  /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --describe --group "$MAE_GROUP" 2>&1 || true)

if echo "$MAE_STATE" | grep -q "has no active members"; then
  info "MAE consumer is stalled — resetting offsets to skip stale backlog..."
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
  info "MAE consumer is active — no offset reset needed."
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
else
  warn "DATASPOKE_DEV_INGRESS_DOMAIN not set — skipping GMS ingress."
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
