# Deploying to Fly.io

Fly runs the Flask app as a long-lived Docker container with a persistent volume for user submissions and uploaded datasets. Free tier covers 3 shared-CPU 256 MB VMs; this app needs 1 GB RAM to handle the 7k-row dataset, so expect ~$2–3/month if you keep it warm.

## Prerequisites

1. Install `flyctl`: https://fly.io/docs/hands-on/install-flyctl/
   ```bash
   # Windows (PowerShell)
   iwr https://fly.io/install.ps1 -useb | iex
   # macOS / Linux
   curl -L https://fly.io/install.sh | sh
   ```
2. Create an account + log in:
   ```bash
   fly auth signup   # or: fly auth login
   ```
3. Install Docker Desktop (optional — Fly can build remotely, but local builds are faster).

## One-time setup

Run from the project root (`c:\Users\awabeltarabilly\Downloads\the hidden variable\`):

```bash
# 1. Pick a unique app name + region.
fly apps create the-hidden-variable   # or let `fly launch` generate one

# 2. Edit fly.toml: set `app = "..."` to the name you chose.
#    Set primary_region to your closest (fra, iad, lhr, sjc, syd, nrt).

# 3. Create the persistent volume. 1 GB is plenty.
fly volumes create hv_data --size 1 --region fra

# 4. Deploy.
fly deploy
```

`fly deploy` will:
- Build the Docker image (uses `Dockerfile` + `.dockerignore`).
- Push it to Fly's registry.
- Boot a VM, mount `/data`, start gunicorn on port 8080.
- Return a URL like `https://the-hidden-variable.fly.dev`.

## Re-deploying after changes

```bash
fly deploy
```

## Inspecting

```bash
fly logs                # live log stream
fly ssh console         # shell into the running VM
fly status              # machines / regions / health
fly apps open           # opens the deployed URL in your browser
```

## Scaling / cost knobs

- **Keep it warm** (no cold starts): set `min_machines_running = 1` in `fly.toml`. Adds ~$2/mo.
- **More RAM** if the dataset grows beyond 10k rows: change `memory = "1gb"` → `"2gb"` in `fly.toml`.
- **Extra regions**: `fly regions add iad` after deploy. Fly will route users to the nearest one. (Each region needs its own volume — `fly volumes create hv_data --region iad --size 1`.)

## Gotchas specific to this app

- **One worker only.** `STATE`, `RECORDS`, `PREDICTIONS` are module globals. `gunicorn --workers 1 --threads 4 --preload` is hard-coded in the Dockerfile. Don't bump workers without refactoring state into Redis or similar.
- **Cold start cost.** On boot `_prime_state()` runs the default DBSCAN fit (~2 s Giza, ~6 s Elephantine). The healthcheck grace period (`30s`) covers this.
- **Volume is per-region.** If you scale to multiple regions, each one gets its own disk — uploads and submissions won't cross-replicate. Use Fly's Tigris/LiteFS if you need that.
- **Uploads live on the volume.** `DATA_DIR=/data` puts user-uploaded CSVs into `/data/other datasets/` on the volume. Bundled sample datasets stay in the image's `/app/other datasets/`.

## Local Docker test (optional)

Sanity-check the image before deploying:

```bash
docker build -t hidden-variable .
docker run --rm -p 8080:8080 -v hv_data:/data hidden-variable
# open http://127.0.0.1:8080
```

## Rollback

```bash
fly releases              # list past deploys
fly releases rollback <N> # revert to release N
```
