#!/usr/bin/env bash
#
# One-command deploy of news-fanout to a single Compute Engine VM running
# docker compose (app + Postgres + Redis).
#
#   cp deploy/config.env.example deploy/config.env   # edit PROJECT_ID
#   ./deploy/gcp-deploy.sh
#
# Every step is idempotent, so this doubles as the redeploy command: re-running it
# rebuilds the image, pushes a new tag and restarts the app on the VM. Existing
# Postgres data survives (it lives in a named docker volume).
#
#   ./deploy/gcp-deploy.sh                 full deploy / redeploy
#   ./deploy/gcp-deploy.sh --app-only      skip infra, just rebuild+push+restart
#   ./deploy/gcp-deploy.sh --infra-only    create infra, do not deploy the app
#   ./deploy/gcp-deploy.sh --no-test       skip the post-deploy end-to-end test
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --------------------------------------------------------------------- config

[ -f deploy/config.env ] && . deploy/config.env

PROJECT_ID="${PROJECT_ID:-}"
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"
REGION="${REGION:-europe-west4}"
ZONE="${ZONE:-${REGION}-a}"
VM_NAME="${VM_NAME:-news-fanout-vm}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-small}"
DISK_SIZE="${DISK_SIZE:-20GB}"
AR_REPO="${AR_REPO:-news-fanout}"
IMAGE_NAME="${IMAGE_NAME:-news-fanout}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
ALLOW_CIDR="${ALLOW_CIDR:-0.0.0.0/0}"

FIREWALL_RULE="allow-news-fanout-http"
REMOTE_DIR="/opt/news-fanout"
SECRETS_DIR="deploy/.secrets"

DO_INFRA=1
DO_APP=1
DO_TEST=1
for arg in "$@"; do
    case "$arg" in
        --app-only) DO_INFRA=0 ;;
        --infra-only) DO_APP=0; DO_TEST=0 ;;
        --no-test) DO_TEST=0 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------- output

step()  { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
ok()    { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()   { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- prerequisites

step "Checking prerequisites"

command -v gcloud >/dev/null 2>&1 || die "gcloud not found. Install: https://cloud.google.com/sdk/docs/install"
command -v docker >/dev/null 2>&1 || die "docker not found. Start OrbStack or Docker Desktop."
docker buildx version >/dev/null 2>&1 || die "docker buildx not available; it is required to cross-build for linux/amd64."
docker info >/dev/null 2>&1 || die "docker daemon is not responding. Is OrbStack running?"
ok "gcloud, docker and buildx present"

ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
[ -n "$ACTIVE_ACCOUNT" ] || die "no active gcloud account. Run: gcloud auth login"
ok "authenticated as $ACTIVE_ACCOUNT"

[ -n "$PROJECT_ID" ] || die "PROJECT_ID is not set. Copy deploy/config.env.example to deploy/config.env and set it."
info "project: $PROJECT_ID   zone: $ZONE   machine: $MACHINE_TYPE"

GCLOUD=(gcloud --project "$PROJECT_ID" --quiet)
IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}"
IMAGE_URI="${IMAGE_REPO}:${IMAGE_TAG}"

# ------------------------------------------------------------------- password

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"
PASSWORD_FILE="${SECRETS_DIR}/${PROJECT_ID}.postgres-password"
if [ -z "$POSTGRES_PASSWORD" ]; then
    if [ -f "$PASSWORD_FILE" ]; then
        POSTGRES_PASSWORD="$(cat "$PASSWORD_FILE")"
        info "reusing stored Postgres password ($PASSWORD_FILE)"
    else
        POSTGRES_PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
        printf '%s' "$POSTGRES_PASSWORD" >"$PASSWORD_FILE"
        chmod 600 "$PASSWORD_FILE"
        ok "generated Postgres password, stored in $PASSWORD_FILE"
    fi
fi

# ----------------------------------------------------------------------- infra

if [ "$DO_INFRA" -eq 1 ]; then
    step "Project"
    if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
        ok "project $PROJECT_ID already exists"
    else
        info "creating project $PROJECT_ID"
        gcloud projects create "$PROJECT_ID" --name="news-fanout" --quiet \
            || die "could not create project $PROJECT_ID (the ID may be taken globally)"
        ok "project created"
    fi

    step "Billing"
    BILLING_ENABLED="$(gcloud billing projects describe "$PROJECT_ID" \
        --format='value(billingEnabled)' 2>/dev/null || echo "")"
    if [ "$BILLING_ENABLED" = "True" ]; then
        ok "billing already enabled"
    else
        if [ -z "$BILLING_ACCOUNT" ]; then
            BILLING_ACCOUNT="$(gcloud billing accounts list \
                --filter='open=true' --format='value(name)' 2>/dev/null | head -1 | sed 's|billingAccounts/||')"
        fi
        [ -n "$BILLING_ACCOUNT" ] || die "no open billing account found. Set BILLING_ACCOUNT in deploy/config.env."
        info "linking billing account $BILLING_ACCOUNT"
        gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT" --quiet \
            || die "failed to link billing. Compute Engine cannot be used without it."
        ok "billing linked"
    fi

    step "Enabling APIs (first run takes a minute)"
    "${GCLOUD[@]}" services enable compute.googleapis.com artifactregistry.googleapis.com \
        || die "failed to enable required APIs"
    ok "compute + artifactregistry enabled"

    step "Artifact Registry"
    if "${GCLOUD[@]}" artifacts repositories describe "$AR_REPO" --location "$REGION" >/dev/null 2>&1; then
        ok "repository $AR_REPO already exists"
    else
        "${GCLOUD[@]}" artifacts repositories create "$AR_REPO" \
            --repository-format=docker --location="$REGION" \
            --description="news-fanout container images"
        ok "repository created"
    fi

    step "Firewall"
    if "${GCLOUD[@]}" compute firewall-rules describe "$FIREWALL_RULE" >/dev/null 2>&1; then
        "${GCLOUD[@]}" compute firewall-rules update "$FIREWALL_RULE" --source-ranges="$ALLOW_CIDR" >/dev/null
        ok "rule $FIREWALL_RULE updated (source $ALLOW_CIDR)"
    else
        "${GCLOUD[@]}" compute firewall-rules create "$FIREWALL_RULE" \
            --allow=tcp:80 --source-ranges="$ALLOW_CIDR" \
            --target-tags=news-fanout --description="news-fanout HTTP"
        ok "rule $FIREWALL_RULE created (source $ALLOW_CIDR)"
    fi
    [ "$ALLOW_CIDR" = "0.0.0.0/0" ] && warn "port 80 is open to the internet; narrow ALLOW_CIDR for a private demo"

    step "VM"
    if "${GCLOUD[@]}" compute instances describe "$VM_NAME" --zone "$ZONE" >/dev/null 2>&1; then
        ok "instance $VM_NAME already exists"
    else
        info "creating $MACHINE_TYPE instance (Debian 12, $DISK_SIZE)"
        "${GCLOUD[@]}" compute instances create "$VM_NAME" \
            --zone="$ZONE" \
            --machine-type="$MACHINE_TYPE" \
            --image-family=debian-12 \
            --image-project=debian-cloud \
            --boot-disk-size="$DISK_SIZE" \
            --boot-disk-type=pd-balanced \
            --tags=news-fanout \
            --scopes=cloud-platform \
            --metadata-from-file=startup-script=deploy/vm-startup.sh \
            || die "failed to create instance"
        ok "instance created"
    fi

    step "Artifact Registry read access for the VM"
    VM_SA="$("${GCLOUD[@]}" compute instances describe "$VM_NAME" --zone "$ZONE" \
        --format='value(serviceAccounts[0].email)')"
    if [ -n "$VM_SA" ]; then
        "${GCLOUD[@]}" projects add-iam-policy-binding "$PROJECT_ID" \
            --member="serviceAccount:${VM_SA}" \
            --role=roles/artifactregistry.reader >/dev/null 2>&1 \
            && ok "granted artifactregistry.reader to $VM_SA" \
            || warn "could not bind artifactregistry.reader (may already be present)"
    fi
fi

if [ "$DO_APP" -eq 0 ]; then
    step "Done (--infra-only)"
    exit 0
fi

# ----------------------------------------------------------- build and push

step "Building image for linux/amd64"
info "GCE runs x86_64; an Apple Silicon build must be cross-compiled or it will not start."
"${GCLOUD[@]}" auth configure-docker "${REGION}-docker.pkg.dev" >/dev/null 2>&1 \
    || die "failed to configure docker credentials for Artifact Registry"

# A named builder gives a persistent cache across deploys; ignore failure if it exists.
docker buildx create --name news-fanout-builder --use >/dev/null 2>&1 || \
    docker buildx use news-fanout-builder >/dev/null 2>&1 || true

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
docker buildx build \
    --platform linux/amd64 \
    --tag "$IMAGE_URI" \
    --tag "${IMAGE_REPO}:${GIT_SHA}" \
    --cache-from "type=registry,ref=${IMAGE_REPO}:buildcache" \
    --cache-to "type=registry,ref=${IMAGE_REPO}:buildcache,mode=max" \
    --push . \
    || die "image build/push failed"
ok "pushed $IMAGE_URI (also tagged $GIT_SHA)"

# ------------------------------------------------------------- wait for VM

step "Waiting for the VM to finish provisioning"
SSH=("${GCLOUD[@]}" compute ssh "$VM_NAME" --zone "$ZONE")

for attempt in $(seq 1 40); do
    if "${SSH[@]}" --command="test -f /var/lib/news-fanout-startup-done" >/dev/null 2>&1; then
        ok "docker is installed and the VM is ready"
        break
    fi
    [ "$attempt" -eq 40 ] && die "VM did not become ready. Inspect: gcloud compute ssh $VM_NAME --zone $ZONE --command 'sudo cat /var/log/news-fanout-startup.log'"
    [ "$attempt" -eq 1 ] && info "first boot installs Docker; this can take ~60-90s"
    sleep 10
done

# ------------------------------------------------------------------- deploy

step "Uploading the compose stack"
TMP_ENV="$(mktemp)"
trap 'rm -f "$TMP_ENV"' EXIT
cat >"$TMP_ENV" <<EOF
APP_IMAGE=${IMAGE_URI}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
EOF

"${SSH[@]}" --command="sudo install -d -o \$(id -u) -g \$(id -g) ${REMOTE_DIR}" >/dev/null \
    || die "could not prepare ${REMOTE_DIR} on the VM"
"${GCLOUD[@]}" compute scp docker-compose.prod.yml "$TMP_ENV" \
    "${VM_NAME}:${REMOTE_DIR}/" --zone "$ZONE" >/dev/null \
    || die "failed to copy files to the VM"
"${SSH[@]}" --command="mv ${REMOTE_DIR}/$(basename "$TMP_ENV") ${REMOTE_DIR}/.env && chmod 600 ${REMOTE_DIR}/.env" >/dev/null
ok "compose file and .env in place at ${REMOTE_DIR}"

step "Starting the stack"
# `docker login` via the VM service account token avoids installing a credential helper.
"${SSH[@]}" --command="
    set -e
    cd ${REMOTE_DIR}
    gcloud auth print-access-token 2>/dev/null \
      | sudo docker login -u oauth2accesstoken --password-stdin https://${REGION}-docker.pkg.dev >/dev/null
    sudo docker compose -f docker-compose.prod.yml pull --quiet
    sudo docker compose -f docker-compose.prod.yml up -d --remove-orphans
    sudo docker image prune -f >/dev/null
" || die "failed to start the stack on the VM"
ok "containers started"

# ------------------------------------------------------------------- verify

EXTERNAL_IP="$("${GCLOUD[@]}" compute instances describe "$VM_NAME" --zone "$ZONE" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
BASE_URL="http://${EXTERNAL_IP}"

step "Waiting for the service to report ready"
READY=0
for attempt in $(seq 1 40); do
    if curl -fsS --max-time 5 "${BASE_URL}/readyz" 2>/dev/null | grep -q '"ready":true'; then
        READY=1
        ok "${BASE_URL}/readyz is ready"
        break
    fi
    sleep 5
done

if [ "$READY" -eq 0 ]; then
    warn "service did not become ready in ~200s"
    info "logs: gcloud compute ssh $VM_NAME --zone $ZONE --command 'sudo docker compose -f ${REMOTE_DIR}/docker-compose.prod.yml logs --tail=80 app'"
    exit 1
fi

if [ "$DO_TEST" -eq 1 ]; then
    step "Running the end-to-end test against the deployment"
    if python3 scripts/e2e_test.py --base-url "$BASE_URL"; then
        ok "end-to-end test passed"
    else
        warn "end-to-end test reported failures (the service is up; see output above)"
        exit 1
    fi
fi

step "Deployed"
cat <<EOF

    URL       ${BASE_URL}
    health    ${BASE_URL}/readyz
    docs      ${BASE_URL}/docs
    stats     ${BASE_URL}/internal/stats
    metrics   ${BASE_URL}/metrics

    logs      gcloud compute ssh ${VM_NAME} --zone ${ZONE} --project ${PROJECT_ID} \\
                --command 'sudo docker compose -f ${REMOTE_DIR}/docker-compose.prod.yml logs -f app'
    redeploy  ./deploy/gcp-deploy.sh --app-only
    teardown  ./deploy/gcp-teardown.sh

EOF
