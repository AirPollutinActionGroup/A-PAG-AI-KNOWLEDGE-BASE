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
request path and it is linear in candidates. Measured on the 100-question evaluation set, twice
each, interleaved: 25 candidates scores 82.0% hit@1 / 95.0% hit@5 against 83.0% / 97.0% at 50,
and is about 1.8x faster. Leave it at 50 on 4 vCPU. (Before this was wired through
`docker-compose.prod.yml` the setting reached no container, so it did nothing.)

**Optional, untested:** `EXTRACTION_CPUS=1.5` in `.env` caps the OCR container so a big scanned PDF cannot take both cores and slow search while it runs. It defaults to no limit. Try it, ask a question during an ingest, and compare the latency with and without before keeping it.

**Set `INFERENCE_THREADS` to the VM's vCPU count (`2` on a B2s_v2).** Left at its default, onnxruntime sizes its thread pool from physical cores, and each Azure vCPU is a hyperthread, so on 2 vCPU the embedding worker used one core at ~100% while the VM sat ~45% idle, with nothing in any log to say so. It applies to the embedding worker and to the API's query embedding and reranking.

**Embedding is the slow, memory-hungry stage.** On 2 vCPU it embeds a full batch of passages in about 80 seconds, and the worker needs ~2.3GB at its peak. The default limit is now 2500m (`EMBEDDING_MEM_LIMIT`), and `EMBEDDING_BATCH_SIZE=16` in `.env` lowers the peak further at some cost in speed. If a limit is set too low Docker does not kill the worker, it swaps it, and ingestion slows to a crawl with nothing in the logs; check `free -m` and the worker's `memory.swap.current` if an ingest is inexplicably slow.

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

## 0. Azure: allow your IP, and fix Docker's packet size

Two things that look like the app being broken and are not. Both bit the first real deploy.

**The firewall rules allow specific source IPs, not everyone.** If `http://<vm-ip>:85/` or SSH
times out while the portal says the VM is Running, your public IP is probably not in the rules.
Find it with `curl https://api.ipify.org`, then add it to both the SSH and port-85 inbound rules
in the VM's Networking page. A timeout means packets are being dropped; "connection refused"
would mean the VM is up and nothing is listening. The login user is **`azureuser`**, not the
VM's name.

**Containers cannot reach the internet at Docker's default MTU on Azure.** The VM itself can
(`curl https://pypi.org` works) while a container's TLS handshake times out, so `pip install`
fails inside `docker build` and the API could never reach Sarvam either. Set a smaller MTU once:

```bash
echo '{"mtu": 1400}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
docker run --rm python:3.12-slim python -c "import urllib.request;print(urllib.request.urlopen('https://pypi.org/simple/fastapi/',timeout=20).status)"   # expect 200
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
COMPOSE_PARALLEL_LIMIT=1 docker compose -f docker-compose.prod.yml up -d --build
```

`COMPOSE_PARALLEL_LIMIT=1` matters on a 2-vCPU box: seven services run the same `pip install`,
and in parallel they compete for bandwidth and time out. One at a time, the later builds reuse
the first one's cached layers. On a **fresh** database volume the first `up` can end with
`apag-postgres is unhealthy` because ParadeDB's first-boot initialisation outlasts the health
check; the database is fine, so run the same command again.

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

**Spreadsheets of operational records are the slow case, not scans.** A 7-sheet complaint log with
one row per record chunked to 1,815 passages and took 19 minutes to embed on 2 vCPU, peaking at
2,537MB. That is why `EMBEDDING_MEM_LIMIT` defaults to `3g`: at the previous `2500m` it fit with
3% to spare, and Docker responds to that limit by swapping rather than failing — so a document
slightly larger would not error, it would simply stop making progress with nothing in any log.
A document sitting at `CHUNKED` with the embedding worker at ~100% CPU is working, not stuck;
check `docker stats` before restarting anything (see `KNOWN_DEBTS.md` #39).

---

## 6b. Google Drive sync (optional)

Instead of copying files up by hand, point the deployment at one shared Drive folder. Staff drop
documents into it, and the **subfolder decides the tier** — so nobody has to remember to pick a
classification, and a file filed anywhere else is skipped rather than given a tier nobody chose.

Everything imported this way goes through `UploadService.receive()`, the same entry point as the
upload form. Scan, OCR, quality gate, chunking, embedding, audit and the Data Boundary Gateway
all apply unchanged. There is no second set of rules.

### In Google, once

1. **Google Cloud Console → APIs & Services → Library → Google Drive API → Enable.**
2. **IAM & Admin → Service Accounts → Create.** Name it `apag-drive-sync`; skip the role step —
   it needs no project role, only Drive sharing.
3. **Keys → Add key → JSON.** This downloads the key. It is a credential: never commit it, never
   email it, never put it in the image.
4. Copy the service account's email (`…@….iam.gserviceaccount.com`).
5. In Drive, create **A-PAG Knowledge Base** with two subfolders, **Public** and **Restricted**.
6. Share the **top-level** folder with that email as **Viewer** — read-only is deliberate, so the
   connector can never alter or delete anyone's files.
7. Open each subfolder and copy its ID from the URL, the part after `/folders/`.

An API key will not work: API keys only reach publicly shared files, and these folders must never
be public.

### On the VM

```bash
# the key, readable only by you, outside the repo
mkdir -p ~/apag/secrets
# paste the JSON into ~/apag/secrets/drive-sync.json, then:
chmod 600 ~/apag/secrets/drive-sync.json
```

Add to `~/apag/.env`:

```bash
GDRIVE_ENABLED=true
GDRIVE_CREDENTIALS_FILE=/app/secrets/drive-sync.json
GDRIVE_PUBLIC_FOLDER_ID=<id of the Public subfolder>
GDRIVE_RESTRICTED_FOLDER_ID=<id of the Restricted subfolder>
GDRIVE_FALLBACK_OWNER_EMAIL=you@a-pag.org
```

`GDRIVE_FALLBACK_OWNER_EMAIL` **must be a registered user**. `RESTRICTED` is owner-scoped, so a
document whose Drive owner has no account here needs someone to belong to — and a RESTRICTED
document with no owner is invisible to everybody except admins.

The folder IDs are **IDs, not names**, on purpose. A name can be renamed, duplicated, or shadowed
by a subfolder somebody calls "Public"; matching on one would silently reclassify everything
beneath it.

### Run it

```bash
cd ~/apag
# see what it would do, touching nothing
docker compose -f docker-compose.prod.yml exec -T api python drive_sync.py --dry-run

# a small first pass is worth it, especially on a burstable VM
docker compose -f docker-compose.prod.yml exec -T api python drive_sync.py --limit 5

# then the folder
docker compose -f docker-compose.prod.yml exec -T api python drive_sync.py
```

Re-running is safe and cheap: unchanged files are recognised by Drive's checksum (or, for Google
Docs, Sheets and Slides, which have none, by their modified time) and are never downloaded twice.
Two runs cannot overlap — a database lock makes the second one stop and say so.

### Let it run every night

The `drive-sync` container does the same thing on its own, **daily at 00:00 India time**. It is
already in `docker-compose.prod.yml` and idles quietly until `GDRIVE_ENABLED=true`:

```bash
docker compose -f docker-compose.prod.yml up -d drive-sync
docker compose -f docker-compose.prod.yml logs drive-sync | grep "Next Drive sync"
# Next Drive sync at 2026-10-09T00:00:00+05:30
```

- **It is a time of day, not an interval.** "Every 24 hours" would be anchored to whenever the
  container last started, so a restart at 4pm would move the sync to 4pm for good.
- **A missed night is caught up.** If the VM was off at midnight, the sync runs as soon as the
  container is back, then returns to midnight.
- **A failure retries within the hour** rather than waiting for the next night.
- **Midnight is deliberate on a B-series VM**: it earns CPU credits while idle and spends them
  when nobody is asking questions.

Change the time with `GDRIVE_SYNC_AT=02:30`, or the zone with `GDRIVE_SYNC_TIMEZONE`. The
result of the last run is in the database, readable without the logs:

```bash
docker compose -f docker-compose.prod.yml exec -T postgres psql -U postgres -d apag_knowledge_base \
  -c "SELECT key, value FROM drive_sync_state;"
```

A document added during the day is searchable the morning after. For anything urgent, run
`drive_sync.py` by hand as above.

### What it does, stated plainly

| In Drive | In the knowledge base |
|---|---|
| File added to `Public` / `Restricted` | Imported at that tier |
| File edited | Imported as a new version. The previous version is retired — hidden, not erased — **only once the new one is searchable**, so the file is never left with nothing to find |
| File moved between `Public` and `Restricted` | The same document changes tier. Nothing is re-imported, and the audit log records that a folder move did it |
| File binned, deleted, or moved out | Hidden from search and answers, reversibly. Bytes and audit trail kept |
| File put back, unedited | **The same document comes back** — not a copy |
| Google Doc / Sheet / Slides | Exported to docx / xlsx / pptx, which keeps headings and tables |
| File elsewhere in Drive | Ignored. The service account cannot see it |
| File added as a **shortcut** | Skipped — a shortcut is a pointer, with no content. Ask people to *Move*, not *Add shortcut* |
| Document deleted here | **Nothing.** The connector never writes to Drive |

**Anyone with edit access to the folder can change a document's tier by moving it.** That is
inherent to "the folder is the tier", and every move is audited. Moving a file into `Public`
widens who can read it and makes it eligible to be sent to the external model, which is why the
report marks those moves.

### Reading the report

Some outcomes are only known a run later. Importing a file only puts it in quarantine; whether it
was a duplicate, or failed to extract, is decided by the workers afterwards. So each run ends with
a section headed *From earlier syncs*, reporting on what previous runs imported:

- **PREVIOUS VERSION RETIRED** — an edit is now live, and the version before it was hidden.
- **CAME OUT A DUPLICATE** — identical content was already in the knowledge base.
- **TIER RAISED** — that duplicate was filed more restricted than the copy already held, so the
  held document was raised to RESTRICTED. Reverse with `POST /api/v1/documents/{id}/classify` if
  a file was filed in the wrong folder.
- **COULD NOT BE MADE SEARCHABLE** — the pipeline rejected it. If it was an edit, the previous
  version stays visible.

And two reported the moment they happen:

- **TIER CHANGED by moving between folders** — with moves into `Public` marked.
- **OWNER FELL BACK** — the Drive owner has no account here, so the fallback account owns the
  document. For a RESTRICTED file that means only that account and admins will see it.

### If nothing is found

Almost always the folder not being shared with the service account, which from here is
indistinguishable from an empty folder. Check the address the command prints at startup against
the folder's sharing list. Failing that, confirm the Drive API is enabled on the project.

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
