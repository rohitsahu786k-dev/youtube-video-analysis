"""Accounts, signed sessions, CSRF (origin) protection, brute-force lockout and security headers."""
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from pyapp.config import settings
from pyapp.db import db, owner_var
from pyapp.vault import app_secret

COOKIE = "tvi_session"
PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = {"/login", "/api/health", "/api/auth/login", "/favicon.ico", "/robots.txt"}
LOCAL_USER = {"id": "local", "username": "local", "role": "admin", "owner": ""}


# ---- passwords -------------------------------------------------------------------------------------

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(candidate.hex(), digest)
    except Exception:
        return False


def check_password_strength(password: str):
    if len(password) < 10:
        raise ValueError("Use a password of at least 10 characters.")
    if password.lower() in {"password123", "1234567890", "qwertyuiop", "admin12345"}:
        raise ValueError("That password is too common.")


# ---- sessions --------------------------------------------------------------------------------------

def _serializer():
    return URLSafeTimedSerializer(app_secret(), salt="session-v1")


def make_token(user: dict) -> str:
    return _serializer().dumps({"u": user["id"], "v": user["passwordHash"][-12:]})


def read_token(token: str | None):
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=settings.SESSION_HOURS * 3600)
    except (BadSignature, SignatureExpired):
        return None


async def user_from_request(request: Request):
    payload = read_token(request.cookies.get(COOKIE))
    if not payload:
        return None
    user = await db.get("users", payload["u"])
    if not user or user.get("disabled") or user["passwordHash"][-12:] != payload["v"]:
        return None
    return user


def public_user(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


# ---- accounts --------------------------------------------------------------------------------------

async def bootstrap_admin():
    """Create/refresh the administrator from ADMIN_PASSWORD. The admin keeps the legacy (un-namespaced) workspace."""
    if settings.auth_required and not settings.ADMIN_PASSWORD and not await db.list("users"):
        raise RuntimeError("Authentication is required but no account exists. Set ADMIN_PASSWORD (and SECRET_KEY) in the environment.")
    if settings.APP_ENV.lower() == "production" and settings.auth_required and len(settings.SECRET_KEY or "") < 32:
        raise RuntimeError("Set SECRET_KEY to a random value of at least 32 characters in production.")
    if not settings.ADMIN_PASSWORD:
        return
    check_password_strength(settings.ADMIN_PASSWORD)
    current = await db.get("users", "admin")
    if current and verify_password(settings.ADMIN_PASSWORD, current["passwordHash"]) and current["username"] == settings.ADMIN_USERNAME:
        return
    await db.save("users", {"id": "admin", "username": settings.ADMIN_USERNAME, "passwordHash": hash_password(settings.ADMIN_PASSWORD), "role": "admin",
                            "owner": "", "createdAt": (current or {}).get("createdAt") or datetime.now(timezone.utc).isoformat()})


async def find_user(username: str):
    found = await db.list("users")
    return next((u for u in found if u["username"].lower() == username.strip().lower()), None)


async def create_user(username: str, password: str, role: str = "user"):
    username = username.strip()
    if not 3 <= len(username) <= 40 or not all(c.isalnum() or c in "._-" for c in username):
        raise ValueError("Usernames are 3-40 characters: letters, digits, dot, dash, underscore.")
    check_password_strength(password)
    if await find_user(username):
        raise ValueError("That username already exists.")
    identifier = "u" + secrets.token_hex(6)
    user = {"id": identifier, "username": username, "passwordHash": hash_password(password), "role": role, "owner": identifier, "createdAt": datetime.now(timezone.utc).isoformat()}
    await db.save("users", user)
    return user


# ---- brute-force protection ------------------------------------------------------------------------

class LoginLimiter:
    def __init__(self):
        self.failures: dict[str, list[float]] = {}

    def _recent(self, key):
        horizon = time.time() - settings.LOGIN_LOCK_MINUTES * 60
        self.failures[key] = [t for t in self.failures.get(key, []) if t > horizon]
        return self.failures[key]

    def locked(self, *keys) -> bool:
        return any(len(self._recent(k)) >= settings.LOGIN_MAX_FAILURES for k in keys)

    def fail(self, *keys):
        for k in keys:
            self._recent(k).append(time.time())

    def clear(self, *keys):
        for k in keys:
            self.failures.pop(k, None)


limiter = LoginLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---- middleware ------------------------------------------------------------------------------------

def security_headers(secure: bool) -> list[tuple[bytes, bytes]]:
    csp = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://s3.tradingview.com https://*.tradingview.com https://www.youtube.com https://s.ytimg.com; "
           "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; "
           "img-src 'self' data: https:; frame-src https://www.tradingview.com https://*.tradingview.com https://*.tradingview-widget.com https://www.youtube.com https://www.youtube-nocookie.com; "
           "connect-src 'self' https://*.tradingview.com; frame-ancestors 'self'; base-uri 'self'; form-action 'self'")
    headers = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"strict-origin-when-cross-origin"),
               (b"x-frame-options", b"SAMEORIGIN"), (b"content-security-policy", csp.encode()), (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
    if secure:
        headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
    return headers


class SecurityMiddleware:
    """Pure ASGI: authenticates, binds the user's workspace to the task context, blocks cross-site writes."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = scope["path"]
        headers = security_headers(settings.secure_cookies)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + headers
            await send(message)

        if scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return await JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)(scope, receive, send_with_headers)
        user = LOCAL_USER
        if settings.auth_required:
            public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
            user = await user_from_request(request)
            if not user and not public:
                if path.startswith("/api/"):
                    return await JSONResponse({"detail": "Authentication required."}, status_code=401)(scope, receive, send_with_headers)
                return await RedirectResponse("/login", status_code=303)(scope, receive, send_with_headers)
        scope.setdefault("state", {})["user"] = user
        token = owner_var.set((user or LOCAL_USER)["owner"])
        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            owner_var.reset(token)
