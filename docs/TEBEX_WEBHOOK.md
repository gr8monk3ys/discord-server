# Tebex "pass ending" alerts

When a buyer cancels a monthly Monkey SMP pass, Tebex keeps it running until the paid month ends and
sends a webhook, `recurring-payment.cancellation.requested`. Front Desk can receive it and post one line
in 🛡️・mod: the player, the package, the end date and the reason they gave Tebex, so staff DM them while
the pass still runs (the SMP repo's `docs/RETENTION.md`, "When a pass is ending").

The receiver is `bot/cogs/tebex.py` (rules in `bot/logic/tebex.py`). It is **off** until
`TEBEX_WEBHOOK_SECRET` is set; with it unset the bot opens no port.

## What it accepts

- `POST /tebex/webhook` only (any other path is 404, any other method 405), bodies up to 256 KB.
- Every request must carry Tebex's `X-Signature`: HMAC-SHA256, keyed with the webhook secret, of the
  hex SHA-256 of the raw body. A missing or wrong signature is 401 and the body is never parsed.
- `validation.webhook` (Tebex checking the endpoint) is answered with its `id`.
- `recurring-payment.cancellation.requested` posts in 🛡️・mod once per webhook id (Tebex retries are
  recognised). If the post fails, the answer is 500 and Tebex retries later.
- Every other type is answered 200 and ignored.
- The secret is never logged, and request lines are not logged.

## Settings (`server/.env`)

| Key | Default | What |
| --- | --- | --- |
| `TEBEX_WEBHOOK_SECRET` | empty (off) | The secret Tebex shows for the endpoint. 16+ characters, or the receiver stays off. |
| `TEBEX_WEBHOOK_HOST` | `127.0.0.1` | The address to listen on. Keep the default behind a tunnel or reverse proxy. |
| `TEBEX_WEBHOOK_PORT` | `8790` | The port (1024 to 65535). |

## Turning it on (the owner's steps)

1. **Make the endpoint public.** The bot listens on `http://127.0.0.1:8790/tebex/webhook`; Tebex needs an
   HTTPS URL that reaches it. Any of these works, and choosing and running it is the owner's job:
   - a Cloudflare Tunnel (`cloudflared`) with a public hostname, for example `tebex-hook.<your domain>`,
     whose service is `http://127.0.0.1:8790`;
   - Tailscale Funnel on this machine, pointed at port 8790;
   - a reverse proxy you already run, forwarding only `/tebex/webhook`.
   Only expose that one path. Nothing else in the bot listens on the network.
2. **Add the endpoint in Tebex.** On the Tebex creator panel's Webhooks page (developer settings), add an endpoint
   `https://<your public host>/tebex/webhook`, and tick `recurring-payment.cancellation.requested` (nothing
   else is needed). Copy the endpoint's **secret**.
3. **Put the secret in `server/.env`** as `TEBEX_WEBHOOK_SECRET=<secret>`. Never commit it, and don't
   paste it into Discord or an issue.
4. **Restart Front Desk** (README runbook: `python server/watchdog.py --pause 60`, then restart the
   "Front Desk bot" scheduled task). The log says `tebex: webhook receiver on 127.0.0.1:8790/tebex/webhook`.
5. **Validate in Tebex.** Tebex sends `validation.webhook` when the endpoint is added or when you press
   validate; the bot answers it. Tebex's "send test webhook" for the cancellation type should put a line
   in 🛡️・mod.

To turn it off, empty `TEBEX_WEBHOOK_SECRET` and restart; then remove the tunnel and the Tebex endpoint.

Tebex sends webhooks only from 18.209.80.3 and 54.87.231.232. Behind a tunnel every request seems to come
from 127.0.0.1, so the bot relies on the signature, not the address; a proxy that can filter by the
real client address may add that allowlist too.
