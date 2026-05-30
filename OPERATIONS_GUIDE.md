# Operations Guide — alpha_pos + pos_control_center

How the two projects fit together, how to deploy them, and how to run the
licensing lifecycle day to day. This guide lives in the control center repo
but covers **both** sides.

---

## 1. The two pieces

| Project | Who runs it | Role |
|---|---|---|
| **pos_control_center** | You (the vendor), one central VPS | Issues license keys, records heartbeats, holds the prepaid-balance/subscription billing + suspend/resume/banner controls in the Django admin. The authority. |
| **alpha_pos** | Each restaurant, one install per site | The actual POS. Phones home to the control center, enforces a kill switch when the license is suspended/expired/offline. |

They are deliberately separate codebases: alpha_pos ships to every customer
and must not carry your admin code; the control center is yours alone.

### How they talk

```
                 ┌──────────────────────── pos_control_center (vendor VPS) ─┐
                 │  POST /api/v1/register   → exchange invite for a key      │
                 │  POST /api/v1/heartbeat  → confirm key still valid        │
                 │  /admin/                 → billing / suspend / resume / msg│
                 └───────────────────────────────────────────────────────────┘
                        ▲  (1) register        ▲  (2) heartbeat every ~5 min
                        │  HTTPS               │  Bearer <license key>
                 ┌──────┴──────────────────────┴──── alpha_pos (restaurant) ─┐
                 │  POST /api/licensing/setup   → setup wizard (calls register)│
                 │  GET  /api/licensing/status  → UI reads license state       │
                 │  POST /api/licensing/unlock  → paste vendor unlock file      │
                 │  LicenseEnforcementMiddleware → kill switch on every request │
                 │  heartbeat_daemon (sidecar process) → the periodic phone-home│
                 └───────────────────────────────────────────────────────────┘
```

The trust anchor for the "vendor disappeared" escape hatch is an **Ed25519
keypair**: the public half is baked into every alpha_pos install, the private
half stays in your offline vault.

### License states (alpha_pos side)

- `UNREGISTERED` — no setup done; everything except the licensing/health endpoints is 503.
- `ACTIVE` — paid up, heartbeats fresh.
- `SUSPENDED` — control center said stop (or rejected the key). Kill switch on.
- `EXPIRED` — the prepaid wallet can't fund the current subscription period (decided by the control center; see §6).
- `PERPETUAL_UNLOCK` — a valid vendor-signed unlock file was accepted; kill switch off forever.

Offline grace: if the control center is unreachable, the install keeps working
for `LICENSE_GRACE_DAYS` (default 7) measured from the last successful
heartbeat, then the kill switch fires.

---

## 2. One-time vendor setup (do this first, once)

Generate the signing keypair on a trusted machine:

```bash
cd pos_control_center
DEBUG=True python manage.py generate_vendor_keypair
```

It prints two hex values:

- `LICENSE_VENDOR_PRIVATE_KEY` → store in an **offline vault** (YubiKey / KMS /
  1Password). Never commit it; never leave it set on the running server.
- `LICENSE_VENDOR_PUBLIC_KEY` → this goes into **every alpha_pos build** and,
  optionally, the control center env (for display).

> Re-running this command invalidates every unlock file the old key ever
> signed. Run it once and keep the output safe.

---

## 3. Deploy the control center

### Option A — Docker Compose (recommended)

```bash
cd pos_control_center
cp .env.example .env
# Edit .env: set DEBUG=False, a real SECRET_KEY, ALLOWED_HOSTS,
# CSRF_TRUSTED_ORIGINS, DB_PASSWORD, and (optionally) LICENSE_VENDOR_PUBLIC_KEY.
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

The web container waits for Postgres, runs migrations, collects static, and
serves on port 8000 via gunicorn. Admin CSS/JS is served by WhiteNoise, so you
can point a TLS-terminating reverse proxy (nginx/Caddy) straight at it — no
separate static server needed. When behind such a proxy set
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

---

## 4. Deploy alpha_pos (per restaurant)

alpha_pos already ships with `Dockerfile`, `docker-compose.yaml`,
`entrypoint.sh`, and `install.sh` / `install.bat`. The licensing-specific env
vars are what matter here:

```bash
# in the restaurant's alpha_pos .env
LICENSE_CONTROL_CENTER_URL=https://control.example.com   # MUST be https when DEBUG=False
LICENSE_FERNET_KEY=<generate once, pin forever>          # see below
LICENSE_VENDOR_PUBLIC_KEY=<the public hex from step 2>
LICENSE_HEARTBEAT_INTERVAL=300                           # seconds (optional)
LICENSE_GRACE_DAYS=7                                     # offline grace (optional)
```

Generate the Fernet key (encrypts the license key at rest) once and **never
rotate it** — rotating it forces a re-run of the setup wizard:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> **Local development — bypass licensing entirely.** Set `DEBUG=True` and
> `LICENSE_DEV_BYPASS=true` in alpha_pos's `.env` to disable the kill switch:
> the POS runs with no control center, no heartbeat, and nothing to pay. It is
> **hard-gated on DEBUG** — a production build (`DEBUG=False`) ignores the flag
> entirely, so a customer can never use it to dodge billing. Never ship with it on.

The heartbeat daemon runs as a **sidecar process**, started by `entrypoint.sh`
(not inside gunicorn). Confirm it's alive with `docker logs`. To send a single
heartbeat manually:

```bash
python manage.py heartbeat_daemon --once
```

---

## 5. Onboard a restaurant

1. **Issue an invite** in the control center admin → *Tenants › Invite codes ›
   Add*. Optionally pre-bind `intended_email` / `intended_org_name` so only the
   right customer can redeem it. Copy the generated `code`.
2. **Send the code** to the restaurant (email/Telegram/paper).
3. The restaurant runs the **setup wizard** — `POST /api/licensing/setup` with
   `{ "email", "org_name", "invite_code" }`. alpha_pos relays it to the control
   center's `/api/v1/register`, receives the key, encrypts and stores it, and
   flips to `ACTIVE`. The key is shown **once** and never echoed again.
4. The kill switch clears; business endpoints start responding.

An invite is one-shot: a second redemption returns 409. A bound invite with a
mismatched email/org returns 403.

---

## 6. Billing: prepaid balance + subscription

Access is **money-driven**, not date-driven. Each tenant has a prepaid
**balance** (wallet); a **subscription** charges a fixed **price** once per
**period** (default 30 days) out of that wallet. While the wallet covers the
period the POS runs; when it can't, the heartbeat reports `EXPIRED` and the
kill switch fires. There are no manual expiry dates to juggle.

**Set up a tenant's plan** — `/admin/` → *Billing › Subscriptions* (one is
auto-created at registration with `price=0`, i.e. free until you set a price):

| Field | Meaning |
|---|---|
| `price` | Charged from the wallet each period. |
| `period_days` | Length of a billing period (e.g. 30). |
| `warn_days` | How many days before cut-off the POS shows a "top up soon" banner — the **dynamic lead time** you control. |
| `grace_days` | Soft cushion **after** `paid_through` lapses. Heartbeat stays ACTIVE (with `in_grace=True` and `warn=True`) for this many days so a missed renewal doesn't brick a busy restaurant mid-service. Set to `0` for instant lockout. |
| `status` | `ACTIVE` (billing on) or `CANCELED` (stop renewing; runs out the current period then expires). |

> `paid_through` / `last_charged_at` / `last_*_sent_at` are managed
> automatically — the billing service advances them when it charges, and
> `bill_subscriptions` updates the `last_*_sent_at` timestamps when it emails.
> Don't hand-edit them.

**Top up a wallet** — credits flow in only through the payment provider
webhooks (the control center has no manual top-up form — keeps the money trail
on one auditable rail). Both are idempotent and write to the append-only
*Billing › Payments* ledger:
- **Click.uz**: configure `CLICK_*` env vars; Click calls
  `/api/billing/click/prepare` + `/complete`. `merchant_trans_id` = the tenant's id.
- **Payme.uz**: set `PAYME_MERCHANT_KEY`; Payme posts JSON-RPC to
  `/api/billing/payme`. The `account.tenant_id` = the tenant's id.

**Warning emails** — `bill_subscriptions` (daily cron) also emails the tenant's
contact address one nudge at a time:
- **warn** — paid through the future but inside `warn_days`: "top up soon".
- **grace** — paid_through already past, inside the `grace_days` cushion:
  "service stops in N days".
- **lockout** — grace exhausted, kill switch will fire on the next heartbeat:
  "service has been paused".

Each fires at most once per ~20 hours per tenant. Configure SMTP via the
`EMAIL_HOST` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` env vars; with
`EMAIL_HOST` unset, the console backend prints emails to stdout (useful for
development and the e2e script).

A top-up immediately **settles** the subscription, so a cut-off install revives
on its very next heartbeat (≤5 min). The heartbeat also forwards `balance`,
`days_remaining`, and a `warn` flag to the POS so it can show the customer their
credit and warn before cutoff.

**Manual overrides** still live on *Licenses › License keys* (independent of
balance):

| Action | Effect (next heartbeat) |
|---|---|
| **Suspend selected** | Force SUSPENDED regardless of balance. |
| **Resume selected** | Clear the manual suspend. |
| **Clear banner message** | Remove a pushed banner. |
| Edit a row → status REVOKED | Permanently retire the key (heartbeat returns 410). |

Every license/billing action writes an append-only **ControlEvent** /
**Payment** row for audit. A daily `python manage.py bill_subscriptions` cron
keeps offline installs' billing state current in the dashboard.

---

## 7. Perpetual unlock (vendor shutdown escape hatch)

If you ever shut the business down, customers must not be bricked. Publish a
signed unlock file. On an **air-gapped** machine:

```bash
cd pos_control_center
export LICENSE_VENDOR_PRIVATE_KEY=<hex from the vault>
DEBUG=True python manage.py generate_unlock
unset LICENSE_VENDOR_PRIVATE_KEY      # remove it immediately afterward
```

Publish the printed base64 blob on your status page. Any operator pastes it
into `POST /api/licensing/unlock` (`{ "unlock_file": "<blob>" }`); their install
verifies it against the embedded public key, flips to `PERPETUAL_UNLOCK`, and
stops phoning home. The file is **not** tenant-bound — one file unlocks every
install, by design.

---

## 8. Verify the two work together

An end-to-end script boots both servers and walks the whole lifecycle
(register → heartbeat → suspend → resume → banner → offline grace → bad/good
unlock → audit trail):

```bash
cd alpha_pos
.venv/bin/python licensing/scripts/e2e_verify.py
# expect: PASSED: 17  FAILED: 0
```

Quick manual smoke test:

```bash
curl https://control.example.com/healthz                      # -> ok
curl https://<pos-host>/api/licensing/status                  # -> {"status":"UNREGISTERED", ...} before setup
```

Run the unit suites:

```bash
cd pos_control_center && DEBUG=True pytest -q          # 34 passed
cd alpha_pos        && DEBUG=True pytest -q            # 267 passed
```

---

## 9. Configuration reference

### pos_control_center

| Env var | Required | Notes |
|---|---|---|
| `DEBUG` | — | `False` in production. |
| `SECRET_KEY` | when DEBUG=False | boot fails without it. |
| `ALLOWED_HOSTS` | when DEBUG=False | comma-separated. |
| `CSRF_TRUSTED_ORIGINS` | for HTTPS admin behind proxy | e.g. `https://control.example.com`. |
| `DB_ENGINE` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` | for Postgres | blank `DB_ENGINE` → SQLite. |
| `TRUST_FORWARDED_PROTO` | behind TLS proxy | trusts `X-Forwarded-Proto`. |
| `SECURE_SSL_REDIRECT` | optional | redirect HTTP→HTTPS at the app. |
| `LICENSE_VENDOR_PUBLIC_KEY` | optional | for dashboard display. |
| `LICENSE_VENDOR_PRIVATE_KEY` | only when signing | set briefly for `generate_unlock`, then unset. |
| `CLICK_SERVICE_ID` / `CLICK_MERCHANT_ID` / `CLICK_SECRET_KEY` | for Click.uz top-ups | merchant credentials. |
| `PAYME_MERCHANT_KEY` | for Payme.uz top-ups | Paycom merchant key (Basic-auth password). |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | for warning emails | leave `EMAIL_HOST` blank to print to stdout (console backend). |
| `EMAIL_USE_TLS` / `EMAIL_USE_SSL` / `EMAIL_TIMEOUT` | optional | TLS defaults to on, timeout 15s. |
| `DEFAULT_FROM_EMAIL` | optional | from-address on warning emails. |
| `DB_CONN_MAX_AGE` | optional | persistent DB connections (default 60s). |
| `GUNICORN_WORKERS` / `GUNICORN_TIMEOUT` / `LOG_LEVEL` | optional | runtime tuning. |

### alpha_pos (licensing)

| Env var | Required | Notes |
|---|---|---|
| `LICENSE_CONTROL_CENTER_URL` | yes | must be `https://` when DEBUG=False. |
| `LICENSE_FERNET_KEY` | production | pin it; rotating forces re-setup. |
| `LICENSE_VENDOR_PUBLIC_KEY` | for unlock | the public hex from step 2. |
| `LICENSE_HEARTBEAT_INTERVAL` | optional | seconds, default 300. |
| `LICENSE_GRACE_DAYS` | optional | offline grace, default 7. |
| `LICENSE_HEARTBEAT_DISABLED` | dev only | skip the sidecar daemon. |

---

## 10. Security notes

- The control center stores only `sha256(key)` + an 8-char prefix — never the
  cleartext. A lost key means revoke + reissue; there is no recovery.
- alpha_pos stores the key **encrypted** (Fernet) at rest. In production set
  `LICENSE_FERNET_KEY` explicitly; the dev fallback derives it from
  `SECRET_KEY`, which breaks if `SECRET_KEY` rotates.
- Keep `LICENSE_CONTROL_CENTER_URL` on HTTPS — a MITM on plaintext could rewrite
  heartbeat responses to keep returning ACTIVE forever. alpha_pos refuses to
  boot with a non-HTTPS URL when DEBUG=False.
- The vendor private key is the master control over the whole install base.
  Keep it offline; load it only to sign an unlock file.
