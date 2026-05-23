# pos_control_center

Central control plane for **alpha_pos** installs. Hosted by the vendor;
each restaurant's POS phones home here to confirm its license is still
valid. See the implementation plan at
`alpha_pos/.claude/plans/kind-gliding-kay.md` for the full design.

## What lives here

- **`tenants/`** — `Tenant` (one row per signed restaurant) and
  `InviteCode` (one-shot tickets staff issue from the Django admin).
- **`licenses/`** — `LicenseKey` (sha256-stored bearer tokens). The
  cleartext is returned exactly once at registration; lose it and the
  vendor must revoke + reissue.
- **`/api/v1/register`** — exchanges an invite code for a fresh license
  key (the alpha_pos setup wizard calls this).
- **`/admin/`** — Django admin is the vendor dashboard for v1. Custom
  bulk actions (suspend, resume, extend expiry, set banner message,
  generate perpetual unlock) come in follow-up commits.

Heartbeat endpoint, perpetual-unlock generator, and admin actions are
listed on the plan but not in this initial scaffold.

## Running locally

```bash
pip install -r requirements-dev.txt
export DEBUG=True SECRET_KEY=dev-key
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 9000
```

Then open `http://127.0.0.1:9000/admin/`, create an `InviteCode`, and
hand its `code` value to an alpha_pos install for the setup wizard.

## Running tests

```bash
DEBUG=True pytest -q
```

## Why a separate repo / project

alpha_pos ships to every restaurant; this control center is yours
alone. Keeping them apart means customers' deployments don't carry
your admin code, and you can iterate on the dashboard without
forcing them to upgrade their POS.
