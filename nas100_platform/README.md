# NAS100 ICT Sniper — Platform

A multi-tenant NAS100 ICT/SMC signal dashboard: anyone can create an
account, connect their own Liquid Charts credentials, and use their own
strategy settings — all on one deployed website. **The app never places
an order automatically.** Every trade requires an explicit, authenticated
click from the account owner it belongs to, using that account's own
broker credentials.

## 1. Architecture at a glance

- **Auth:** email/password accounts, bcrypt-hashed passwords, sessions
  via httpOnly cookies.
- **Per-user data:** strategy settings and broker credentials live in a
  database (Postgres in production, SQLite for local dev), scoped to
  each account — see the security section below for how isolation is
  enforced and verified.
- **Backtests:** run as background jobs with polling, so one user's
  backtest can't stall the site for everyone else.
- **Strategy engine:** `smc_ict.py` (SMC/ICT confluences), `backtest_engine.py`,
  `news.py` (economic calendar awareness), and `liquidcharts_client.py`
  (the broker REST client) hold the actual trading logic — everything
  else in this project is the multi-tenant web app wrapped around them.

## 2. Local development

```bash
cd nas100_platform
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example app/.env
```

Generate the two required secrets and put them in `app/.env`:
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"                      # JWT_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # CREDENTIAL_ENCRYPTION_KEY
```

Leave `DATABASE_URL` blank to use a local SQLite file (`app/app.db`) —
fine for development. Then run:

```bash
cd app
python -m uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — you'll land on the dashboard, which
redirects to `/login.html` if you're not signed in yet. Sign up, then
go to **Settings** to add broker credentials (or just leave dry-run mode
on — new accounts default to dry-run automatically).

To develop against Postgres locally instead of SQLite (closer to
production), use `docker compose up --build` from the project root
instead — see `docker-compose.yml`.

## 3. Deploying

This needs a platform that runs a **persistent process**, not
serverless functions — see the note below on why. Railway or Render are
the simplest options (both do managed Postgres + a web service in one
project); Fly.io or any VPS work too since it's just a Docker container.

**Railway (or similar) steps:**
1. Push this project to a git repo, connect it to Railway.
2. Add a Postgres database in the same project — Railway will inject
   `DATABASE_URL` automatically (or copy the connection string it gives
   you into your service's env vars if it doesn't auto-link).
3. Set environment variables on the web service: `JWT_SECRET`,
   `CREDENTIAL_ENCRYPTION_KEY` (generate both as shown above — do this
   once and keep them somewhere safe, see the warning below),
   `COOKIE_SECURE=true` (you're on HTTPS in production), `SESSION_HOURS`
   if you want something other than the 168-hour default.
4. Deploy. Railway will build from the `Dockerfile` automatically.
5. Point your domain at it (or use the platform-provided URL) and you're live.

**Why not Vercel:** this app keeps real state between requests
(sessions, background backtest jobs, broker sessions) and needs a
persistent database connection and background threads — none of that
fits Vercel's serverless function model without a substantial rewrite
(external job queue, connection pooling service, etc.). A normal
persistent web service is simpler and a better fit here.

**Back up your secrets.** `CREDENTIAL_ENCRYPTION_KEY` encrypts every
user's stored broker password. If you lose it, every stored credential
becomes permanently undecryptable (users would need to re-enter them).
Store it in a password manager or your hosting platform's secret store
— not just in a `.env` file on one machine.

## 4. Security model — what's actually protecting people's accounts

- **Passwords:** hashed with bcrypt (via `passlib`), never stored or logged in plaintext.
- **Broker credentials:** encrypted at rest with Fernet (`cryptography`
  library) using a server-side master key (`CREDENTIAL_ENCRYPTION_KEY`).
  Verified directly against the database in testing — the stored
  columns are ciphertext, not plaintext.
- **Sessions:** JWT in an httpOnly, SameSite=Lax cookie (`Secure` too
  when `COOKIE_SECURE=true`). httpOnly means client-side JS/an XSS
  payload can't read the token; SameSite=Lax mitigates most CSRF for a
  same-origin app like this.
- **Cross-user isolation:** every query that touches user-specific data
  (settings, broker credentials, signals, backtest jobs) is scoped by
  the authenticated user's own id from the session — verified directly:
  one test account could not read another's settings, poll another's
  backtest job id, or confirm a trade using another user's signal id
  (all returned 404/409 as expected).
- **Rate limiting:** signup/login are rate-limited per IP (`slowapi`) to
  slow down credential-stuffing/brute-force attempts — verified the
  limiter actually returns 429 past the threshold.
- **No trading without an explicit click:** `/api/confirm` only acts on
  the signal currently cached for *that* user, and only places an order
  through *that* user's own decrypted broker credentials. There's no
  code path — scheduled job, admin action, or otherwise — that places an
  order without this specific authenticated request.
- **XSS defense-in-depth:** text sourced from outside your own server
  (news calendar titles, broker position fields) is HTML-escaped before
  being inserted into the page, in case an upstream feed is ever
  compromised or returns something unexpected.

**What this is *not*:** a full security audit, and not a substitute for
one before you handle real money at scale. If this grows to real
users/real capital, get an actual third-party security review, add
proper key management (KMS instead of a raw env var), and consider a
managed auth provider if you want to offload session/password security
entirely.

## 5. Known gaps / next steps

- **No email verification or password reset.** Signup just needs an
  email + password; there's no confirmation email flow and no "forgot
  password" — both need an email-sending service (SMTP/Resend/SendGrid)
  wired in. Users who forget their password currently have no
  self-service way back in.
- **No billing.** By design for this version (you asked to hold off) —
  Stripe (or similar) is the natural next addition once you want
  subscriptions.
- **No database migrations.** Tables are created with
  `Base.metadata.create_all()` on startup — fine while the schema is
  new, but add Alembic before you have real user data you can't afford
  to lose to a schema change.
- **No per-user alerting.** By design for this version — the dashboard
  is pull-based (open it to see the current signal). Adding
  email/push/SMS alerts when a signal fires means a background loop per
  user watching the market continuously, which is a real infrastructure
  addition (and cost, if you're polling many users' worth of market
  data on a schedule).
- **Single-broker (Liquid Charts).** Also by design for this version —
  generalizing to other brokers means an adapter interface and a
  broker-selection step in onboarding.
- **Basic key management.** See the security section above — a raw env
  var for `CREDENTIAL_ENCRYPTION_KEY` is a reasonable v1, not where
  you'd want to stay long-term with many real users.
- **No admin tooling.** No way to view/disable users, see aggregate
  usage, etc., outside of querying the database directly.

## 6. Files

```
nas100_platform/
  app/
    main.py                 # FastAPI app, mounts routers + static files
    config.py                 # server secrets from env (JWT, encryption key, DB URL)
    db.py                      # SQLAlchemy engine/session
    models.py                  # User, UserSettings, BrokerCredential, LastSignal, SignalLog, BacktestJob
    schemas.py                  # Pydantic request/response models
    auth.py                      # password hashing, JWT cookies, current_user dependency
    crypto.py                     # Fernet encrypt/decrypt for stored broker credentials
    rate_limit.py                  # shared slowapi limiter
    data_source.py                  # per-user dry-run simulator / live client resolution
    routers/
      auth.py                        # signup, login, logout, change password
      settings.py                     # strategy settings + broker credential CRUD + go-live toggle
      trading.py                       # /api/signal, /api/confirm, /api/positions, /api/news
      backtest.py                       # background-job backtest start/poll
    smc_ict.py, backtest_engine.py, candle_utils.py, news.py,
    data_import.py, liquidcharts_client.py   # strategy engine, backtester, news, and broker client
  static/
    login.html, signup.html, settings.html, index.html, backtest.html
    style.css, js/api.js
  requirements.txt
  Dockerfile, docker-compose.yml, .dockerignore
  .env.example
```

## 7. Verification performed

Ran locally end-to-end before delivery: two independent test accounts
signing up, updating settings, saving/testing/deleting broker
credentials, fetching signals in dry-run, confirming a simulated trade,
and running a full backtest job through to completion — plus explicit
cross-user isolation checks (wrong user can't read another's settings,
poll their backtest job, or confirm using their signal id), rate-limit
threshold behavior, cookie flags, and confirming credentials/passwords
are stored encrypted/hashed rather than in plaintext by inspecting the
database directly. Docker build itself wasn't tested (no Docker
available in this environment) — the app was verified with the same
run command the Dockerfile uses, but do a test deploy before pointing
real users at it.
