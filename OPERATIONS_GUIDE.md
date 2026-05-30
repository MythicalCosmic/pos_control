# Operations Guide — alpha_pos + pos_control_center

How the two projects fit together, how to deploy them, and how to run the
licensing lifecycle day to day. This guide lives in the control center repo
but covers **both** sides. It is kept in sync with the code — every command,
env var, and endpoint here is exactly what the current builds expect.

---

## 1. The two pieces

| Project | Who runs it | Role |
|---|---|---|
| **pos_control_center** | You (the vendor), one central VPS | Issues license keys, records heartbeats, holds the prepaid-balance/subscription billing + suspend/resume/banner controls in the Django admin. The authority. |
| **alpha_pos** | Each restaurant, one install per site | The actual POS. Phones home to the control center, enforces a kill switch when the license is suspended, the prepaid wallet runs dry, or it has been offline too long. |

They are deliberately separate codebases: alpha_pos ships to every customer
and must not carry your admin code; the control center is yours alone.

### How they talk

```
                 ┌──────────────────────── pos_control_center (vendor VPS) ────┐
                 │  POST /api/v1/register   → exchange a pre-bound email for a  │
                 │                            license key                       │
                 │  POST /api/v1/heartbeat  → confirm key still valid           │
                 │                            (response is HMAC-signed)         │
                 │  POST /api/billing/click/prepare  + /complete                │
                 │  POST /api/billing/payme           → top-up webhooks         │
                 │  /admin/                 → billing / suspend / resume / msg  │
                 │  python manage.py bill_subscriptions  → daily cron           │
                 └──────────────────────────────────────────────────────────────┘
                        ▲  (1) register        ▲  (2) heartbeat every ~5 min
                        │  HTTPS               │  Bearer <license key>
                        │                      │  + verifies X-Response-Signature
                 ┌──────┴──────────────────────┴────── alpha_pos (restaurant) ──┐
                 │  POST /api/licensing/setup  → setup wizard (calls register)  │
                 │  GET  /api/licensing/status → UI reads license state          │
                 │  LicenseEnforcementMiddleware → kill switch on every request  │
                 │  heartbeat_daemon (sidecar process) → the periodic phone-home │
                 └──────────────────────────────────────────────────────────────┘
```

### License states (alpha_pos side)

- `UNREGISTERED` — no setup done; everything except the licensing/health
  endpoints is 503.
- `ACTIVE` — paid up, heartbeats fresh.
- `SUSPENDED` — control center said stop, OR rejected the bearer key, OR the
  encrypted-key blob can't be decrypted on this install. Kill switch on.
- `EXPIRED` — the prepaid wallet can't fund the current subscription period
  AND the grace cushion has run out. Decided by the control center.

**Offline grace**: if the control center is unreachable, the install keeps
working for `LICENSE_GRACE_DAYS` (default 7) measured from the last
**successful** heartbeat (clock anchored on the control center's
`server_now`, not the host wall clock), then the kill switch fires.

**Billing grace**: independently, when `paid_through` lapses the heartbeat
keeps reporting ACTIVE for `Subscription.grace_days` (default 3) before
flipping to EXPIRED — so a missed renewal doesn't brick a busy restaurant
mid-service.

---

## 2. Deploy the control center

### Option A — Docker Compose (recommended)

```bash
cd pos_control_center
cp .env.example .env
# Edit .env: set DEBUG=False, a real SECRET_KEY, ALLOWED_HOSTS,
# CSRF_TRUSTED_ORIGINS, DB_PASSWORD. (Email + payment-provider creds can
# be wired later — emails fall back to the console backend.)
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

The web container waits for Postgres, runs migrations, collects static, and
serves on port 8000 via gunicorn. Admin CSS/JS is served by WhiteNoise, so
you can point a TLS-terminating reverse proxy (nginx/Caddy) straight at it —
no separate static server needed. When behind such a proxy set
`TRUST_FORWARDED_PROTO=True`.

### Option B — bare metal / venv

```bash
cd pos_control_center
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DEBUG=False SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(64))')"
export ALLOWED_HOSTS=control.example.com CSRF_TRUSTED_ORIGINS=https://control.example.com
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
gunicorn pos_control_center.wsgi:application --bind 0.0.0.0:8000 --workers 3
```

### Local dev

```bash
pip install -r requirements-dev.txt
export DEBUG=True SECRET_KEY=dev-key
python manage.py migrate && python manage.py createsuperuser
python manage.py runserver 9000
```

### Cron — daily billing settle + warning emails

```cron
# Settle every active subscription and send warn/grace/lockout emails.
# Idempotent; safe to run more often than once a day.
30 6 * * *  cd /srv/pos_control_center && /srv/.venv/bin/python manage.py bill_subscriptions >> /var/log/pos_control_center/bill.log 2>&1
```

The command also throttles each email to one per ~20 hours per tenant, so
running it hourly during a recovery window won't spam anybody.

---

## 3. Deploy alpha_pos (per restaurant)

alpha_pos already ships with `Dockerfile`, `docker-compose.yaml`,
`entrypoint.sh`, and `install.sh` / `install.bat`. The licensing-specific
env vars are what matter here:

```bash
# in the restaurant's alpha_pos .env
LICENSE_CONTROL_CENTER_URL=https://control.example.com   # MUST be https when DEBUG=False
LICENSE_FERNET_KEY=<generate once, pin forever>          # see below
LICENSE_HEARTBEAT_INTERVAL=300                           # seconds (optional)
LICENSE_GRACE_DAYS=7                                     # offline grace (optional)
# LICENSE_TLS_CA_BUNDLE=/etc/ssl/private-ca.pem          # optional, for private CAs
```

Generate the Fernet key (encrypts the license key at rest) once and **never
rotate it** — rotating it makes the existing encrypted key unreadable, which
the heartbeat daemon now treats as **fail-closed** (status → SUSPENDED,
kill switch fires). Recovery is "re-run setup wizard":

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> **Local development — bypass licensing entirely.** Set `DEBUG=True` and
> `LICENSE_DEV_BYPASS=true` in alpha_pos's `.env` to disable the kill switch:
> the POS runs with no control center, no heartbeat, and nothing to pay. It
> is **hard-gated on DEBUG** — a production build (`DEBUG=False`) ignores
> the flag entirely, so a customer can never use it to dodge billing. Never
> ship with it on.

The heartbeat daemon runs as a **sidecar process**, started by
`entrypoint.sh` (not inside gunicorn). Confirm it's alive with `docker
logs`. To send a single heartbeat manually:

```bash
python manage.py heartbeat_daemon --once
```

---

## 4. Onboard a restaurant

The flow is **"email verified by you, then they pay monthly"** — the
customer never sees an invite code; the vendor (you) verifies the email by
pre-issuing an `InviteCode` bound to it.

1. **Issue an invite** in the control center admin →
   *Tenants › Invite codes › Add*. Set `intended_email` to the address the
   restaurant will type in their POS. Optionally set `intended_org_name`
   (cosmetic — pre-fills the Tenant row's display name) and `expires_at`.
   Save. You do not need to send the customer the generated `code` — they
   never type it.
2. **Customer types only their email** in the alpha_pos setup wizard, which
   POSTs `{"email": "..."}` to `/api/licensing/setup`. alpha_pos relays
   `{"email": "..."}` to the control center's `/api/v1/register`.
3. The control center looks up the **oldest unconsumed, unexpired invite
   whose `intended_email` matches** (case-insensitive), consumes it,
   creates the Tenant + Subscription + LicenseKey, and returns the cleartext
   key to alpha_pos. alpha_pos encrypts it with `LICENSE_FERNET_KEY` and
   flips to `ACTIVE`. **The cleartext is never echoed back to the operator
   — there is no recovery if alpha_pos's Fernet key is lost.**
4. The kill switch clears; business endpoints start responding.

**Legacy "printed code" path** — `/api/v1/register` still accepts
`{"email", "org_name", "invite_code"}` so an operator can hand a customer a
raw code on a card if that's preferable. The code is one-shot: a second
redemption returns 409. A code pre-bound to a different email returns 403.

---

## 5. Billing: prepaid balance + subscription

Access is **money-driven**, not date-driven. Each tenant has a prepaid
**balance** (wallet); a **subscription** charges a fixed **price** once per
**period** (default 30 days) out of that wallet. While the wallet covers
the period, the POS runs; when it can't and the grace cushion is exhausted,
the heartbeat reports `EXPIRED` and the kill switch fires. There are no
manual expiry dates to juggle.

### Set up a tenant's plan

`/admin/` → *Billing › Subscriptions* (one is auto-created at registration
with `price=0`, i.e. free until you set a price):

| Field | Meaning |
|---|---|
| `price` | Charged from the wallet each period. |
| `period_days` | Length of a billing period (e.g. 30). |
| `warn_days` | How many days **before** cut-off the POS shows a "top up soon" banner — the **dynamic lead time** you control. |
| `grace_days` | Soft cushion **after** `paid_through` lapses. Heartbeat stays ACTIVE (with `in_grace=True` and `warn=True`) for this many days so a missed renewal doesn't brick a busy restaurant mid-service. Set to `0` for instant lockout. Default 3. |
| `status` | `ACTIVE` (billing on) or `CANCELED` (stop renewing; runs out the current period then expires). |

> `paid_through` / `last_charged_at` / `last_*_sent_at` are managed
> automatically — the billing service advances them when it charges, and
> `bill_subscriptions` updates the `last_*_sent_at` timestamps when it
> emails. Don't hand-edit them.

### Top up a wallet

Credits flow in only through the payment provider webhooks. The control
center has **no manual top-up form** — keeping money movement on a single,
auditable rail. Both providers are idempotent and write to the append-only
*Billing › Payments* ledger:

- **Click.uz**: configure `CLICK_*` env vars; Click calls
  `/api/billing/click/prepare` + `/complete`. `merchant_trans_id` = the
  tenant's primary key.
- **Payme.uz**: set `PAYME_MERCHANT_KEY`; Payme posts JSON-RPC to
  `/api/billing/payme`. `account.tenant_id` = the tenant's primary key.

A successful top-up immediately **settles** the subscription, so a cut-off
install revives on its very next heartbeat (≤5 min). The heartbeat
forwards `balance`, `days_remaining`, `warn`, and `in_grace` to the POS so
the renderer can show the customer their credit and a "pay now" banner
before the kill switch fires.

A safety cap (`MAX_CREDIT_AMOUNT`, default 1,000,000,000 so'm) rejects
absurd top-ups so a runaway webhook can't credit an unrecoverable amount
in one shot.

### Warning emails

`bill_subscriptions` (run daily from cron — see §2) emails the tenant's
contact address one nudge at a time. Each fires at most once per ~20 hours
per tenant:

| Trigger | Subject |
|---|---|
| inside `warn_days` of cut-off | "Your POS subscription renews in N days" |
| `paid_through` past, inside `grace_days` cushion | "POS payment overdue — service stops in N days" |
| grace exhausted (status now EXPIRED) | "POS service has been paused — top up to restore" |

Configure SMTP via the `EMAIL_HOST` / `EMAIL_HOST_USER` /
`EMAIL_HOST_PASSWORD` env vars; with `EMAIL_HOST` unset, the console
backend prints emails to stdout (useful for development and the e2e
script).

### Manual overrides

`/admin/` → *Licenses › License keys* (independent of balance):

| Action | Effect (next heartbeat) |
|---|---|
| **Suspend selected** | Force SUSPENDED regardless of balance. |
| **Resume selected** | Clear the manual suspend. |
| **Clear banner message** | Remove a pushed banner. |
| Edit a row → status REVOKED | Permanently retire the key (heartbeat returns 410). |

Every license/billing action writes an append-only **ControlEvent** /
**Payment** row for audit.

---

## 6. Heartbeat security model

The heartbeat is the single channel the kill switch listens to, so it is
defended on four axes. None of these require any operational work — they
are baked into the code — but you should know how they fail so you can
diagnose an oddly-behaving install.

| Defence | What it stops | How it fails (what to look for) |
|---|---|---|
| **TLS verify=True (always), private-CA bundle via `LICENSE_TLS_CA_BUNDLE`** | MITM with a self-signed cert | heartbeat logs `SSLError` until the operator fixes the trust chain |
| **`LICENSE_CONTROL_CENTER_URL` must be HTTPS in production** (re-checked at every heartbeat) | env var being silently mutated to plaintext post-boot | heartbeat returns `503 control_center_url_must_be_https` |
| **HMAC signature on every heartbeat response** — `X-Response-Signature: sha256=<hmac>` over canonical JSON, keyed on the bearer license key | MITM that has bypassed TLS forging `{"status":"ACTIVE"}` | heartbeat returns `502 response_signature_invalid`; License row unchanged so the grace clock keeps ticking; the operator must fix the proxy or there's a real attack in progress |
| **`last_heartbeat_at` anchored to control center's `server_now`** (not the local wall clock) | operator winding the host clock forward to fake successful heartbeats and extend `grace_until` by decades | nothing visible — the protection just kicks in automatically |
| **Fernet decryption failure → SUSPENDED** | `LICENSE_FERNET_KEY` being rotated (or the encrypted blob being corrupted) leaving the install drifting ACTIVE until `grace_until` lapses | License row immediately flips to SUSPENDED with `last_message` "License key cannot be decrypted on this install. Re-run the setup wizard to restore service." |

The HMAC key is the bearer license key itself — both sides already share
it, no extra secret to manage. Rotating the key (revoke + reissue)
invalidates all old signatures for free.

---

## 7. Verify the two work together

An end-to-end script boots both servers and walks the whole lifecycle:
register → empty-wallet expiry → top-up revive → warn flag → suspend →
resume → banner → offline grace → audit trail.

```bash
cd alpha_pos
.venv/bin/python licensing/scripts/e2e_verify.py
# expect: PASSED: 17  FAILED: 0
```

Quick manual smoke test:

```bash
curl https://control.example.com/healthz                  # -> ok
curl https://<pos-host>/api/licensing/status              # -> {"status":"UNREGISTERED", ...} before setup
```

Run the unit suites (each project has its own venv with its own deps):

```bash
cd pos_control_center && DEBUG=True .venv/bin/pytest -q   # 73 passed
cd alpha_pos          && DEBUG=True .venv/bin/pytest -q   # 264 passed
```

---

## 8. Configuration reference

### pos_control_center

| Env var | Required | Notes |
|---|---|---|
| `DEBUG` | — | `False` in production. |
| `SECRET_KEY` | when DEBUG=False | boot fails without it. |
| `ALLOWED_HOSTS` | when DEBUG=False | comma-separated. |
| `CSRF_TRUSTED_ORIGINS` | for HTTPS admin behind proxy | e.g. `https://control.example.com`. |
| `DB_ENGINE` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` | for Postgres | blank `DB_ENGINE` → SQLite. |
| `DB_CONN_MAX_AGE` | optional | persistent DB connections (default 60s). |
| `TRUST_FORWARDED_PROTO` | behind TLS proxy | trusts `X-Forwarded-Proto`. |
| `SECURE_SSL_REDIRECT` | optional | redirect HTTP→HTTPS at the app. |
| `CLICK_SERVICE_ID` / `CLICK_MERCHANT_ID` / `CLICK_SECRET_KEY` | for Click.uz top-ups | merchant credentials. |
| `PAYME_MERCHANT_KEY` | for Payme.uz top-ups | Paycom merchant key (Basic-auth password). |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | for warning emails | leave `EMAIL_HOST` blank to print to stdout (console backend). |
| `EMAIL_USE_TLS` / `EMAIL_USE_SSL` / `EMAIL_TIMEOUT` | optional | TLS defaults to on, timeout 15s. |
| `DEFAULT_FROM_EMAIL` | optional | from-address on warning emails. |
| `GUNICORN_WORKERS` / `GUNICORN_TIMEOUT` / `LOG_LEVEL` | optional | runtime tuning. |

### alpha_pos (licensing)

| Env var | Required | Notes |
|---|---|---|
| `LICENSE_CONTROL_CENTER_URL` | yes | must be `https://` when DEBUG=False — re-checked at every heartbeat. |
| `LICENSE_FERNET_KEY` | production | pin it; rotating triggers fail-closed (SUSPENDED). |
| `LICENSE_TLS_CA_BUNDLE` | optional | path to a custom CA bundle for a private control-center cert. |
| `LICENSE_HEARTBEAT_INTERVAL` | optional | seconds, default 300. |
| `LICENSE_GRACE_DAYS` | optional | offline grace, default 7. |
| `LICENSE_HTTP_TIMEOUT_S` | optional | per-request HTTP timeout, default 10s. |
| `LICENSE_STATE_CACHE_TTL` | optional | middleware cache TTL, default 60s. |
| `LICENSE_HEARTBEAT_DISABLED` | dev only | skip the sidecar daemon. |
| `LICENSE_DEV_BYPASS` | dev only (DEBUG=True only) | disable kill switch entirely. |

---

## 9. Security notes

- The control center stores only `sha256(key)` + an 8-char prefix — never
  the cleartext. A lost key means revoke + reissue; there is no recovery.
- alpha_pos stores the key **encrypted** (Fernet) at rest. In production
  set `LICENSE_FERNET_KEY` explicitly; the dev fallback derives it from
  `SECRET_KEY`, which breaks (fail-closed → SUSPENDED) if `SECRET_KEY`
  rotates.
- Keep `LICENSE_CONTROL_CENTER_URL` on HTTPS — the heartbeat refuses to
  call out over plaintext in production, and even with TLS the response is
  HMAC-signed so a MITM that bypasses TLS still can't forge ACTIVE.
- Credits move only through the Click.uz / Payme.uz webhooks. The admin
  has no manual top-up form. This is intentional — one auditable rail for
  every soum that enters a tenant's wallet.
- The grace cushion (`Subscription.grace_days`) is "soft fail before hard
  fail". Set it to 0 if you want strict cut-off; leave it at the default
  3 to absorb a customer being a few hours late with a transfer.
- `bill_subscriptions` is the daily cron heartbeat for the dashboard:
  without it, offline installs would stay "ACTIVE / paid through some
  date in the past" in the admin until they came back online to settle
  themselves.

---

## Appendix A — what's NOT in this build

These were removed during hardening and are documented here so you don't
go looking for them:

- **Perpetual-unlock escape hatch** (Ed25519 vendor signature). The
  `generate_vendor_keypair` and `generate_unlock` management commands
  still exist in the control center but the consuming code on alpha_pos
  (URL, view, service, License status, public-key verification) was
  removed. Treat both commands as orphaned until the feature is
  redesigned end to end.
- **Admin "Add credit" form**. Removed in favor of the
  Click/Payme-webhook-only credit path — see §5.
