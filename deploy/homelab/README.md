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

All four key fields start empty so tokens can be added later in Admin Settings → Connections. ChatGPT/Codex plan usage is a separate per-user connection: do not paste a ChatGPT or Codex subscription token into an API-key field.

## ChatGPT plan sign-in

Open WebUI supports the official Sign in with ChatGPT flow for eligible ChatGPT plan usage. The OAuth callback is a local loopback URL so the homelab does not need another public port. From the computer where you will complete the browser sign-in, keep this tunnel open:

```sh
ssh -N -L 1455:127.0.0.1:3001 evanh@homelab
```

Then open Open WebUI → Settings → Connections → Continue with ChatGPT and complete the sign-in in the browser yourself. The tunnel must remain open until the callback page confirms the connection. Open WebUI stores the resulting credentials encrypted on the homelab and never in browser storage.

Plan allowance and per-app limits are controlled by ChatGPT and may be shared with other ChatGPT/Codex usage. Review or change them at [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage). If the callback expires, the browser says it cannot connect, or port `1455` is busy, close the old tunnel, run the SSH command again, and start a fresh sign-in. Keep the URI as `127.0.0.1` (not `localhost`) and leave the SSH terminal open. If plan usage was not approved, reconnect and approve the plan-usage permission; disconnecting removes the local encrypted credential and attempts remote revocation.

The connection rows are stored in Open WebUI's data volume. The Compose environment also seeds them if that volume is ever initialized from scratch; it never contains provider secrets.

## Updating

```sh
git pull --ff-only origin main
docker compose --env-file deploy/homelab/.env -f deploy/homelab/compose.yaml pull
docker compose --env-file deploy/homelab/.env -f deploy/homelab/compose.yaml up -d
```

Keep the `.env` file and the Docker volumes outside Git. Stopping the stack does not delete its data.
