
import os

from authlib.integrations.base_client.errors import OAuthError
from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, request, session, url_for
from flask_cors import CORS

from utils.data_processor import build_economic_dataset, summarize_dataset
from utils.database import (
    DatabaseServiceError,
    database_configured,
    database_status,
    fetch_audit_events,
    fetch_dataset,
    fetch_research_rows,
    write_audit_event,
)
from utils.economic_models import analyze_series, detect_anomalies, forecast_series
from utils.identity import (
    ENDPOINT_PERMISSIONS,
    PUBLIC_AUTH_ENDPOINTS,
    authentication_required,
    google_auth_configured,
    has_permission,
    register_google_oauth,
    user_from_google_claims,
)
from utils.ai_providers import (
    AIProviderError,
    explain_research_result,
    provider_status,
)
from utils.research_assistant import (
    ResearchProviderError,
    ResearchQueryError,
    get_research_intent,
    parse_local_intent,
    summarize_observations,
)
from utils.sap_btp import SAPIntegrationError, check_sap_connection, sap_status

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET") or (
    "artha-local-development-session-key"
    if not authentication_required()
    else None
)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
)
frontend_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:5174,http://localhost:5174",
    ).split(",")
    if origin.strip()
]
CORS(app, origins=frontend_origins, supports_credentials=True)
google_oauth = register_google_oauth(app)
economic_dataset = build_economic_dataset()


def current_user() -> dict[str, str] | None:
    if app.secret_key is None:
        return None
    user = session.get("user")
    return user if isinstance(user, dict) else None


def audit_actor(user: dict[str, str]) -> dict[str, str]:
    return {
        "sub": user["sub"],
        "email": user["email"],
        "role": user["role"],
    }


@app.before_request
def enforce_identity_and_audit():
    g.audit_user = None
    endpoint = request.endpoint
    if request.method == "OPTIONS" or endpoint in PUBLIC_AUTH_ENDPOINTS:
        return None

    if authentication_required():
        if not google_auth_configured() or not os.getenv("SESSION_SECRET"):
            return jsonify({"error": "Enterprise authentication is not fully configured."}), 503

        origin = request.headers.get("Origin")
        if request.method not in {"GET", "HEAD"} and origin and origin.rstrip("/") not in frontend_origins:
            return jsonify({"error": "Request origin is not permitted."}), 403

        user = current_user()
        if user is None:
            return jsonify({"error": "Google sign-in is required.", "authenticated": False}), 401

        permission = ENDPOINT_PERMISSIONS.get(endpoint or "")
        if permission and not has_permission(user, permission):
            try:
                write_audit_event(
                    audit_actor(user),
                    request.method,
                    request.path,
                    "denied",
                    {"reason": "permission_denied"},
                )
            except DatabaseServiceError:
                app.logger.exception("Denied request could not be recorded in the audit log.")
                return jsonify({"error": "The required audit service is unavailable."}), 503
            return jsonify({"error": "Your assigned role does not permit this action."}), 403

        try:
            write_audit_event(
                audit_actor(user),
                request.method,
                request.path,
                "started",
            )
        except DatabaseServiceError:
            app.logger.exception("Protected request could not be recorded in the audit log.")
            return jsonify({"error": "The required audit service is unavailable."}), 503
        g.audit_user = user
    elif endpoint in {"sap_connection_check", "enterprise_audit"}:
        return jsonify({"error": "Enable Google authentication before using this control-plane action."}), 503

    return None


@app.after_request
def finalize_audit(response):
    user = getattr(g, "audit_user", None)
    if user:
        try:
            write_audit_event(
                audit_actor(user),
                request.method,
                request.path,
                "succeeded" if response.status_code < 400 else "failed",
                {"status_code": response.status_code},
            )
        except DatabaseServiceError:
            app.logger.exception("Protected request outcome could not be recorded in the audit log.")
            failed_response = jsonify({"error": "The required audit service is unavailable."})
            failed_response.status_code = 503
            return failed_response
    return response

def current_dataset() -> tuple[list[dict[str, object]], str]:
    stored = fetch_dataset()
    if stored is None:
        return economic_dataset, "synthetic"
    return stored, "neon_postgres"


@app.route("/")
def home():
    return jsonify({
        "project": "ARTHA AI",
        "status": "ONLINE",
        "message": "Economic Intelligence Backend Running"
    })

@app.route("/api/health")
def health():
    return jsonify({
        "status": "healthy"
    })


@app.route("/api/auth/login")
def auth_login():
    if google_oauth is None or not os.getenv("SESSION_SECRET") and authentication_required():
        return jsonify({"error": "Google sign-in is not fully configured."}), 503
    callback_url = os.getenv("GOOGLE_REDIRECT_URI") or url_for(
        "auth_callback",
        _external=True,
    )
    return google_oauth.authorize_redirect(callback_url)


@app.route("/api/auth/callback")
def auth_callback():
    if google_oauth is None or not os.getenv("SESSION_SECRET") and authentication_required():
        return jsonify({"error": "Google sign-in is not fully configured."}), 503
    try:
        token = google_oauth.authorize_access_token()
    except OAuthError:
        app.logger.warning("Google sign-in callback was rejected.")
        return jsonify({"error": "Google sign-in could not be completed."}), 401

    claims = token.get("userinfo")
    if not isinstance(claims, dict):
        return jsonify({"error": "Google did not return a verified user profile."}), 401
    try:
        user = user_from_google_claims(claims)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403

    session.clear()
    session["user"] = user
    session.permanent = True
    return redirect(frontend_origins[0] + "/#research")


@app.route("/api/auth/me")
def auth_me():
    user = current_user()
    return jsonify({
        "authenticated": user is not None,
        "auth_required": authentication_required(),
        "google_configured": google_auth_configured(),
        "user": user,
    }), 200 if user or not authentication_required() else 401


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"status": "signed_out"})


@app.route("/api/enterprise/status")
def enterprise_status():
    if database_configured():
        try:
            database = database_status()
        except DatabaseServiceError:
            app.logger.exception("Enterprise database status check failed.")
            return jsonify({"error": "The configured database could not be checked."}), 503
    else:
        database = {"status": "not_configured", "schema_version": None}

    return jsonify({
        "auth_required": authentication_required(),
        "google": {
            "configured": google_auth_configured(),
            "status": "configured" if google_auth_configured() else "not_configured",
            "session_secret_configured": bool(os.getenv("SESSION_SECRET")),
        },
        "database": database,
        "audit": {
            "status": "ready" if database["status"] == "connected" else "not_ready",
            "migration": "002_enterprise_control_plane",
        },
        "providers": provider_status(),
        "sap": sap_status(),
    })


@app.route("/api/enterprise/sap/check", methods=["POST"])
def sap_connection_check():
    if not authentication_required():
        return jsonify({"error": "Enable Google authentication before testing SAP connectivity."}), 503
    try:
        result = check_sap_connection()
    except SAPIntegrationError as exc:
        app.logger.warning("Read-only SAP connection test failed: %s", exc)
        return jsonify({"error": str(exc)}), 502
    return jsonify(result)


@app.route("/api/enterprise/audit")
def enterprise_audit():
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        return jsonify({"error": "Audit limit must be an integer from 1 to 200."}), 400
    if not 1 <= limit <= 200:
        return jsonify({"error": "Audit limit must be an integer from 1 to 200."}), 400
    try:
        events = fetch_audit_events(limit)
    except DatabaseServiceError:
        app.logger.exception("Enterprise audit events could not be retrieved.")
        return jsonify({"error": "The configured audit log could not be retrieved."}), 503
    return jsonify({"events": events})


@app.route("/api/ready")
def readiness():
    if authentication_required() and (
        not google_auth_configured()
        or not os.getenv("SESSION_SECRET")
        or not database_configured()
    ):
        return jsonify({
            "status": "not_ready",
            "mode": "enterprise",
            "reason": "Configure Google OAuth, SESSION_SECRET, and the audited database.",
        }), 503

    if not database_configured():
        return jsonify({
            "status": "ready",
            "mode": "synthetic",
            "database": "not_configured",
            "schema_version": None,
        })

    try:
        status = database_status()
    except DatabaseServiceError:
        app.logger.exception("Database readiness check failed.")
        return jsonify({
            "status": "not_ready",
            "mode": "database",
            "database": "unavailable",
        }), 503

    ready = status["status"] == "connected"
    return jsonify({
        "status": "ready" if ready else "not_ready",
        "mode": "database",
        "database": status["status"],
        "schema_version": status["schema_version"],
    }), 200 if ready else 503


@app.route("/api/economic/dataset")
def economic_data():
    try:
        rows, source = current_dataset()
    except DatabaseServiceError:
        app.logger.exception("Economic dataset retrieval failed.")
        return jsonify({"error": "The configured database could not provide economic data."}), 503
    return jsonify({
        "data": rows,
        "source": source,
        "updated_at": rows[-1]["Date"] if rows else None,
    })


@app.route("/api/economic/summary")
def economic_summary():
    try:
        rows, source = current_dataset()
    except DatabaseServiceError:
        app.logger.exception("Economic summary retrieval failed.")
        return jsonify({"error": "The configured database could not provide economic data."}), 503
    if not rows:
        return jsonify({"error": "No economic observations are available."}), 404
    return jsonify({**summarize_dataset(rows), "source": source})


@app.route("/api/economic/analysis")
def economic_analysis():
    metric = request.args.get("metric", "GDP_Growth")
    try:
        rows, source = current_dataset()
        analysis = analyze_series(rows, metric)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except DatabaseServiceError:
        app.logger.exception("Economic trend analysis retrieval failed.")
        return jsonify({"error": "The configured database could not provide economic data."}), 503
    analysis["source"] = source
    return jsonify(analysis)


@app.route("/api/economic/anomalies")
def economic_anomalies():
    try:
        rows, source = current_dataset()
        result = detect_anomalies(rows)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except DatabaseServiceError:
        app.logger.exception("Economic anomaly screening retrieval failed.")
        return jsonify({"error": "The configured database could not provide economic data."}), 503
    result["source"] = source
    return jsonify(result)


@app.route("/api/economic/forecast")
def economic_forecast():
    metric = request.args.get("metric", "GDP_Growth")
    try:
        months = int(request.args.get("months", "6"))
        rows, source = current_dataset()
        result = forecast_series(rows, metric, months)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except DatabaseServiceError:
        app.logger.exception("Economic forecast retrieval failed.")
        return jsonify({"error": "The configured database could not provide economic data."}), 503
    result["source"] = source
    return jsonify(result)


@app.route("/api/research/query", methods=["POST"])
def research_query():
    payload = request.get_json(silent=True)
    question = payload.get("question") if isinstance(payload, dict) else None
    provider = payload.get("provider", "local") if isinstance(payload, dict) else "local"
    if not isinstance(question, str) or not question.strip() or len(question) > 300:
        return jsonify({"error": "Provide a research question of 1–300 characters."}), 400
    if not isinstance(provider, str):
        return jsonify({"error": "Select a supported research provider."}), 400
    if provider != "local" and not authentication_required():
        return jsonify({"error": "Enable Google authentication before using external AI providers."}), 503

    try:
        if authentication_required():
            intent, assistant = get_research_intent(question.strip())
        else:
            intent, assistant = parse_local_intent(question.strip()), "local"
        if database_configured():
            rows = fetch_research_rows(intent)
            source = "neon_postgres"
        else:
            rows, source = current_dataset()
            rows = [
                (row["Date"], float(row[intent["metric"]]))
                for row in rows
                if (intent["start_date"] is None or row["Date"] >= intent["start_date"])
                and (intent["end_date"] is None or row["Date"] <= intent["end_date"])
            ]
        result = summarize_observations(rows, intent)
        explanation = explain_research_result(provider, question.strip(), result)
    except ResearchQueryError as exc:
        return jsonify({"error": str(exc)}), 400
    except ResearchProviderError as exc:
        app.logger.warning("Research provider request failed: %s", exc)
        return jsonify({"error": str(exc)}), 502
    except AIProviderError as exc:
        app.logger.warning("Selected research provider request failed: %s", exc)
        return jsonify({"error": str(exc)}), 502
    except DatabaseServiceError:
        app.logger.exception("Validated research query failed.")
        return jsonify({"error": "The configured database could not complete the validated analysis."}), 503

    return jsonify({
        **result,
        "answer": explanation or result["answer"],
        "explanation": explanation,
        "provider": provider,
        "assistant": assistant,
        "source": source,
        "synthetic_data": source == "synthetic",
    })


if __name__ == "__main__":
    print("Starting ARTHA AI Backend...")
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True)
