# Homelab deployment

This Compose file runs Open WebUI as a standalone frontend on the homelab. It does not install or run Ollama; connect a model provider separately when needed.

The service is intentionally passwordless (`WEBUI_AUTH=False`). Access control is provided by the Caddy route, which only permits Tailscale addresses. Anyone who can reach the hostname over the homelab tailnet can use the application, so do not remove that Caddy restriction without adding another access-control layer.

## Deployment

From the repository root on the homelab:

```sh
cp deploy/homelab/.env.example deploy/homelab/.env
openssl rand -hex 32
# Put the generated value in deploy/homelab/.env as WEBUI_SECRET_KEY.
docker compose --env-file deploy/homelab/.env -f deploy/homelab/compose.yaml config
docker compose --env-file deploy/homelab/.env -f deploy/homelab/compose.yaml pull
docker compose --env-file deploy/homelab/.env -f deploy/homelab/compose.yaml up -d
```

The WebUI listens only on `127.0.0.1:3001`; Caddy serves it at `https://ai.wowzerbowser.xyz`. Open WebUI data is stored in the named `open-webui` volume.

## Updating

```sh
git pull --ff-only origin main
docker compose --env-file deploy/homelab/.env -f deploy/homelab/compose.yaml pull
docker compose --env-file deploy/homelab/.env -f deploy/homelab/compose.yaml up -d
```

Keep the `.env` file and the Docker volumes outside Git. Stopping the stack does not delete its data.
