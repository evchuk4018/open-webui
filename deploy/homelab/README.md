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

The deployment seeds these OpenAI-compatible connections on a fresh data volume:

- OpenAI API (regular API-key access only): `https://api.openai.com/v1`
- OpenRouter: `https://openrouter.ai/api/v1`
- OpenCode Inference: `https://opencode.ai/inference/openai/v1`
- DeepSeek: `https://api.deepseek.com/v1`

All four key fields start empty so tokens can be added later in Admin Settings → Connections. No Codex API-key connection is seeded: Open WebUI does not accept a ChatGPT/Codex subscription token as an OpenAI API key, so your Codex subscription cannot be used through this regular provider field.

The connection rows are stored in Open WebUI's data volume. The Compose environment also seeds them if that volume is ever initialized from scratch; it never contains provider secrets.

## Updating

```sh
git pull --ff-only origin main
docker compose --env-file deploy/homelab/.env -f deploy/homelab/compose.yaml pull
docker compose --env-file deploy/homelab/.env -f deploy/homelab/compose.yaml up -d
```

Keep the `.env` file and the Docker volumes outside Git. Stopping the stack does not delete its data.
