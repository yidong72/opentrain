# Deploy on another machine

Requires Git, Docker Engine, and the Docker Compose plugin. Each independent deployment has its own accounts, API keys, database, and files; cloning the code does not migrate training data.

## Install and configure

```bash
git clone https://github.com/yidong72/opentrain.git
cd opentrain
cp .env.example .env
chmod 600 .env
```

Edit `.env` on the server:

- Keep `OPEN_TRAIN_AUTH_MODE=accounts`.
- Set `OPEN_TRAIN_PUBLIC_URL` to your HTTPS origin, such as `https://opentrain.example.com`.
- Configure at least one Google/GitHub client ID and client secret. These identify the server application; each user signs in separately and creates their own training API key.
- Optionally set `OPEN_TRAIN_ADMIN_EMAILS` before the administrator's first sign-in.
- Leave `OPEN_TRAIN_API_KEY` empty: the legacy shared key grants server-wide administrator access.

Register the exact callback `https://opentrain.example.com/auth/callback/github` or `/auth/callback/google`. A GitHub App requires **Account permissions → Email addresses → Read-only**; users must approve any new permission. No wildcard callback matching is needed. If using an existing app, preserve its existing callbacks and permissions.

```bash
docker compose -p open-train up -d --build
docker compose -p open-train ps
curl --fail http://127.0.0.1:8080/healthz
```

The app binds to **host loopback only**. Configure a host-side HTTPS reverse proxy to `http://127.0.0.1:8080`, or follow [Cloudflare Tunnel setup](deployment-brev.md). A proxy in another container must use a shared Docker network and `http://open-train:8080`, not its own loopback. Do not expose an unauthenticated local-mode server publicly.

Once HTTPS routing is working, open the site and sign in with GitHub or Google. Use the top-right account/key button → **Account & access → API keys → Create key**. Save the key immediately; it is only displayed once.

In your training environment:

```bash
export WANDB_BASE_URL=https://opentrain.example.com
export WANDB_ENTITY=your-displayed-entity
export WANDB_API_KEY=your-per-user-api-key
```

Continue using the official `wandb` client. See the [compatibility matrix](compatibility.md) before relying on untested W&B features.

## Verify

- `/healthz` returns HTTP 200.
- `/auth/providers` lists the configured provider and `accounts_enabled: true`.
- Anonymous `/api/runs` and `/graphql` requests return HTTP 401.
- Complete a browser login and send a small run with your own API key.

If GitHub login fails after authorization, check email-read permission and approve the updated authorization. Treat server logs as sensitive: OAuth callback URLs can include short-lived authorization codes.

## Update and preserve data

```bash
git pull --ff-only
docker compose -p open-train up -d --build
```

After changing `.env`, use `docker compose -p open-train up -d --force-recreate open-train`; a plain restart does not reload environment values. If using Cloudflare, include both Compose files as shown in its guide for every Compose command.

The stable project name stores data in `open-train_training-data`, mounted at `/data`. Preserve `.env`, `secrets/`, and the volume. **Do not use `docker compose down -v`**, which deletes the volume. Back up the complete volume with the app stopped, or use SQLite's online backup API plus a consistent blob snapshot. Never copy only a live SQLite database without its WAL. Keep credential backups encrypted and separate from the repository.

This is a single-node alpha, without HA, automatic backups, quotas, or artifact garbage collection. A second independent instance does not automatically share runs or users with the first.
