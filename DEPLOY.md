# Deploying news-fanout to GCP

Target: a **single Compute Engine VM running `docker compose`** with three containers — the
app (API + all background workers), Postgres 16 and Redis 7.

Chosen because it is the fastest path from "I have a GCP account" to a reachable URL,
it needs nothing beyond Docker, and it costs ~$15/month. The trade-offs and the
managed alternative are in [Appendix B](#appendix-b--when-to-move-off-the-single-vm).

```
                      GCE VM  (e2-small, Debian 12, docker compose)
   you ──http:80──►  ┌──────────────────────────────────────────────┐
                     │  app        news-fanout:latest   :8000       │
                     │             API + ingest + classifier + push │
                     │  postgres   postgres:16-alpine   (volume)    │
                     │  redis      redis:7-alpine       (push dedup)│
                     └──────────────────────────────────────────────┘
                                     ▲
                     image pulled from Artifact Registry
                                     ▲
                     built on your Mac for linux/amd64 (OrbStack)
```

---

## The short version

```bash
cp deploy/config.env.example deploy/config.env
$EDITOR deploy/config.env          # set PROJECT_ID to something globally unique
./deploy/gcp-deploy.sh
```

That single script performs every step in Phase 1–4 below, is safe to re-run, and
finishes by running the end-to-end test against the live URL. **Phases 1–4 exist so
you know what it did** and can do it by hand or debug a failure.

| Command | What it does |
|---|---|
| `./deploy/gcp-deploy.sh` | Full deploy or redeploy (infra + image + app + test) |
| `./deploy/gcp-deploy.sh --app-only` | Rebuild, push, restart app. Skips infra checks — the everyday redeploy |
| `./deploy/gcp-deploy.sh --infra-only` | Create the project/VM/registry, deploy nothing |
| `./deploy/gcp-deploy.sh --no-test` | Deploy without the post-deploy test |
| `./deploy/gcp-teardown.sh` | Delete VM + firewall + images |
| `./deploy/gcp-teardown.sh --delete-project` | Delete the whole project |

---

## Phase 0 — Verify locally first (2 min)

Never debug the cloud and the app at the same time. Confirm the stack is healthy on
your machine before deploying it.

```bash
docker compose up --build -d
python3 scripts/e2e_test.py
```

Expect `14/14 checks passed`. Then `docker compose down` (add `-v` to drop the
Postgres volume too).

---

## Phase 1 — Project, billing, APIs (5 min, once)

**1.1 Authenticate.** Skip if `gcloud auth list` already shows your account.

```bash
gcloud auth login
```

**1.2 Create the project.** Project IDs are globally unique, so add a suffix if the
name is taken.

```bash
export PROJECT_ID=news-fanout-demo
gcloud projects create "$PROJECT_ID" --name=news-fanout
gcloud config set project "$PROJECT_ID"
```

**1.3 Link billing.** Compute Engine will not start without it. Free-tier credits
still apply — linking an account does not by itself charge you.

```bash
gcloud billing accounts list                     # copy the ACCOUNT_ID
gcloud billing projects link "$PROJECT_ID" --billing-account=017D09-388CDF-100DBB
```

**1.4 Enable the two APIs you need.** First call takes ~60s.

```bash
gcloud services enable compute.googleapis.com artifactregistry.googleapis.com
```

> Nothing else needs enabling. There is no Cloud SQL, no Memorystore, no VPC
> connector, no Cloud Build — that is the point of this topology.

---

## Phase 2 — Build and push the image (3 min)

**2.1 Create the Artifact Registry repository.** Keep it in the same region as the VM
so pulls stay on Google's network and free of egress cost.

```bash
export REGION=europe-west4
gcloud artifacts repositories create news-fanout \
    --repository-format=docker --location="$REGION"
```

**2.2 Let your local Docker authenticate to it.**

```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev"
```

**2.3 Build for `linux/amd64` and push.**

> **This is the one step that reliably bites.** Your Mac is arm64; the VM is x86_64.
> A plain `docker build` produces an arm64 image that fails on the VM with
> `exec format error`. `--platform linux/amd64` is mandatory, and `buildx` is what
> makes the cross-build work.

```bash
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/news-fanout/news-fanout:latest"
docker buildx build --platform linux/amd64 --tag "$IMAGE" --push .
```

---

## Phase 3 — Create the VM (3 min, once)

**3.1 Open port 80** to tagged instances only.

```bash
gcloud compute firewall-rules create allow-news-fanout-http \
    --allow=tcp:80 --source-ranges=0.0.0.0/0 --target-tags=news-fanout
```

For a private demo, narrow the range instead:
`--source-ranges="$(curl -s ifconfig.me)/32"`.

**3.2 Create the instance.** `deploy/vm-startup.sh` runs as root on boot and installs
Docker plus the compose plugin. `--scopes=cloud-platform` is what lets the VM mint an
access token to pull from Artifact Registry without a key file.

```bash
export ZONE=${REGION}-a
gcloud compute instances create news-fanout-vm \
    --zone="$ZONE" \
    --machine-type=e2-small \
    --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=20GB --boot-disk-type=pd-balanced \
    --tags=news-fanout \
    --scopes=cloud-platform \
    --metadata-from-file=startup-script=deploy/vm-startup.sh
```

**3.3 Grant the VM read access to the registry.**

```bash
VM_SA=$(gcloud compute instances describe news-fanout-vm --zone "$ZONE" \
        --format='value(serviceAccounts[0].email)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${VM_SA}" --role=roles/artifactregistry.reader
```

**3.4 Wait for Docker to finish installing** (~60–90s on first boot):

```bash
until gcloud compute ssh news-fanout-vm --zone "$ZONE" \
      --command='test -f /var/lib/news-fanout-startup-done' 2>/dev/null; do sleep 10; done
```

If it never appears, read the startup log:

```bash
gcloud compute ssh news-fanout-vm --zone "$ZONE" \
    --command='sudo cat /var/log/news-fanout-startup.log'
```

---

## Phase 4 — Run the stack (2 min)

**4.1 Upload the compose file and its environment.** `docker-compose.prod.yml` reads
`APP_IMAGE` and `POSTGRES_PASSWORD` from a sibling `.env`, which never enters git.

```bash
gcloud compute ssh news-fanout-vm --zone "$ZONE" \
    --command='sudo install -d -o $(id -u) -g $(id -g) /opt/news-fanout'

printf 'APP_IMAGE=%s\nPOSTGRES_PASSWORD=%s\n' \
    "$IMAGE" "$(openssl rand -base64 24 | tr -d /=+)" > /tmp/nf.env

gcloud compute scp docker-compose.prod.yml /tmp/nf.env \
    news-fanout-vm:/opt/news-fanout/ --zone "$ZONE"
gcloud compute ssh news-fanout-vm --zone "$ZONE" \
    --command='mv /opt/news-fanout/nf.env /opt/news-fanout/.env && chmod 600 /opt/news-fanout/.env'
```

**4.2 Log in to the registry from the VM and start everything.**

```bash
gcloud compute ssh news-fanout-vm --zone "$ZONE" --command="
    cd /opt/news-fanout
    gcloud auth print-access-token \
      | sudo docker login -u oauth2accesstoken --password-stdin https://${REGION}-docker.pkg.dev
    sudo docker compose -f docker-compose.prod.yml pull
    sudo docker compose -f docker-compose.prod.yml up -d
"
```

The app applies `schema.sql` itself on startup (idempotent, advisory-locked), so there
is no separate migration step. To run it explicitly anyway:
`sudo docker compose -f docker-compose.prod.yml run --rm app migrate`.

**4.3 Verify.**

```bash
IP=$(gcloud compute instances describe news-fanout-vm --zone "$ZONE" \
     --format='value(networkInterfaces[0].accessConfigs[0].natIP)')

curl "http://${IP}/readyz"                          # {"ready":true,...}
python3 scripts/e2e_test.py --base-url "http://${IP}"
```

---

## Phase 5 — Operate

```bash
SSH="gcloud compute ssh news-fanout-vm --zone $ZONE --command"
COMPOSE="sudo docker compose -f /opt/news-fanout/docker-compose.prod.yml"

$SSH "$COMPOSE ps"                    # container status
$SSH "$COMPOSE logs -f app"           # follow app logs
$SSH "$COMPOSE restart app"           # restart just the app
$SSH "docker stats --no-stream"       # CPU / memory
curl "http://${IP}/internal/stats"    # pipeline counters
```

**Redeploying after a code change** is one command — it rebuilds, pushes, restarts the
app container and re-runs the test. Postgres data survives:

```bash
./deploy/gcp-deploy.sh --app-only
```

**Back up the database:**

```bash
$SSH "$COMPOSE exec -T postgres pg_dump -U postgres news_fanout | gzip" > backup.sql.gz
```

**Stop paying without destroying anything** — a stopped VM bills only for its disk
(~$2/month):

```bash
gcloud compute instances stop news-fanout-vm --zone "$ZONE"
gcloud compute instances start news-fanout-vm --zone "$ZONE"   # compose restarts itself
```

---

## Cost

| Item | Monthly |
|---|---|
| e2-small (2 vCPU burst, 2 GB) | ~$13 |
| 20 GB pd-balanced | ~$2 |
| Artifact Registry (<1 GB) | ~$0.10 |
| Egress (demo traffic) | ~$0 |
| **Total** | **~$15** |

An `e2-micro` in a US region falls under the always-free tier, but 1 GB of RAM for
app + Postgres + Redis is tight. `e2-small` is the honest floor.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `exec format error` in app logs | arm64 image on an x86 VM. Rebuild with `--platform linux/amd64` (Phase 2.3). |
| `denied` / `unauthorized` on `docker compose pull` | VM service account lacks `artifactregistry.reader` (3.3), or the VM token expired — re-run the `docker login` in 4.2. |
| `/readyz` returns `{"ready":false,"database":false}` | Postgres is still starting or the password disagrees. `$SSH "$COMPOSE logs postgres"`. |
| `/readyz` returns `"redis":false` | Redis container is down: `$SSH "$COMPOSE ps redis"`. |
| Connection times out on port 80 | Firewall rule missing, or the VM lacks the `news-fanout` tag: `gcloud compute instances describe news-fanout-vm --zone $ZONE --format='value(tags.items)'`. |
| `docker: command not found` over SSH | Startup script still running. Wait for `/var/lib/news-fanout-startup-done` (3.4). |
| Test passes locally, fails on the VM | Compare `curl http://$IP/internal/stats` against local. `classify_jobs.failed > 0` points at the classifier, not the deploy. |
| Disk filling up | The stub source is capped by `NEWS_FANOUT_INGEST__STUB_MAX_PAGE_ID` (2000 in prod compose). Lower it, or `docker system prune -af`. |

---

## Appendix A — Security before this faces real traffic

This deploys a demo. The gaps, in the order they matter:

1. **No authentication.** `X-User-Id` is a trusted header — anyone can send any user
   ID and read that user's feed. Real deployment needs a token the server validates.
2. **`/internal/stats` and `/metrics` are public.** Set
   `NEWS_FANOUT_SERVER__EXPOSE_INTERNAL_STATS=false`, or put both behind a reverse
   proxy that allowlists your IP.
3. **HTTP, not HTTPS.** Add Caddy as a fourth container for automatic Let's Encrypt
   certificates, or front the VM with a Google HTTPS load balancer.
4. **Postgres password in a file on the VM.** Fine for a demo; move it to Secret
   Manager for anything real.
5. **Port 80 open to `0.0.0.0/0`** by default. Narrow `ALLOW_CIDR` in
   `deploy/config.env`.

## Appendix B — When to move off the single VM

This topology is the right answer for a demo and the wrong answer for the 1M
articles/day the design targets. What breaks first, and what replaces it:

| Limit hit | Move to |
|---|---|
| The VM is a single point of failure; restarts drop traffic | Managed instance group behind a load balancer, or Cloud Run for the API role |
| Postgres on the same box as the app competes for CPU and has no PITR | Cloud SQL for Postgres (`NEWS_FANOUT_DATABASE__HOST` is all that changes) |
| Redis is unreplicated; a restart loses dedup markers | Memorystore for Redis |
| Workers and API cannot scale independently | Split by role — the `NEWS_FANOUT_ROLE` env var (`api`, `ingest`, `classifier`, `push`) already runs each as its own service |
| Postgres `articles_by_topic` becomes the bottleneck | This is where the design's Cassandra reasoning applies — the SQL schema is the demo's stand-in |

Because the roles are already separated by config, the migration is deployment work,
not a rewrite. Note the one caveat: `NEWS_FANOUT_SERVER__AUTO_MIGRATE` should be
turned off and `news-fanout migrate` run as a discrete step once more than one
replica exists.
