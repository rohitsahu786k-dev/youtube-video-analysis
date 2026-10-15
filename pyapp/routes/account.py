"""Authentication, user administration, Google connection and operational endpoints (health, backup)."""
import asyncio
import io
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from pyapp import auth, sheets
from pyapp.config import settings
from pyapp.db import db

router = APIRouter(prefix="/api", tags=["Account"])


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class PasswordChange(BaseModel):
    current: str
    new: str


class NewUser(BaseModel):
    username: str
    password: str
    role: str = Field(default="user", pattern="^(user|admin)$")


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Authentication required.")
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "Administrator access required.")
    return user


@router.post("/auth/login")
async def login(req: Login, request: Request, response: Response):
    if not settings.auth_required:
        return {"authRequired": False, "user": auth.LOCAL_USER}
    ip = auth.client_ip(request)
    keys = (f"ip:{ip}", f"user:{req.username.lower()}")
    if auth.limiter.locked(*keys):
        raise HTTPException(429, f"Too many failed attempts. Try again in {settings.LOGIN_LOCK_MINUTES} minutes.")
    user = await auth.find_user(req.username)
    valid = bool(user) and not user.get("disabled") and auth.verify_password(req.password, user["passwordHash"])
    if not valid:
        if not user:
            auth.hash_password(req.password)             # keep timing similar for unknown users
        auth.limiter.fail(*keys)
        await db.save("auth_events", {"id": f"{datetime.now(timezone.utc).timestamp()}-{ip}", "at": datetime.now(timezone.utc).isoformat(), "ip": ip, "username": req.username[:80], "ok": False})
        raise HTTPException(401, "Invalid username or password.")
    auth.limiter.clear(*keys)
    response.set_cookie(auth.COOKIE, auth.make_token(user), max_age=settings.SESSION_HOURS * 3600, httponly=True, samesite="lax", secure=settings.secure_cookies, path="/")
    return {"authRequired": True, "user": auth.public_user(user)}


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(request: Request):
    user = getattr(request.state, "user", None)
    return {"authRequired": settings.auth_required, "user": auth.public_user(user) if user and user.get("passwordHash") else user}


@router.post("/auth/password")
async def change_password(req: PasswordChange, request: Request, response: Response):
    user = current_user(request)
    stored = await db.get("users", user["id"])
    if not stored or not auth.verify_password(req.current, stored["passwordHash"]):
        raise HTTPException(403, "Current password is incorrect.")
    try:
        auth.check_password_strength(req.new)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    stored["passwordHash"] = auth.hash_password(req.new)
    await db.save("users", stored)
    response.set_cookie(auth.COOKIE, auth.make_token(stored), max_age=settings.SESSION_HOURS * 3600, httponly=True, samesite="lax", secure=settings.secure_cookies, path="/")
    return {"ok": True}


@router.get("/users")
async def list_users(request: Request):
    require_admin(request)
    return [auth.public_user(u) | {"disabled": bool(u.get("disabled")), "createdAt": u.get("createdAt")} for u in await db.list("users")]


@router.post("/users", status_code=201)
async def add_user(req: NewUser, request: Request):
    require_admin(request)
    try:
        user = await auth.create_user(req.username, req.password, req.role)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return auth.public_user(user)


@router.post("/users/{user_id}/disable")
async def disable_user(user_id: str, request: Request):
    me_ = require_admin(request)
    if user_id == me_["id"]:
        raise HTTPException(409, "You cannot disable your own account.")
    user = await db.get("users", user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user["disabled"] = not user.get("disabled")
    await db.save("users", user)
    return {"id": user_id, "disabled": user["disabled"]}


# ---- Google ------------------------------------------------------------------------------------------

@router.get("/google/status")
async def google_status():
    record = await db.get("integrations", "google")
    return {"configured": sheets.configured(), "connected": bool(record and record.get("connected")), "redirectUri": sheets.redirect_uri(), "scope": sheets.SCOPE}


@router.get("/google/connect")
async def google_connect(request: Request):
    user = current_user(request)
    try:
        return {"url": sheets.authorization_url(user["id"])}
    except sheets.SheetsError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    user = current_user(request)
    if error:
        return RedirectResponse("/integrations?google=denied", status_code=303)
    try:
        if sheets.verify_state(state) != user["id"]:
            raise sheets.SheetsError("Authorization was started by a different account.")
        await sheets.exchange_code(code)
    except sheets.SheetsError as exc:
        return RedirectResponse("/integrations?google=error&reason=" + str(exc)[:120].replace(" ", "+"), status_code=303)
    return RedirectResponse("/integrations?google=connected", status_code=303)


@router.post("/google/disconnect")
async def google_disconnect():
    await db.delete("integrations", "google")
    return {"connected": False}


# ---- operations --------------------------------------------------------------------------------------

def snapshot_database(target: Path):
    source = sqlite3.connect(Path(settings.STORAGE_PATH) / "intelligence.sqlite3")
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


@router.get("/admin/backup")
async def backup(request: Request):
    """Consistent SQLite snapshot plus market data sets as one ZIP (evidence frames are listed, not bundled)."""
    require_admin(request)
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "intelligence.sqlite3"
        await asyncio.to_thread(snapshot_database, snapshot)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "intelligence.sqlite3")
            datasets = Path(settings.STORAGE_PATH) / "datasets"
            if datasets.is_dir():
                for path in datasets.glob("*.json.gz"):
                    archive.write(path, f"datasets/{path.name}")
            archive.writestr("README.txt", "Restore: stop the app, replace storage/intelligence.sqlite3 and storage/datasets, restart. Back up storage/evidence separately.")
    return Response(buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="backup-{datetime.now(timezone.utc):%Y%m%d-%H%M}.zip"'})


@router.get("/admin/system")
async def system_status(request: Request):
    require_admin(request)
    storage = Path(settings.STORAGE_PATH)
    usage = shutil.disk_usage(storage)
    return {"diskFreeGb": round(usage.free / 1e9, 1), "diskTotalGb": round(usage.total / 1e9, 1), "owners": await db.owners(), "authRequired": settings.auth_required,
            "workerEnabled": settings.ENABLE_WORKER, "env": settings.APP_ENV}
