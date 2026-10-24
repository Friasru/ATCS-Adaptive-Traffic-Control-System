from flask import current_app, request
from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.routing import Map, Rule

from app.api import api_bp


def api_adapter():
    """URL map without the static-file rule, which accepts GET on every path."""
    adapter = current_app.extensions.get("atcs_api_adapter")
    if adapter is None:
        rules = [Rule(r.rule, methods=r.methods, endpoint=r.endpoint)
                 for r in current_app.url_map.iter_rules() if r.endpoint != "static"]
        adapter = current_app.extensions["atcs_api_adapter"] = Map(rules).bind("")
    return adapter


@api_bp.app_errorhandler(404)
def api_method_not_allowed(error):
    """A GET to a POST-only /api route reaches the static-file route and ends as 404;
    report it as 405 like any other wrong method. Everything else is unchanged."""
    if not request.path.startswith("/api/"):
        return error
    try:
        api_adapter().match(request.path, method=request.method)
    except MethodNotAllowed as exc:
        allowed = ", ".join(sorted(exc.valid_methods or []))
        return {"error": "method not allowed"}, 405, {"Allow": allowed}
    except NotFound:
        pass
    return error
