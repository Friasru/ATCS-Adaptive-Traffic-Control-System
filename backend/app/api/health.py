from app.api import api_bp


@api_bp.route("/health")
def health():
    return {"status": "ok"}
