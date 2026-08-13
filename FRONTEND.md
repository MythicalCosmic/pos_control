# POS Control Center — Frontend Guide

This is the **vendor dashboard** for Alpha POS: where *you* (the vendor) manage the
restaurants running Alpha POS — their licenses, kill-switch, subscriptions, billing,
and invites. One control center serves every install (cloud + every till).

> Status: the install-facing API, payment webhooks, Django admin, and the bearer-token
> vendor API under `/api/admin/` are implemented. CORS allows all origins only
> in development; production origins must be listed in `CORS_ALLOWED_ORIGINS`.

## Core concepts (the data model)

- **Tenant** — one restaurant/business (natural key = email). Has a prepaid `balance`,
  one **Subscription**, many **LicenseKeys**, many **InviteCodes**.
- **LicenseKey** — one per *install* (the cloud + each till of a tenant hold their own).
  `status` ∈ `ACTIVE | SUSPENDED | REVOKED`, `expires_at`, `message` (banner pushed to
  the POS), `last_heartbeat`.
- **Account model:** the first install redeems an **InviteCode** (creates the tenant);
  additional installs "log in" with the same email — no new code. **Suspending a tenant
  suspends ALL its keys** (the kill-switch hits cloud + every till at once).
- **Subscription / SubscriptionPlan** — prepaid billing: a plan charges `price` every
  `period_days` from the tenant `balance`; when it can't, heartbeat reports EXPIRED and
  the POS kill-switch fires. `warn_days`/`grace_days` soften the edge.
- **InviteCode** — one-shot ticket (`intended_email`, `expires_at`, `consumed_at`).
- **HeartbeatEvent** — every install check-in (version, branch, fingerprint, ip, time).
- **ControlEvent** — audit trail (who suspended/resumed/messaged what, when).
- **Payment** — top-up ledger (Click.uz / Payme.uz).

## Pages to build (~9)

1. **Login** — vendor auth (token). One screen.
2. **Overview** — KPI tiles: tenants, active installs, suspended, expiring-soon, balance/
   revenue; "installs gone quiet" (no recent heartbeat); recent ControlEvents feed.
3. **Tenants** — searchable list (org / email), status chips, create-tenant + issue-invite.
4. **Tenant detail** — the workhorse: profile + balance; its license keys (status, last
   heartbeat) with per-key Suspend/Resume/Revoke + set-banner; **Suspend ALL / Resume ALL**
   (account kill-switch); subscription (plan, paid-through, status); invites;
   recent heartbeats + events.
5. **Licenses** — all keys across tenants, filter by status, bulk Suspend/Resume.
6. **Invites** — list (unused / consumed / expired), create (email, org, expiry), revoke.
7. **Billing & Subscriptions** — per-tenant subscription + plan-change requests to approve/
   reject; read-only payment ledger.
8. **Plans** — CRUD subscription plans (code, name, price, period_days, warn/grace, active).
9. **Audit / Heartbeats** — ControlEvent log + a live HeartbeatEvent stream (install health).

(Optional **Settings**: vendor Ed25519 keypair for offline "unlock" files, payment-provider
keys — these can stay in Django admin for v1.)

## Vendor API contract (implemented under `/api/admin/`)

All routes below require `Authorization: Bearer <vendor-access-token>` and return JSON.
Mutations accept `Idempotency-Key`. Access tokens live for 15 minutes by default and
refresh tokens rotate on use.

```
POST   /api/admin/auth/login|refresh|logout
GET    /api/admin/auth/me
GET    /api/admin/overview                      -> KPI counts + recent events
GET    /api/admin/tenants?q=&status=            -> paginated tenants
POST   /api/admin/tenants                       -> create tenant
GET    /api/admin/tenants/{id}                  -> tenant + keys + subscription + invites
PATCH  /api/admin/tenants/{id}                  -> edit (org_name, notes)
POST   /api/admin/tenants/{id}/suspend          -> SUSPEND ALL keys (account kill-switch)
POST   /api/admin/tenants/{id}/resume           -> RESUME ALL keys
GET    /api/admin/licenses?status=&health=&tenant= -> license keys
POST   /api/admin/licenses/{id}/suspend|resume|revoke
POST   /api/admin/licenses/{id}/message         -> {message} banner shown on the POS
DELETE /api/admin/licenses/{id}/message         -> clear banner
POST   /api/admin/licenses/bulk-action
GET/POST /api/admin/invites ; GET/DELETE /api/admin/invites/{id}
GET    /api/admin/plans ; POST/PATCH/DELETE     -> subscription plans
GET    /api/admin/subscriptions ; GET/PATCH /api/admin/subscriptions/{tenant}
GET    /api/admin/plan-changes?status=PENDING
POST   /api/admin/plan-changes/{id}/approve|reject
GET    /api/admin/payments ; /api/admin/payments/summary
GET    /api/admin/heartbeats?tenant= ; /api/admin/events?tenant=
GET    /api/admin/system/status
```

Manual browser/admin credits are intentionally unavailable. Click.uz and Payme.uz are
the only wallet-credit rails; payment rows and control events remain append-only.

(The per-tenant suspend/resume already exists in the Django admin as the
`suspend_tenant`/`resume_tenant` actions — the API just exposes the same logic.)

## Conventions
- **Auth:** Bearer token. (CORS is allow-all + credentials OFF, so use tokens, not cookies.
  For cookie/session auth, switch CORS to an explicit origin + credentials — see settings.)
- **Base URL (prod):** `https://control.<server-ip>.nip.io`
- **Money:** decimals as strings ("200000.00"); UZS (so'm).
- **Health:** `GET /healthz` -> `ok`.
