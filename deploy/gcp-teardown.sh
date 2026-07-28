#!/usr/bin/env bash
#
# Remove what gcp-deploy.sh created, so the deployment stops costing money.
#
#   ./deploy/gcp-teardown.sh                 delete VM, firewall rule and image repo
#   ./deploy/gcp-teardown.sh --vm-only       delete just the VM (keep images)
#   ./deploy/gcp-teardown.sh --delete-project delete the whole project
#
# Deleting the VM destroys the Postgres volume with it. Nothing here is recoverable,
# so each target is listed and confirmed before anything is removed.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

[ -f deploy/config.env ] && . deploy/config.env

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-europe-west4}"
ZONE="${ZONE:-${REGION}-a}"
VM_NAME="${VM_NAME:-news-fanout-vm}"
AR_REPO="${AR_REPO:-news-fanout}"
FIREWALL_RULE="allow-news-fanout-http"

VM_ONLY=0
DELETE_PROJECT=0
for arg in "$@"; do
    case "$arg" in
        --vm-only) VM_ONLY=1 ;;
        --delete-project) DELETE_PROJECT=1 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "$PROJECT_ID" ] || die "PROJECT_ID is not set (deploy/config.env)."
GCLOUD=(gcloud --project "$PROJECT_ID" --quiet)

# List what will go before touching anything.
step "About to delete from project $PROJECT_ID"
if [ "$DELETE_PROJECT" -eq 1 ]; then
    info "the ENTIRE project $PROJECT_ID and everything in it"
else
    info "VM            $VM_NAME (zone $ZONE) — including its Postgres data volume"
    if [ "$VM_ONLY" -eq 0 ]; then
        info "firewall rule $FIREWALL_RULE"
        info "image repo    $AR_REPO (region $REGION) — all pushed images"
    fi
fi

printf '\n    Type "yes" to confirm: '
read -r CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "    aborted"; exit 1; }

if [ "$DELETE_PROJECT" -eq 1 ]; then
    step "Deleting project"
    gcloud projects delete "$PROJECT_ID" --quiet
    ok "project $PROJECT_ID scheduled for deletion (recoverable for ~30 days)"
    exit 0
fi

step "Deleting the VM"
if "${GCLOUD[@]}" compute instances describe "$VM_NAME" --zone "$ZONE" >/dev/null 2>&1; then
    "${GCLOUD[@]}" compute instances delete "$VM_NAME" --zone "$ZONE"
    ok "instance $VM_NAME deleted"
else
    info "instance $VM_NAME not found, skipping"
fi

if [ "$VM_ONLY" -eq 0 ]; then
    step "Deleting the firewall rule"
    if "${GCLOUD[@]}" compute firewall-rules describe "$FIREWALL_RULE" >/dev/null 2>&1; then
        "${GCLOUD[@]}" compute firewall-rules delete "$FIREWALL_RULE"
        ok "rule $FIREWALL_RULE deleted"
    else
        info "rule $FIREWALL_RULE not found, skipping"
    fi

    step "Deleting the image repository"
    if "${GCLOUD[@]}" artifacts repositories describe "$AR_REPO" --location "$REGION" >/dev/null 2>&1; then
        "${GCLOUD[@]}" artifacts repositories delete "$AR_REPO" --location "$REGION"
        ok "repository $AR_REPO deleted"
    else
        info "repository $AR_REPO not found, skipping"
    fi
fi

step "Teardown complete"
info "the project itself remains; delete it with --delete-project"
