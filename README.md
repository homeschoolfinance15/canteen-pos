# Canteen POS

A production-oriented canteen point-of-sale app for a camp or small store.

Architecture:

- React + Vite frontend
- FastAPI backend
- Postgres database
- HttpOnly cookie sessions
- Role-based permissions for cashier and manager
- Immutable transaction ledger for purchases, balance edits, inventory changes, and imports

## Roles

- `cashier`: run purchases from the register.
- `manager`: cashier access plus account balance controls, inventory editing, worker management, backup, restore, and clearing data.

The backend enforces permissions on every protected endpoint. The frontend only hides UI that the current worker should not use.

## Required Environment Variables

```text
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DB
SESSION_SECRET=<long random secret>
COOKIE_SECURE=1
REPORT_TIME_ZONE=America/New_York
INITIAL_MANAGER_EMAIL=manager@example.com
INITIAL_MANAGER_PASSWORD=<strong temporary password>
INITIAL_MANAGER_NAME=Manager
```

For local HTTP development, use `COOKIE_SECURE=0`.

On first boot, if the `users` table is empty, the app creates the initial manager from the `INITIAL_MANAGER_*` variables. The older `INITIAL_OWNER_*` names are still accepted for existing deployments.

Daily reports are grouped from local 12:00 AM to the next 12:00 AM using `REPORT_TIME_ZONE`.

## Local Development

Install frontend dependencies:

```sh
npm install
```

Install backend dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Start FastAPI:

```sh
DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/canteen' \
SESSION_SECRET='replace-with-output-from-python-secrets-token-urlsafe' \
COOKIE_SECURE=0 \
INITIAL_MANAGER_EMAIL='manager@example.com' \
INITIAL_MANAGER_PASSWORD='use-a-strong-temporary-password' \
uvicorn server:app --host 127.0.0.1 --port 8000
```

Generate a local `SESSION_SECRET` with:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Start Vite in another terminal:

```sh
npm run dev
```

Open the Vite URL, usually:

```text
http://127.0.0.1:5173
```

Vite proxies `/api` to `http://127.0.0.1:8000`.

## Production Docker

Build:

```sh
docker build -t canteen-pos .
```

Run:

```sh
docker run -p 8000:8000 \
  -e DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DB' \
  -e SESSION_SECRET='use-a-long-random-secret' \
  -e COOKIE_SECURE=1 \
  -e INITIAL_MANAGER_EMAIL='manager@example.com' \
  -e INITIAL_MANAGER_PASSWORD='change-this-password' \
  canteen-pos
```

Recommended hosts:

- Render, Railway, or Fly.io for the Docker web service
- Managed Postgres from the host, Supabase, or Neon

## Render Blueprint

This repo includes `render.yaml` for a Docker web service and managed Postgres database on Render.

During Blueprint creation, fill in:

- `INITIAL_MANAGER_EMAIL`
- `INITIAL_MANAGER_PASSWORD`

Render generates `SESSION_SECRET`, sets `COOKIE_SECURE=1`, and wires `DATABASE_URL` from the managed Postgres database.

## Data Model

Main tables:

- `users`: workers, roles, password hashes, active status
- `accounts`: customer/camper balances
- `items`: catalog and inventory
- `transactions`: immutable audit ledger
- `sale_lines`: per-item detail for purchases

Purchases run inside a database transaction. The server locks the account and items, checks balance and stock, updates inventory and account balance, writes the purchase ledger row, and writes sale lines.

Account names and product names must be unique within their own lists, ignoring capitalization. On startup, if duplicate accounts or products already exist, the oldest row is kept and later duplicates are deleted before unique indexes are enforced.

## Backup And Restore

Managers can download a JSON backup from the Data screen. The backup includes accounts, items, ledger rows, sale lines, and worker records.

Restore validates the JSON shape before replacing account, item, and ledger data. It preserves the currently logged-in worker as an active manager to prevent accidental lockout.

## Security Notes

- Do not expose the Vite development server directly to the internet; deploy the FastAPI app with the built frontend.
- Use HTTPS in production and set `COOKIE_SECURE=1`.
- Set a long random `SESSION_SECRET`; the app refuses the built-in development secret and uses only a temporary random secret if none is configured.
- Set a strong `INITIAL_MANAGER_PASSWORD` before the first startup; the app refuses to create the initial manager with the default password.
- Keep manager accounts limited to trusted workers. Cashiers can make purchases, while managers can change balances, inventory, workers, and data.
- After deploying, run `BASE_URL=https://your-app.example.com npm run security:no-auth` to confirm anonymous users cannot access protected API endpoints.

## Deployment Smoke Test

After deploying:

1. Log in as the initial manager.
2. Create a manager or cashier worker.
3. Create one account and one product.
4. Complete a sale.
5. Refresh the app and confirm the balance, inventory, and ledger persisted.
6. Restart the service and confirm the same data is still present.
