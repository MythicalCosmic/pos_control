# How `pos_control_center` and `alpha_pos` Work Together in Production

A one-pager for anyone who needs to understand the system at a glance —
operators, stakeholders, security reviewers, new engineers.

For deploy steps, env vars, and runbooks see
[`OPERATIONS_GUIDE.md`](OPERATIONS_GUIDE.md).

---

## TL;DR

Two Django apps, one paying customer:

- **`pos_control_center`** — the vendor's central server. Issues license
  keys, holds the prepaid-billing wallet, runs the suspend/resume/banner
  controls, publishes the subscription plan catalog. **You run one of
  these.** Authoritative for every dollar and every license decision.
- **`alpha_pos`** — the actual point-of-sale shipped to each restaurant.
  Phones home every 5 minutes, enforces a kill switch when the control
  center says stop, displays the operator-facing UI. **The restaurant
  runs one of these per site.**



The two communicate over HTTPS with HMAC-signed responses. The control
center is the only writable side; alpha_pos is a strict read-and-enforce
client.

> **Both projects are backend-only — no UI ships in either repo.**
>
> - `pos_control_center` exposes a pure JSON REST API. Its only rendered
>   surface is the auto-generated Django admin (built into Django itself,
>   not custom code), which the vendor uses as the operational dashboard.
> - `alpha_pos` exposes a pure JSON REST API too. The setup wizard,
>   settings screen, plan picker, balance banner, etc. all live in the
>   **separate frontend project** (the Electron renderer / POS app)
>   which consumes these JSON endpoints.
>
> Wherever this document says "the wizard does X" or "the settings screen
> shows Y," that means the separate frontend calls our JSON endpoint and
> renders the result — there is no HTML in this backend that does it.

---

## 1. The bird's-eye picture

```
   ┌─────────────────────────────────────────────────────────────┐
   │              pos_control_center (vendor VPS)                │
   │                                                             │
   │   Django admin          Django REST API                     │
   │   ┌──────────┐          ┌──────────────────────┐            │
   │   │Plans     │          │ GET  /api/v1/plans   │ ← wizard   │
   │   │Invites   │          │ POST /api/v1/register│ ← wizard   │
   │   │Tenants   │          │ POST /api/v1/heartbt │ ← daemon   │
   │   │Subs      │          │ POST /api/v1/plan-ch │ ← settings │
   │   │Payments  │          │                      │            │
   │   │PlanChgRq │          │ POST /api/billing/   │ ← Click.uz │
   │   │LicKeys   │          │      click/*         │   Payme.uz │
   │   │Heartbts  │          │ POST /api/billing/   │            │
   │   │CtrlEvts  │          │      payme           │            │
   │   └──────────┘          └──────────────────────┘            │
   │                                                             │
   │   Daily cron: python manage.py bill_subscriptions           │
   │   (settles every active sub; emails warn/grace/lockout)     │
   └─────────────────────────────────────────────────────────────┘
                              ▲ HTTPS, TLS verified
                              │ Bearer <license key> per heartbeat
                              │ HMAC-SHA256(key, body) on every response
                              │
   ┌──────────────────────────┴───────────────────────────────────┐
   │           alpha_pos install (one per restaurant)             │
   │                                                              │
   │   Setup wizard       LicenseEnforcementMiddleware            │
   │   ┌──────────────┐   ┌────────────────────────────┐          │
   │   │GET /licensing│   │503 unless state is ACTIVE  │          │
   │   │    /plans    │   │allowlist: /healthz,        │          │
   │   │POST .../setup│   │           /api/licensing/* │          │
   │   │POST .../plan-│   └────────────────────────────┘          │
   │   │    change    │                                           │
   │   │GET .../status│   heartbeat_daemon (sidecar process)       │
   │   └──────────────┘   ┌─────────────────────────┐             │
   │                      │posts /heartbeat every    │             │
   │                      │LICENSE_HEARTBEAT_INTERVAL│             │
   │                      │(300s) — verifies HMAC,   │             │
   │                      │anchors clock to server   │             │
   │                      └─────────────────────────┘             │
   └──────────────────────────────────────────────────────────────┘
```

---

## 2. The end-to-end customer lifecycle

The picture in chronological order. Times are realistic for a single
restaurant on a 30-day plan.

### Day −7 — Vendor onboarding work

| Step | Where | What |
|---|---|---|
| Publish plans | `/admin/billing/subscriptionplan/` | One row per pricing tier (Basic, Pro, Enterprise). Sets price, period_days, warn_days, grace_days. |
| Issue invite | `/admin/tenants/invitecode/` | Set `intended_email` to the restaurant's contact address. Save. (The generated `code` is never sent to the customer.) |

### Day 0 — Customer setup wizard

| t | Side | What happens |
|---|---|---|
| 0s | alpha_pos | Operator opens setup wizard, sees plan picker |
| 0s | alpha_pos→cc | `GET /api/v1/plans` (no auth — public-read catalog) |
| 1s | cc→alpha_pos | List of active plans with prices |
| 5s | operator | Types email, picks "Basic", submits |
| 5s | alpha_pos→cc | `POST /api/v1/register {"email":..., "plan_id":1}` |
| 5s | cc | Looks up unconsumed invite where `intended_email == email`. None? → 403. Found? → consume it, create Tenant + Subscription (bound to chosen plan), issue LicenseKey, **return cleartext key once**. |
| 6s | alpha_pos | Encrypt key (Fernet), store, flip `License.status = ACTIVE`. Kill switch clears. |
| 6s | operator | Business endpoints respond; restaurant can sell. |

### Day 0 → Day N — Steady-state operation

| Every 300s | Side | What |
|---|---|---|
| | alpha_pos→cc | `POST /api/v1/heartbeat` with bearer key |
| | cc | Settle subscription (charge `price` if `paid_through` lapsed AND balance covers it). Compute status: ACTIVE / EXPIRED / SUSPENDED. |
| | cc→alpha_pos | JSON response signed with `X-Response-Signature: sha256=HMAC(key, body)` |
| | alpha_pos | Verify HMAC. Update License row with status, expires_at (= paid_through), balance, days_remaining, warn, in_grace, plan, pending_plan_change. Anchor `last_heartbeat_at` to control center's `server_now` (not local clock). |

### Customer tops up

| t | Side | What |
|---|---|---|
| | customer | Pays via Click.uz / Payme.uz on the vendor's payment page |
| | provider→cc | Webhook `/api/billing/click/complete` or `/api/billing/payme` (PerformTransaction) |
| | cc | Verify signature (Click MD5 / Payme Basic auth + idempotency on transaction id). Credit wallet. Settle subscription (advances `paid_through` if a period was overdue). |
| | next heartbeat | alpha_pos sees `status=ACTIVE`, `balance` updated, kill switch clear if it had fired |

### Customer is running out of money

```
paid_through                                              now
     │                                                    │
─────┼─────────── period (30 days) ───────────────────────┤
     │                                                    │
     │   ◄── warn_days (3) ──►       │
     │                       ▲       │
     │            "Top up soon" email fires (warn=True)   │
     │            heartbeat: status=ACTIVE warn=True      │
     │                                                    │
                                                          │  ◄─ grace_days (3) ─►
                                                          ▼                      ▼
                                              heartbeat: status=ACTIVE       heartbeat:
                                              in_grace=True warn=True        status=EXPIRED
                                              "Payment overdue" email        in_grace=False
                                                                            "Service paused" email
                                                                             kill switch fires (503)
```

### Customer requests plan change

| t | Side | What |
|---|---|---|
| | customer | Settings screen → picks "Pro" → submits |
| | alpha_pos→cc | `POST /api/v1/plan-change {"plan_id":2}` with bearer key |
| | cc | Queues a `PlanChangeRequest` (status=PENDING). One PENDING per tenant — concurrent retries collapse to the same row. |
| | next heartbeat | Response carries `pending_plan_change.requested_plan = Pro` → renderer shows "Plan change pending vendor approval" badge |
| | vendor | `/admin/billing/planchangerequest/` → select rows → **Approve plan change** action |
| | cc | Swaps Subscription onto new plan, copies its pricing in. Current `paid_through` stays (customer already paid for this period). |
| | next heartbeat | `plan=Pro`, `pending_plan_change=None`. Next settle uses Pro's price. |

---

## 3. Wire protocol — what crosses the network

Every byte that flows between the two projects:

### Control-center endpoints

| Method | Path | Auth | Used by | Purpose |
|---|---|---|---|---|
| GET | `/healthz` | none | uptime probes | "still alive" |
| GET | `/api/v1/plans` | none | wizard | catalog of active plans |
| POST | `/api/v1/register` | none (one-shot invite) | wizard | redeem invite → cleartext license key |
| POST | `/api/v1/heartbeat` | Bearer <license key> | daemon | periodic phone-home; response is HMAC-signed |
| POST | `/api/v1/plan-change` | Bearer <license key> | settings screen | file a plan-change request for vendor approval |
| POST | `/api/billing/click/prepare` | Click signature | Click.uz | reserve top-up amount |
| POST | `/api/billing/click/complete` | Click signature | Click.uz | finalize top-up → credit wallet |
| POST | `/api/billing/payme` | HTTP Basic (merchant key) | Payme.uz | JSON-RPC: Create/Perform/Cancel/Check transactions |
| GET / POST | `/admin/...` | session (staff login) | vendor | Django admin |

### Alpha_pos endpoints (relevant to licensing)

All JSON. The frontend project (Electron renderer / POS app) is the one
that calls these.

| Method | Path | Auth | Called by | Purpose |
|---|---|---|---|---|
| GET | `/healthz` | none | uptime probes | "still alive" |
| GET | `/api/licensing/status` | none | frontend (any screen) | current license snapshot (always 200, even when blocked) |
| GET | `/api/licensing/plans` | none (rate-limited) | frontend wizard screen | proxies control center's plan catalog |
| POST | `/api/licensing/setup` | none (rate-limited) | frontend wizard screen | email-only onboarding → relays to `/api/v1/register` |
| POST | `/api/licensing/plan-change` | none (bearer is internal) | frontend settings screen | relays to `/api/v1/plan-change` |

Everything else on alpha_pos goes through `LicenseEnforcementMiddleware`
and returns **503** with a structured body when the kill switch is on.

### Heartbeat response shape (the wire contract)

```json
{
  "success": true,
  "status": "ACTIVE",
  "expires_at": "2026-06-15T12:00:00+00:00",
  "server_now": "2026-05-31T09:30:00+00:00",
  "next_heartbeat_in_s": 300,
  "message": null,
  "ack_id": "8c0b9a30-21b9-4f7c-...",
  "balance": "250.00",
  "days_remaining": 15,
  "warn": false,
  "in_grace": false,
  "plan": {
    "id": 1, "code": "basic", "name": "Basic",
    "price": "100.00", "period_days": 30,
    "warn_days": 3, "grace_days": 3
  },
  "pending_plan_change": null
}
```

Header: `X-Response-Signature: sha256=<hex>` — HMAC-SHA256 of the
canonical-JSON serialization of the body, keyed on the bearer license key.

---

## 4. Security model

Four layers of defense on the heartbeat path. Each is independent — an
attacker has to defeat all of them, not any one.

| Layer | What it stops | Failure surfaces as |
|---|---|---|
| **TLS** with `verify=True` (system trust store or `LICENSE_TLS_CA_BUNDLE` for private CAs) | classic MITM with a swapped cert | `SSLError` in heartbeat logs |
| **Runtime HTTPS check** on every heartbeat (refuses plaintext URL in production) | env var being mutated to `http://...` post-boot | `503 control_center_url_must_be_https` |
| **HMAC signature** on every heartbeat response, keyed on the bearer license key | MITM that has bypassed TLS forging `{"status":"ACTIVE"}` | `502 response_signature_invalid`; License row unchanged so grace clock keeps ticking |
| **`server_now` clock anchor** — `last_heartbeat_at` is the control center's clock, not the local wall clock | operator winding host clock forward to fake heartbeats and extend `grace_until` by decades | silent (the protection just kicks in) |
| **Fernet decrypt → SUSPENDED** — if `LICENSE_FERNET_KEY` rotates or the encrypted blob is corrupt, the license flips SUSPENDED immediately | install drifting ACTIVE for days after a key rotation | License.status = SUSPENDED with `last_message` "License key cannot be decrypted" |

Additional surface-level protections:

- License keys are stored **only as `sha256(key)` + an 8-char prefix** on the
  control center. The cleartext is returned exactly once at registration.
  Lost keys are unrecoverable — revoke + reissue.
- The control center has **no manual "Add credit" form**. Every soum that
  enters a wallet flows through Click.uz / Payme.uz webhooks with
  idempotency keys (`Payment.external_id`), bounded by `MAX_CREDIT_AMOUNT`
  (1 B so'm), recorded in the append-only `Payment` ledger.
- Request bodies on `/register` and `/heartbeat` are **capped at 8 KB** —
  413 if exceeded.
- Heartbeat replay protection: responses with `server_now` older than the
  newest already-applied one are refused.
- Admin actions write **append-only `ControlEvent` audit rows**.
- One PENDING `PlanChangeRequest` per tenant at a time, DB-enforced
  (`one_pending_plan_change_per_tenant` partial unique constraint) — so a
  hammering customer can't flood the vendor approval queue.

---

## 5. What "production" means for each side

### Control center (one VPS, behind a TLS-terminating reverse proxy)

```
[ Internet ]
     │
     ▼
[ Caddy / nginx (TLS) ]   ── handles TLS, sets X-Forwarded-Proto
     │
     ▼
[ gunicorn 3 workers ]    ── pos_control_center
     │            │
     ▼            ▼
[ Postgres ]   [ stdout → journald → log aggregator ]
     │
     ▼
[ daily cron: bill_subscriptions ]   ── settles + emails
```

Required in `.env`:
- `DEBUG=False`, real `SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`
- Postgres credentials
- `TRUST_FORWARDED_PROTO=True` (reverse-proxy setup)
- Payment provider creds: `CLICK_*`, `PAYME_MERCHANT_KEY`
- Email SMTP: `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`

### Alpha_pos (one per restaurant — Docker on a small box)

```
[ POS terminal browser / Electron renderer ]
     │
     ▼
[ alpha_pos gunicorn ]   ── kill-switch middleware on every request
     │            │
     │            ▼
     │     [ heartbeat_daemon ]   ── sidecar process posting every 300s
     │            │
     ▼            ▼
[ Postgres ]   [ HTTPS → control center ]
```

Required in `.env`:
- `DEBUG=False`, real `SECRET_KEY`
- `LICENSE_CONTROL_CENTER_URL=https://control.example.com`
- `LICENSE_FERNET_KEY` (generate once, pin forever — rotation triggers
  fail-closed → SUSPENDED)
- Optional `LICENSE_TLS_CA_BUNDLE` for a private CA

---

## 6. Day-2 operations — what to do when X happens

| Symptom | Where to look | Fix |
|---|---|---|
| Restaurant says "POS won't take orders" | `/admin/licenses/licensekey/` → search by email | Check `status`. If SUSPENDED-by-admin: **Resume selected**. If EXPIRED: check `Subscription.paid_through` and `Tenant.balance`. |
| Customer paid but POS still EXPIRED | `/admin/billing/payment/` → filter by tenant | Confirm the Payment row landed. If yes, wait ≤5 min for next heartbeat. If no, check `/api/billing/click/*` or `/api/billing/payme` logs. |
| Customer wants to switch plan | `/admin/billing/planchangerequest/` | Find their PENDING row → **Approve plan change** action. |
| New restaurant signing up | `/admin/tenants/invitecode/add/` | Set `intended_email`. Done. Customer types that email in the wizard. |
| Vendor wants to retire a plan | `/admin/billing/subscriptionplan/` → edit → uncheck `is_active` | Existing subscribers unaffected. Plan vanishes from the wizard. |
| Suspicious heartbeat (HMAC failures spiking on one install) | alpha_pos logs: `heartbeat: response signature failed verification` | Either a MITM proxy with bad TLS, or the bearer key on that install doesn't match what the control center has → revoke + reissue. |
| Lost Fernet key on alpha_pos | License auto-flipped SUSPENDED | Re-run setup wizard (issue a fresh invite first). |
| Control center DB restored from backup | Some Subscriptions may have stale `paid_through` | Run `python manage.py bill_subscriptions` to resettle everyone. |

---

## 7. Verifying it all works

```bash
# Unit suites
cd pos_control_center && DEBUG=True .venv/bin/pytest -q   # 88 passed
cd alpha_pos          && DEBUG=True .venv/bin/pytest -q   # 264 passed

# End-to-end against both servers
cd alpha_pos && .venv/bin/python licensing/scripts/e2e_verify.py   # 17/0

# Bug-hunt simulation (32 scenarios across the integration)
cd pos_control_center && .venv/bin/python /tmp/bug_hunt.py         # 32/0
```

---

## 8. What's deliberately NOT in the build

So nobody hunts for them:

- **No customer-facing UI in either backend.** Both projects are JSON
  REST APIs only. The setup wizard, plan picker, balance banner,
  settings screen — all of that lives in the separate frontend project
  (Electron renderer / POS app) which calls these endpoints. The control
  center's only rendered surface is the Django admin, which is the
  vendor's internal dashboard, not anything the restaurant sees.
- **Perpetual-unlock escape hatch** (Ed25519 vendor signature). Removed
  from alpha_pos; the control center's `generate_unlock` /
  `generate_vendor_keypair` commands are orphans until the feature is
  redesigned end to end.
- **Manual admin "Add credit" form.** Removed — credits flow only through
  the Click/Payme webhooks so the money trail stays auditable on one rail.
- **Self-serve tenant signup.** Registration is gated by a vendor-issued
  `InviteCode`. No invite for that email → 403.

---

*Last verified: 2026-05-31 — 88 control-center tests, 264 alpha_pos tests,
17 e2e steps, 32 bug-hunt scenarios. All green.*
