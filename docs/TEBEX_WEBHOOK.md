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
- With referrals on (below), `payment.completed`, `payment.refunded` and `payment.dispute.opened` go to the
  referral rules, once per webhook id, and a failure is 500 so Tebex retries.
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

## Referrals (the Kindred Band)

Off unless `REFERRALS=on`, and it needs the receiver above (`TEBEX_WEBHOOK_SECRET`). A linked member runs
`/referral` and gets a personal code (`MK` and six letters or digits, kept until they leave or are banned).
When a new buyer's Tebex purchase carries that code, both players get the **Kindred Band**, a wardrobe helm
that only changes how a helmet looks, and the code's owner gets one more "friend invited" (`rw.invites`,
the SMP's invite ladder: the Verdant blade at 1, the Horned Helm at 3). No money and no discount change
hands. Rules in `bot/logic/referral.py`, Discord side in `bot/cogs/referral.py`; the helm itself is in the
SMP repo (`tools/wardrobe.py`, score `rw.kin`).

Guards: a buyer's Minecraft name is referred once, ever (a renewal or a second package doesn't count again);
a Tebex transaction counts once; a code can't refer its owner's own linked name, nor a name linked to the
owner's Discord account (staff get a note in 🛡️・mod). An unknown code, or a purchase with none, does nothing.

### Where the code comes from

Pick one; both reach the bot inside `payment.completed`.

1. **A custom variable (recommended, no money involved).** Tebex creator panel, Settings > Custom
   Variables: add one with the identifier `referral` (or set `REFERRAL_VARIABLE` to yours), type
   Alphanumeric, a description such as "Referral code from a friend (optional)", and leave it optional.
   Enable it on the pass packages. Tebex shows it at checkout and sends what the buyer typed in each
   product's `variables`. Case, spaces and dashes don't matter.
2. **Tebex creator codes (optional, opt-in).** Tebex's own creator codes come with a revenue share the owner
   sets per code, so the bot ignores them unless `REFERRAL_CREATOR_CODES=on`. To use them, make a creator
   code in Tebex equal to the member's `/referral` code (Tebex: Creator Codes), with the share and discount
   you decide (0 for neither). The bot reads the payment's `creator_code`. Whether any money or discount is
   involved is entirely the owner's Tebex setting; the bot never moves money.

### Delivering the helm

The unlock is three scoreboard commands on the players' names, which work while they are offline, the same
path the store uses for every `rw.*` score:

```
scoreboard players set <buyer> rw.kin 1
scoreboard players set <referrer> rw.kin 1
scoreboard players add <referrer> rw.invites 1
```

- `REFERRAL_GRANT=staff` (default): the bot posts them in 🛡️・mod for staff to run over RCON or the console.
- `REFERRAL_GRANT=rcon`: the bot sends them through the Monkey SMP bridge (`MONKEY_RCON_*`). The bridge
  accepts exactly these three shapes for this and nothing else; chat still sends only `list` and `tellraw`.
  If the server doesn't answer part way, the post lists the commands still to run.

If nothing can be delivered or posted, the bot answers 500 and Tebex retries; the claim is not kept until
something got out. The referrer gets a DM when the helm is given.

A refund (`payment.refunded`) or a chargeback (`payment.dispute.opened`) on a referred purchase posts the
undo commands in 🛡️・mod once. Nothing is taken back automatically; staff decide.

### Turning it on (the owner's steps)

1. Merge the SMP repo's Kindred Band change and deploy the datapack, so `rw.kin` exists in game.
2. Set up the receiver (above) if it isn't already.
3. In Tebex, add the custom variable (option 1) and enable it on the pass packages, and/or make creator
   codes (option 2).
4. In the Webhooks endpoint, also tick `payment.completed`, `payment.refunded` and `payment.dispute.opened`.
5. In `server/.env`: `REFERRALS=on`; optionally `REFERRAL_VARIABLE`, `REFERRAL_CREATOR_CODES=on`,
   `REFERRAL_PACKAGES` (package names or ids that count, comma-separated; empty = any) and
   `REFERRAL_GRANT=rcon`.
6. Restart Front Desk, run `/referral` yourself, and make a test purchase (Tebex's sandbox or a 100%-off
   coupon) with the code from a second Minecraft name. The post in 🛡️・mod should name both players.

Not checked against a live store: the payload shape comes from Tebex's webhook documentation
(`products[].variables[].identifier/option`, `creator_code`, `customer.username.username`). Step 6 is the
first real check; if the code doesn't show up, the post never appears and nothing else changes.
