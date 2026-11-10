# ARTHA backend

The Flask API runs in local synthetic-data mode by default:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Copy `.env.example` to `.env` to configure optional integrations. Set `DATABASE_URL` to a Neon/PostgreSQL connection string to enable database-backed reads. Database reads are now read-only and never create or seed tables. After reviewing your database schema, explicitly apply compatible migrations from the backend directory:

```powershell
python migrate.py
```

The initial migration creates an `economic_indicators` dimension and a normalized `economic_observations` fact table. `(observation_date, metric_key)` is the composite primary key, `metric_key` is a foreign key, and a `(metric_key, observation_date)` index supports indicator/date-range analysis. Existing tables with incompatible shapes cause the migration to stop before applying changes; review and migrate legacy schemas manually rather than replacing them. Synthetic observations are not inserted into Neon automatically.

`GET /api/health` is a liveness check. `GET /api/ready` reports whether local synthetic mode is available or the configured database has an applied schema migration.

## Enterprise control plane

The Research Studio includes a Secure AI Control Plane for Google identity, model routing, SAP BTP, database readiness, and audit status. Integration status means **configured**, not live-verified; real provider credentials and an SAP tenant are required to test the external connections.

### Google identity and access policy

Configure `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_ALLOWED_DOMAINS` (comma-separated), `GOOGLE_REDIRECT_URI`, and a strong `SESSION_SECRET` in the backend environment. Register the exact redirect URI with the Google OAuth web client. For local Vite, `FRONTEND_ORIGINS` should include the UI origin (for example `http://localhost:5173`); use the actual deployed HTTPS origin in production.

`GOOGLE_ROLE_MAP` is a JSON object mapping account email addresses to `viewer`, `analyst`, `approver`, `auditor`, or `admin`, for example:

```json
{"researcher@example.com":"analyst","audit@example.com":"auditor"}
```

Accounts not explicitly mapped receive the least-privilege `viewer` role. Set `AUTH_REQUIRED=true` to require Google sign-in. In this mode, the application requires Google OAuth, a session secret, a configured database, and migration `002_enterprise_control_plane`; protected requests fail closed if audit logging is unavailable. Use `SESSION_COOKIE_SECURE=true` behind HTTPS in production. Keep local synthetic mode (`AUTH_REQUIRED=false`) for development; external model calls and SAP connectivity checks are disabled until authentication is enabled.

### AI providers

Provider credentials are read by Flask only; never put them in the frontend. The supported explanation providers are `gemini`, `openai`, `anthropic`, and `azure_openai`, configured with the matching keys/endpoints in `.env.example`. `GEMINI_API_KEY` can also enable constrained research-intent parsing when authentication is enabled. The model receives the user's question and a server-computed result for explanation; generated SQL is never executed. In local unauthenticated mode, research uses the local parser and local explanation only. Provider status indicates that settings exist, not that a live provider request succeeded.

### SAP BTP

Set the `SAP_BTP_*` values and `SAP_ODATA_READ_PATH` for the tenant's destination service. Use a destination that issues a bearer token. The control-plane test performs a read-only GET and clamps `$top` to one record; it reports field names and sample count but never returns sample record values. Only an authenticated analyst, approver, or admin can run the test.

### API and audit endpoints

- `GET /api/auth/me`, `GET /api/auth/login`, and `POST /api/auth/logout` manage Google sign-in.
- `GET /api/enterprise/status` reports configured integration and migration states.
- `POST /api/enterprise/sap/check` runs the read-only SAP test.
- `GET /api/enterprise/audit?limit=50` returns recent audit events to auditors and admins (limit 1–200).
- Protected API requests record start and outcome events; access denials are also audited.

Run database migrations only after reviewing the target schema. `002_enterprise_control_plane` adds the indexed audit log. The app does not apply migrations automatically and never seeds synthetic rows into Neon.

Set `GEMINI_API_KEY` to enable Gemini intent extraction when authentication is enabled. Gemini is asked for a constrained metric, operation, and optional date range—not SQL. The backend validates that intent against allowlists, then builds a parameterized query against the fixed schema. Without a Gemini key, a local parser supports common questions about GDP growth, CPI/WPI inflation, repo rates, and forex reserves.

Research endpoints:

- `POST /api/research/query` with `{"question":"What was average GDP growth in 2024?"}`
- `GET /api/economic/analysis?metric=GDP_Growth`
- `GET /api/economic/anomalies`
- `GET /api/economic/forecast?metric=GDP_Growth&months=6`

Trend, anomaly, and forecast outputs are statistical analysis, not verified releases or financial advice. The default dataset is synthetic. Run the API tests with `python -m unittest discover -s tests`.
