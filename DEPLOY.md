# Deploying to the VM

Written for whoever is at the SSH prompt. Every command is meant to be pasted as-is; where a
value has to be chosen, the command generates it rather than asking you to invent one.

The stack is eight containers off one image: Postgres, MinIO, the API, and one worker per
pipeline stage. Postgres and MinIO are **not** published to the host's public interface — the
only port reachable from outside is the API on **85**, because A-PAG's office network permits
outbound traffic to this host on that port only.

---

## Before you start: the machine has to be big enough

Measured on a 10-core development machine with the full stack running:

| container | memory |
|---|---|
| embedding-worker | 1,285 MB |
| extraction-worker (OCR loaded) | ~858 MB |
| api (embedding + reranker models resident) | ~560 MB |
| chunking-worker (loads the embedding model to tokenise) | 244 MB |
| postgres, minio, scan, normalize | ~300 MB |
| **peak concurrent** | **≈ 3.1–4.2 GB** |

**8 GB RAM is the floor, whatever the user count.** The models load once per process, not per
person, so two testers and fifty staff need the same memory. A 4 GB VM sits exactly on the line
above and will OOM-kill a worker.

vCPU is what scales with use, and only loosely — two people rarely ask at the same moment.

| | vCPU | RAM | per month | questions take |
|---|---|---|---|---|
| **Testing, 2 people** | **`B2s_v2`** 2 | 8 GB | ~₹2,200 | ~4s with the tuning below |
| Rollout, ~50 people | `B4ls_v2` 4 | 8 GB | ~₹3,200 | ~2.5s |

On a **2-vCPU box set `RERANK_CANDIDATES=25`** in `.env`. Reranking is the only CPU cost in the
request path and it is linear in candidates, so halving them halves the wait — about 4s instead
of 8 — while keeping most of the accuracy reranking buys. Set it back to 50 on 4 vCPU.

Expect the **first bulk ingest to be slow** on 2 vCPU: OCR runs at roughly 12s a page, so a
162-page scanned PDF takes about half an hour. It runs in the background and the system stays
usable — start it and leave it.

Testing only: **deallocate when nobody is using it.** Azure bills compute by the hour, so eight
hours a day on weekdays is nearer ₹500/month than ₹2,200. The disk keeps charging (~₹300 for
64 GB) and the data is untouched.

```bash
az vm deallocate --resource-group <group> --name <vm>   # stops the compute charge
az vm start --resource-group <group> --name <vm>        # back in about a minute
```

**The original sizing below assumed a rollout.** The original 3.8 GB / 2 vCPU VM does not fit this — three
workers alone are capped at 1,200 MB each — and on 2 vCPU reranking rises from ~2.2s to roughly
6–10s per question while OCR rises from 3.5s to about 12s per page.

On Azure that is a **B2ms** (8 GB / 2 vCPU) at minimum, **B4ms** (16 GB / 4 vCPU) comfortably.
Resize with the VM stopped:

```bash
# from your own machine, with the Azure CLI
az vm deallocate --resource-group <group> --name <vm>
az vm resize --resource-group <group> --name <vm> --size Standard_B4ms
az vm start --resource-group <group> --name <vm>
```

Confirm once you are back on the box:

```bash
free -h            # "total" should be 8Gi or more
nproc              # 4 or more
df -h /            # at least 20GB free: images are ~2.5GB and documents accumulate
```

---

## 1. Docker

```bash
docker --version && docker compose version
```

If either is missing:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker          # or log out and back in
```

---

## 2. Get the code

```bash
cd ~
git clone https://github.com/AirPollutinActionGroup/A-PAG-AI-KNOWLEDGE-BASE.git apag
cd apag
git checkout main_ci          # or main, once the stack has been merged there
```

Already cloned from a previous deploy:

```bash
cd ~/apag && git fetch origin && git checkout main_ci && git pull
```

---

## 3. Write the `.env`

**This file is the only place secrets live. It is gitignored. It is not in the repository and
must never be committed.**

Generate the three credentials rather than choosing them — a password someone typed is a
password someone can guess:

```bash
cd ~/apag
cat > .env <<EOF
POSTGRES_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)
MINIO_ACCESS_KEY=$(openssl rand -hex 12)
MINIO_SECRET_KEY=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)
JWT_SECRET_KEY=$(openssl rand -hex 32)

SARVAM_API_KEY=
SARVAM_MODEL=sarvam-105b-conversations

RERANK_ENABLED=true
# On a 2-vCPU VM. Reranking is linear in candidates, so 25 halves the wait at a small cost in
# accuracy. Raise to 50 on 4 vCPU or more.
RERANK_CANDIDATES=25
OCR_ENABLED=true
OCR_THREADS=0
EOF
chmod 600 .env
```

Then paste the Sarvam key in:

```bash
nano .env          # fill SARVAM_API_KEY=sk-...
```

**Without a Sarvam key the stack still runs.** Search works, documents ingest, and `/ask`
returns 503 while the UI falls back to showing passages. That is deliberate: a missing key is a
reduced service, not a broken one.

`JWT_SECRET_KEY` is what signs login tokens. Changing it later logs everyone out — which is
also how you revoke every session at once if you ever need to.

---

## 4. Bring it up

The first build downloads the base image and bakes in the embedding, reranker and OCR models.
Expect **10–20 minutes** and do not interrupt it.

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Migrations are not a separate step: a one-shot `migrate` service runs `alembic upgrade head` and
exits, and the API and workers refuse to start until it has completed successfully.

Watch it settle:

```bash
docker compose -f docker-compose.prod.yml ps
```

Every row should read `running` or `healthy`, except `apag-migrate` which should read `exited
(0)`. That exit code is success.

---

## 5. Verify, in this order

Each check assumes the one before it passed.

```bash
# 1. the API is up
curl -s localhost:85/health
# {"status":"healthy","service":"apag-ai-knowledge-base"}

# 2. the schema is at the latest migration
docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -U postgres -d apag_knowledge_base -c "SELECT version_num FROM alembic_version;"

# 3. pg_search is loaded — BM25 is half of retrieval and fails loudly without it
docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -U postgres -d apag_knowledge_base -c "SELECT extname FROM pg_extension;"
# expect both 'vector' and 'pg_search'

# 4. the models are baked in, not fetched at runtime
docker compose -f docker-compose.prod.yml logs api | grep -i "fetching"
# expect NO output. Any "Fetching N files" means a model is being downloaded on
# boot, which fails closed on a network blip and is not reproducible.

# 5. every worker is alive
docker compose -f docker-compose.prod.yml ps --format '{{.Name}}\t{{.Status}}'
```

Then make an account and ask something:

```bash
curl -s -X POST localhost:85/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@a-pag.org","password":"<a real password>","full_name":"Your Name"}'

TOK=$(curl -s -X POST localhost:85/api/v1/auth/login \
  -d 'username=you@a-pag.org&password=<a real password>' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s "localhost:85/api/v1/search?q=test&limit=3" -H "Authorization: Bearer $TOK"
```

The first account you create is a **USER**, not an admin — self-registration cannot grant a
role. To make yourself an administrator:

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -U postgres -d apag_knowledge_base \
  -c "UPDATE users SET role='ADMIN' WHERE email='you@a-pag.org';"
```

---

## 6. Load the documents

From your own machine, with the folder to hand:

```bash
# copy the documents up
scp -r "./Thermal Power Plants" <user>@<vm>:~/apag/incoming/

# then on the VM
cd ~/apag
docker compose -f docker-compose.prod.yml exec -T api \
  python bulk_ingest.py /app/incoming --owner you@a-pag.org --classification PUBLIC
```

`--classification` is **required**, with no default. The Data Boundary Gateway reads exactly
that field to decide what may be sent to an external model, so a folder ingested without one
would be eligible to leave the network on a value nobody chose. Use `RESTRICTED` for anything
that must stay inside.

Watch it land:

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -U postgres -d apag_knowledge_base \
  -c "SELECT status, count(*) FROM documents GROUP BY status ORDER BY 2 DESC;"
```

A 162-page scanned PDF takes about 10 minutes to OCR on 4 vCPU. `AWAITING_CLASSIFICATION` or
`CHUNKED` rows that stop moving mean a worker died; see below.

---

## 7. Open it to the office

The API listens on **85**. Allow it inbound:

```bash
# Azure network security group, from your own machine
az network nsg rule create --resource-group <group> --nsg-name <nsg> \
  --name allow-apag-85 --priority 1001 \
  --destination-port-ranges 85 --protocol Tcp --access Allow
```

Then `http://<vm-ip>:85/search` from the office network.

**Postgres and MinIO stay closed.** They are not published to the host at all in this compose
file. To reach them for maintenance, tunnel over SSH rather than opening a port:

```bash
ssh -L 5432:localhost:5432 -L 9001:localhost:9001 <user>@<vm>
```

---

## Running it afterwards

```bash
# deploy a new version
cd ~/apag && git pull && docker compose -f docker-compose.prod.yml up -d --build

# logs
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs --tail 100 extraction-worker

# memory, which is the thing most likely to bite
docker stats --no-stream

# restart one worker
docker compose -f docker-compose.prod.yml restart embedding-worker
```

### Back up before anything risky

The database holds every document record, passage and vector. MinIO holds the files.

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U postgres apag_knowledge_base | gzip > ~/apag-$(date +%F).sql.gz
```

### When something stops moving

**Documents stuck mid-pipeline.** Each stage hands off by inserting a job row, so a document
that came to rest before a stage existed has no job for it:

```bash
docker compose -f docker-compose.prod.yml exec -T api \
  python backfill_jobs.py --stage EMBED --dry-run
```

**A worker exited.** `docker compose ps` shows it; the logs say why. Out-of-memory is the usual
cause and appears as `Killed` with no traceback — check `docker stats` and whether the VM was
actually resized.

**Answers return 503.** Either `SARVAM_API_KEY` is unset or empty, or Sarvam is unreachable.
Retrieval is unaffected; the UI falls back to showing passages.

**Search returns nothing for everything.** Check `pg_search` is in `pg_extension` (step 5.3).
An existing data directory initialised by an older Postgres image will not have
`shared_preload_libraries` set, which is why the compose file passes it as an explicit
`command` flag.
