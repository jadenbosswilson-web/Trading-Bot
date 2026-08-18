# NAS100 ICT Sniper — Platform

A multi-tenant NAS100 ICT/SMC signal dashboard: anyone can create an
account and use their own strategy settings — all on one deployed
website. **There is no broker connection and the app never places a
real order.** Market data comes from OANDA (if you configure a free
API token) or a delayed Yahoo Finance proxy, with a clearly-labeled
synthetic simulator as a last resort if neither is reachable.
"Confirm order" always just logs a paper trade for the account owner's
own tracking — execution happens manually, on whatever broker or
platform the user actually trades with.

## 1. Architecture at a glance

- **Auth:** email/password accounts, bcrypt-hashed passwords, sessions
  via httpOnly cookies.
- **Per-user data:** strategy settings live in a database (Postgres in
  production, SQLite for local dev), scoped to each account — see the
  security section below for how isolation is enforced and verified.
- **Market data:** shared across all users (it's public market data) —
  see `data_source.py` for the OANDA → Yahoo → synthetic fallback chain.
- **Backtests:** run as background jobs with polling, so one user's
  backtest can't stall the site for everyone else.
- **Strategy engine:** `smc_ict.py` (SMC/ICT confluences),
  `backtest_engine.py`, and `news.py` (economic calendar awareness) hold
  the actual trading logic — everything else in this project is the
  multi-tenant web app wrapped around them.

## 2. Local development

```bash
cd nas100_platform
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example app/.env
```

Generate the required secret and put it in `app/.env`:
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET
```

Leave `DATABASE_URL` blank to use a local SQLite file (`app/app.db`) —
fine for development. Optionally set `OANDA_API_TOKEN` (free with an
OANDA practice account) for genuinely real-time market data instead of
the delayed Yahoo proxy — see `.env.example`. Then run:

```bash
cd app
python -m uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — you'll land on the dashboard, which
redirects to `/login.html` if you're not signed in yet. Sign up and
you're straight into a live (paper-trading) dashboard — no broker setup
required.

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
3. Set environment variables on the web service: `JWT_SECRET` (generate
   as shown above and keep it somewhere safe), `COOKIE_SECURE=true`
   (you're on HTTPS in production), `SESSION_HOURS` if you want
   something other than the 168-hour default, and optionally
   `OANDA_API_TOKEN`/`OANDA_PRACTICE`/`OANDA_INSTRUMENT` for real-time
   data.
4. Deploy. Railway will build from the `Dockerfile` automatically.
5. Point your domain at it (or use the platform-provided URL) and you're live.

**Why not Vercel:** this app keeps real state between requests
(sessions, background backtest jobs) and needs a persistent database
connection and background threads — none of that fits Vercel's
serverless function model without a substantial rewrite (external job
queue, connection pooling service, etc.). A normal persistent web
service is simpler and a better fit here.

## 4. Security model — what's actually protecting people's accounts

- **Passwords:** hashed with bcrypt (via `passlib`), never stored or logged in plaintext.
- **Sessions:** JWT in an httpOnly, SameSite=Lax cookie (`Secure` too
  when `COOKIE_SECURE=true`). httpOnly means client-side JS/an XSS
  payload can't read the token; SameSite=Lax mitigates most CSRF for a
  same-origin app like this.
- **Cross-user isolation:** every query that touches user-specific data
  (settings, signals, backtest jobs) is scoped by the authenticated
  user's own id from the session — verified directly: one test account
  could not read another's settings, poll another's backtest job id, or
  confirm a trade using another user's signal id (all returned 404/409
  as expected).
- **Rate limiting:** signup/login are rate-limited per IP (`slowapi`) to
  slow down credential-stuffing/brute-force attempts — verified the
  limiter actually returns 429 past the threshold.
- **No real trading, ever.** There is no broker connection anywhere in
  this app. `/api/confirm` only acts on the signal currently cached for
  *that* user and always logs a paper trade — there is no code path,
  scheduled job, admin action, or otherwise, that sends a real order
  anywhere.
- **XSS defense-in-depth:** text sourced from outside your own server
  (news calendar titles) is HTML-escaped before being inserted into the
  page, in case an upstream feed is ever compromised or returns
  something unexpected.

**What this is *not*:** a full security audit, and not a substitute for
one before you handle real money at scale. If this grows to real
users, get an actual third-party security review and consider a
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
- **No real order execution.** By design — this is a signal/paper-trading
  dashboard, not a broker integration. If you want the app to place real
  orders, that means picking a broker with a real API, an adapter
  interface, and secure per-user credential storage — a meaningful
  addition, not a small one.
- **No admin tooling.** No way to view/disable users, see aggregate
  usage, etc., outside of querying the database directly.

## 6. Files

```
nas100_platform/
  app/
    main.py                 # FastAPI app, mounts routers + static files
    config.py                 # server secrets from env (JWT, DB URL, OANDA token)
    db.py                      # SQLAlchemy engine/session
    models.py                  # User, UserSettings, LastSignal, SignalLog, BacktestJob
    schemas.py                  # Pydantic request/response models
    auth.py                      # password hashing, JWT cookies, current_user dependency
    rate_limit.py                  # shared slowapi limiter
    data_source.py                  # OANDA / Yahoo / synthetic data source resolution + fallback chain
    routers/
      auth.py                        # signup, login, logout, change password
      settings.py                     # strategy + news settings CRUD
      trading.py                       # /api/signal, /api/candles, /api/quote, /api/confirm, /api/news
      backtest.py                       # background-job backtest start/poll
    smc_ict.py, backtest_engine.py, candle_utils.py, news.py,
    data_import.py                  # strategy engine, backtester, news, and CSV/Yahoo history import
  static/
    login.html, signup.html, settings.html, index.html, backtest.html
    style.css, js/api.js
  requirements.txt
  Dockerfile, docker-compose.yml, .dockerignore
  .env.example
```

## 7. Verification performed

Ran locally end-to-end before delivery: test account signing up,
updating settings, fetching signals and candles, confirming a paper
trade, and running a full backtest job through to completion — plus
explicit cross-user isolation checks (wrong user can't read another's
settings, poll their backtest job, or confirm using their signal id),
rate-limit threshold behavior, cookie flags, and confirming the data
source fallback chain (OANDA → Yahoo → synthetic simulator) correctly
degrades when upstream feeds are unreachable, verified against
sandbox-simulated outages. Docker build itself wasn't tested (no Docker
available in this environment) — the app was verified with the same
run command the Dockerfile uses, but do a test deploy before pointing
real users at it.
