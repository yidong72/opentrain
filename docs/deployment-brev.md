# Open Train on Brev + Cloudflare

This overlay works on Brev or another Linux Docker host. You need Git, Docker Engine with Compose, `cloudflared`, and management access to a Cloudflare DNS zone.

Use a separate tunnel for each independent application. Replicas of one tunnel can receive traffic for that tunnel's routes. Different subdomains can point to different tunnels on different machines.

## First-time setup

Connect to your host (for example `brev shell open-train`), then:

```bash
git clone https://github.com/yidong72/opentrain.git
cd opentrain
cp deploy/brev.env.example .env
cp deploy/cloudflared.example.yml deploy/cloudflared.yml
mkdir -p secrets
chmod 700 secrets
chmod 600 .env
```

On a trusted management machine with `cloudflared`, create a new tunnel (choose a unique name if `open-train` already exists):

```bash
cloudflared tunnel login
cloudflared tunnel create open-train
cloudflared tunnel route dns YOUR_TUNNEL_UUID opentrain.example.com
```

Substitute your tunnel UUID and chosen hostname. Do not overwrite an existing DNS record without checking what it serves. Securely copy only the newly created tunnel's credential JSON, whose location is reported by the create command, to `secrets/cloudflared.json` on the deployment host. The account-management `cert.pem` stays on the trusted management machine; the connector does not need it.

Replace the UUID and hostname placeholders in `deploy/cloudflared.yml`, and configure `.env` as described below. Keep the container credential path and ingress service unchanged. Both local configuration and credentials are gitignored. The connector runs as UID/GID 1000:1000, so ensure the JSON is readable by that user on the Linux host:

```bash
sudo chown 1000:1000 secrets/cloudflared.json
sudo chmod 600 secrets/cloudflared.json
docker compose -p open-train -f compose.yaml -f compose.cloudflare.yaml up -d --build
```

Ensure Docker starts on host boot. Each independent deployment has its own users and training data; cloning source does not migrate them.

## Services and storage

```bash
# From the cloned opentrain directory:
docker compose -p open-train -f compose.yaml -f compose.cloudflare.yaml ps
docker compose -p open-train -f compose.yaml -f compose.cloudflare.yaml logs --tail=50
```

The app is published on host loopback `127.0.0.1:8080` only. The `cloudflared` container routes to `http://open-train:8080` on the Compose network. Both services restart automatically. Data lives in Docker volume `open-train_training-data`, mounted at `/data`. Source deployment excludes local training data and demo runs.

`secrets/cloudflared.json` is a sensitive tunnel credential, readable only by the deployment user on the host and mounted read-only in the connector. It is excluded from source control and Docker build context. Do not print it in logs or paste it in chat. The connector image is pinned by digest.

## Enable user login

The deployment forces accounts mode. With no OAuth providers configured, protected data endpoints return 401; there is no anonymous training access or default administrator API key.

Create Google and/or GitHub OAuth applications with these callback URLs:

- `https://opentrain.example.com/auth/callback/google`
- `https://opentrain.example.com/auth/callback/github`

Replace the example hostname with your own. On the server, edit `.env` using the keys in `deploy/brev.env.example`. Keep mode 600. Set the public origin and provider client ID and secret; optionally set `OPEN_TRAIN_ADMIN_EMAILS` before the administrator's first sign-in. This is one-time server configuration shared by all users, not an environment file for every user. Leave the legacy `OPEN_TRAIN_API_KEY` empty.

For GitHub Apps, enable **Account permissions → Email addresses → Read-only**, save, and approve updated permissions when signing in. Keep wildcard callback matching disabled.

Apply changed settings by recreating the app (a plain restart does not reload Compose environment values):

```bash
docker compose -p open-train -f compose.yaml -f compose.cloudflare.yaml up -d --force-recreate open-train
```

Then use the top-right key/account button to sign in, and **Account & access → Create key** for a per-user training key.

```bash
export WANDB_BASE_URL=https://opentrain.example.com
export WANDB_ENTITY=your-displayed-entity
export WANDB_API_KEY=your-per-user-key
```

## Updates and backups

Preserve the server `.env`, `deploy/cloudflared.yml`, `secrets/`, and Docker data volume. From the deployment directory:

```bash
git pull --ff-only
docker compose -p open-train -f compose.yaml -f compose.cloudflare.yaml up -d --build
```

Do not run `docker compose down -v`: that deletes the database/blob volume. Back up the complete volume with the app stopped, or use a consistent SQLite online backup plus blobs. There is no scheduled backup configured yet. Brev must remain running for the hostname to work.

## Checks and limitations

Verify `/healthz` returns 200, `/auth/providers` reports accounts mode, and anonymous `/api/runs` and `/graphql` return 401. Live OAuth requires valid provider credentials and must be tested after configuration.

Cloudflare imposes plan-dependent HTTP upload limits (100 MB per request on Free/Pro). A tunnel does not remove these limits. Large checkpoint uploads may require a separate private/object-storage path or verified small-part uploads; the current SDK must not be assumed to chunk every large file automatically. Do not switch a tunnel hostname to DNS-only as a workaround. See [Cloudflare upload limits](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/) and [tunnel routing](https://developers.cloudflare.com/tunnel/concepts/routing/).

This remains a single-node development-stage tracker, not an HA production service. Authentication protects data, but quotas, rate limiting, artifact garbage collection, and automatic backups remain deployment follow-up work.
