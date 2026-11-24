import base64
import hmac
import os
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from . import config


def _origin(value, *, origin_header=False):
    """Compare origins, never URL prefixes or sibling subdomains."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        if origin_header and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError):
        return None


def _browser_write_allowed(request):
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return True
    # Basic credentials are attached automatically by browsers. CORS alone does
    # not stop cross-origin HTML forms or body-less POSTs from making changes.
    site = request.headers.get("sec-fetch-site", "").lower()
    if site not in {"", "same-origin", "none"}:
        return False
    # Cloud Run terminates TLS and forwards HTTP to the container. Its public
    # endpoint is HTTPS even when the ASGI server sees the internal scheme.
    target_url = request.url.replace(scheme="https") if os.environ.get("K_SERVICE") else request.url
    expected = _origin(str(target_url))
    origin = request.headers.get("origin")
    if origin is not None:
        return expected is not None and _origin(origin, origin_header=True) == expected
    referer = request.headers.get("referer")
    if referer is not None:
        return expected is not None and _origin(referer) == expected
    # Preserve authenticated non-browser clients that do not send browser
    # metadata; browser requests with opaque/null origins were rejected above.
    return True


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """모든 요청에 HTTP Basic Auth를 건다.

    브라우저가 자체 로그인 팝업을 띄우므로 앱 쪽에 로그인 화면이 필요 없다.
    APP_BASIC_PASS가 비어 있으면(로컬 개발 등) 인증을 건너뛴다.
    """

    async def dispatch(self, request, call_next):
        # Route matching uses the ASGI path. Host header text must never turn a
        # private route into one of the public authentication exemptions.
        if config.APP_BASIC_PASS and request.scope.get("path") not in {"/healthz", "/about", "/privacy"}:
            user, pw = "", ""
            header = request.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
                    user, _, pw = decoded.partition(":")
                except (ValueError, UnicodeError):
                    pass
            if not (hmac.compare_digest(user.encode("utf-8"), config.APP_BASIC_USER.encode("utf-8")) and hmac.compare_digest(
                pw.encode("utf-8"), config.APP_BASIC_PASS.encode("utf-8")
            )):
                return Response(
                    status_code=401,
                    content="Authentication required",
                    headers={"WWW-Authenticate": 'Basic realm="worksheet"'},
                )
        if not _browser_write_allowed(request):
            return JSONResponse({"detail": "서비스 화면에서 다시 시도해 주세요."}, status_code=403)
        return await call_next(request)
