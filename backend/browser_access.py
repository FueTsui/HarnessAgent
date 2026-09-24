"""Browser request boundaries shared by cookie sessions ."""
from urllib.parse import urlsplit


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def cross_origin_cookie_write(request) -> bool:
    """Reject ambient browser writes, including login session fixation.

    Machine clients without browser origin headers retain their existing API contract.
    Explicit Bearer/API-key clients without a session cookie are not ambient authority.
    """
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if not request.url.path.startswith("/api/v1/"):
        return False
    from .config import settings
    explicit = request.headers.get("authorization", "").lower().startswith("bearer ") or request.headers.get("x-api-key")
    if explicit and not request.cookies.get(settings.AUTH_COOKIE_NAME):
        return False
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    target = _origin(str(request.url))
    origin = request.headers.get("origin")
    if origin is not None:
        return _origin(origin) is None or _origin(origin) != target
    referer = request.headers.get("referer")
    return bool(referer and _origin(referer) != target)

