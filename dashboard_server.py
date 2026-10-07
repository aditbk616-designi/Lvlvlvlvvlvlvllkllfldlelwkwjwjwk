# -*- coding: utf-8 -*-
"""FreeFire bot dashboard with owner authentication and local license management."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Dict, List, Any, Optional
from aiohttp import web

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
LICENSE_FILE = os.path.join(BASE_DIR, "licenses.json")
OWNER_FILE = os.path.join(BASE_DIR, "owner.json")
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")

# First-run credentials. Change these with environment variables before exposing the panel.
DEFAULT_OWNER_USER = os.getenv("DASHBOARD_OWNER_USER", "owner")
DEFAULT_OWNER_PASSWORD = os.getenv("DASHBOARD_OWNER_PASSWORD", "ChangeMe_84FF!")
DEMO_LICENSE = "84FF-DEMO-PERMANENT"
SESSION_TTL = 24 * 60 * 60

SESSIONS: Dict[str, Dict[str, Any]] = {}


def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210_000)
    return f"{salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210_000).hex()
        return hmac.compare_digest(actual, digest_hex)
    except Exception:
        return False


def _ensure_owner():
    if os.path.exists(OWNER_FILE):
        return
    data = {
        "username": DEFAULT_OWNER_USER,
        "password_hash": _hash_password(DEFAULT_OWNER_PASSWORD),
        "created_at": int(time.time()),
    }
    with open(OWNER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _ensure_licenses():
    licenses = _load_json(LICENSE_FILE, None)
    if licenses is None:
        licenses = [{
            "code": DEMO_LICENSE,
            "created_at": int(time.time()),
            "expires_at": None,
            "active": True,
            "uses": 0,
            "label": "Local demo permanent license",
        }]
        _save_json(LICENSE_FILE, licenses)


def _get_session(request: web.Request):
    sid = request.cookies.get("dashboard_session")
    if not sid:
        return None
    session = SESSIONS.get(sid)
    if not session:
        return None
    if session["expires_at"] < time.time():
        SESSIONS.pop(sid, None)
        return None
    return session


def _get_license(request: web.Request):
    code = request.cookies.get("dashboard_license")
    if not code:
        return None
    for item in _load_json(LICENSE_FILE, []):
        if item.get("code") == code and item.get("active", True):
            exp = item.get("expires_at")
            if exp is None or exp > time.time():
                return item
    return None


def _authorized(request: web.Request):
    session = _get_session(request)
    if session and session.get("role") == "owner":
        return {"role": "owner", "license": None}
    lic = _get_license(request)
    if lic:
        return {"role": "user", "license": lic}
    return {"role": "unlicensed", "license": None}


def _json_error(message, status=400):
    return web.json_response({"status": "error", "error": message}, status=status)


class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        self.logs.append({"time": time.strftime("%H:%M:%S"), "level": level, "message": message, "uid": uid})
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid, nickname, region, level, exp, likes=0):
        uid_str = str(uid)
        if uid_str not in self.accounts:
            self.accounts[uid_str] = {
                "uid": uid_str, "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD", "level": level or 1,
                "initial_exp": exp, "current_exp": exp, "gained_exp": 0,
                "likes": likes or 0, "status": "ONLINE", "matches_played": 0,
                "active_matches": 0, "last_match_time": None, "last_updated": time.strftime("%H:%M:%S")
            }
        else:
            acc = self.accounts[uid_str]
            if nickname: acc["nickname"] = nickname
            if region: acc["region"] = region
            if level: acc["level"] = level
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = time.strftime("%H:%M:%S")
        self.recalc_totals()

    def update_exp(self, uid, current_exp, level=None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0: acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            diff = current_exp - old_exp
            if diff > 0:
                self.log(f"Account {acc['nickname']} ({uid_str}) gained +{diff} EXP! Total: +{acc['gained_exp']}", "success", uid_str)
            self.recalc_totals()

    def update_status(self, uid, status, active_matches=None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None: self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] += 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            self.log(f"Account {self.accounts[uid_str]['nickname']} finished Match #{self.accounts[uid_str]['matches_played']}", "info", uid_str)

    def recalc_totals(self):
        self.total_gained_exp = sum(a.get("gained_exp", 0) for a in self.accounts.values())


bot_state = BotState()
_ensure_owner()
_ensure_licenses()


async def handle_index(request):
    if not os.path.exists(TEMPLATE_PATH):
        return web.Response(text="Dashboard template missing", status=500)
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html", charset="utf-8")


async def handle_auth_me(request):
    auth = _authorized(request)
    if not auth:
        return web.json_response({"authenticated": False})
    return web.json_response({
        "authenticated": True,
        "role": auth["role"],
        "licensed": auth["role"] in ("owner", "user"),
        "license": auth["license"],
    })


async def handle_login(request):
    try:
        data = await request.json()
        owner = _load_json(OWNER_FILE, {})
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if username != owner.get("username") or not _verify_password(password, owner.get("password_hash", "")):
            return _json_error("Invalid owner username or password", 401)
        sid = secrets.token_urlsafe(32)
        SESSIONS[sid] = {"role": "owner", "created_at": time.time(), "expires_at": time.time() + SESSION_TTL}
        response = web.json_response({"status": "ok", "role": "owner"})
        response.set_cookie("dashboard_session", sid, httponly=True, samesite="Lax", max_age=SESSION_TTL)
        response.del_cookie("dashboard_license")
        return response
    except Exception as e:
        return _json_error(str(e), 400)


async def handle_activate(request):
    try:
        data = await request.json()
        code = str(data.get("code", "")).strip().upper()
        if not code:
            return _json_error("License code is required")
        licenses = _load_json(LICENSE_FILE, [])
        for item in licenses:
            if item.get("code") == code and item.get("active", True):
                exp = item.get("expires_at")
                if exp is not None and exp <= time.time():
                    return _json_error("This license has expired", 410)
                item["uses"] = int(item.get("uses", 0)) + 1
                _save_json(LICENSE_FILE, licenses)
                response = web.json_response({"status": "ok", "license": item})
                response.set_cookie("dashboard_license", code, httponly=True, samesite="Lax", max_age=10 * 365 * 24 * 3600)
                return response
        return _json_error("Invalid license code", 404)
    except Exception as e:
        return _json_error(str(e), 400)


def _index_response():
    if not os.path.exists(TEMPLATE_PATH):
        return web.Response(text="Dashboard template missing", status=500)
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html", charset="utf-8")


async def handle_owner_login_form(request):
    """Single-site HTML login: return the page directly instead of redirecting.
    This avoids 404s on hosts/proxies that do not preserve POST redirects.
    """
    response = _index_response()
    try:
        data = await request.post()
        owner = _load_json(OWNER_FILE, {})
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if username != owner.get("username") or not _verify_password(password, owner.get("password_hash", "")):
            response.headers["X-Login-Error"] = "1"
            return response
        sid = secrets.token_urlsafe(32)
        SESSIONS[sid] = {"role": "owner", "created_at": time.time(), "expires_at": time.time() + SESSION_TTL}
        response.set_cookie("dashboard_session", sid, httponly=True, samesite="Lax", max_age=SESSION_TTL, path="/")
        response.del_cookie("dashboard_license", path="/")
        return response
    except Exception:
        response.headers["X-Login-Error"] = "1"
        return response


async def handle_activate_form(request):
    """Single-site HTML license activation: return the same page directly.
    No redirect is used, so the hosting provider only needs one public route.
    """
    response = _index_response()
    try:
        data = await request.post()
        code = str(data.get("code", "")).strip().upper()
        if not code:
            response.headers["X-Code-Error"] = "missing"
            return response
        licenses = _load_json(LICENSE_FILE, [])
        for item in licenses:
            if item.get("code") == code and item.get("active", True):
                exp = item.get("expires_at")
                if exp is not None and exp <= time.time():
                    response.headers["X-Code-Error"] = "expired"
                    return response
                item["uses"] = int(item.get("uses", 0)) + 1
                _save_json(LICENSE_FILE, licenses)
                response.set_cookie("dashboard_license", code, httponly=True, samesite="Lax", max_age=10 * 365 * 24 * 3600, path="/")
                response.del_cookie("dashboard_session", path="/")
                return response
        response.headers["X-Code-Error"] = "invalid"
        return response
    except Exception:
        response.headers["X-Code-Error"] = "server"
        return response


async def handle_logout(request):
    sid = request.cookies.get("dashboard_session")
    if sid: SESSIONS.pop(sid, None)
    response = web.json_response({"status": "ok"})
    response.del_cookie("dashboard_session")
    response.del_cookie("dashboard_license")
    return response


async def owner_required(request):
    auth = _authorized(request)
    if not auth or auth["role"] != "owner":
        return None
    return auth


async def access_required(request):
    auth = _authorized(request)
    if not auth or auth["role"] == "unlicensed":
        return None
    return auth


async def handle_create_license(request):
    if not await owner_required(request):
        return _json_error("Owner access required", 403)
    try:
        data = await request.json()
        duration = str(data.get("duration", "days")).lower()
        amount = int(data.get("amount", 1))
        label = str(data.get("label", "")).strip()[:80]
        if duration not in {"minutes", "hours", "days", "months", "permanent"}:
            return _json_error("Invalid duration")
        if duration != "permanent" and amount < 1:
            return _json_error("Duration must be at least 1")
        code = "84FF-" + secrets.token_hex(5).upper()
        seconds = None
        if duration == "minutes": seconds = amount * 60
        elif duration == "hours": seconds = amount * 3600
        elif duration == "days": seconds = amount * 86400
        elif duration == "months": seconds = amount * 30 * 86400
        item = {
            "code": code, "created_at": int(time.time()),
            "expires_at": None if seconds is None else int(time.time() + seconds),
            "active": True, "uses": 0,
            "label": label or ("Permanent" if duration == "permanent" else f"{amount} {duration}"),
        }
        licenses = _load_json(LICENSE_FILE, [])
        licenses.append(item)
        _save_json(LICENSE_FILE, licenses)
        bot_state.log(f"Owner created license {code} ({item['label']})", "success")
        return web.json_response({"status": "ok", "license": item})
    except Exception as e:
        return _json_error(str(e), 400)


async def handle_list_licenses(request):
    if not await owner_required(request):
        return _json_error("Owner access required", 403)
    licenses = _load_json(LICENSE_FILE, [])
    return web.json_response({"status": "ok", "licenses": licenses})


async def handle_revoke_license(request):
    if not await owner_required(request):
        return _json_error("Owner access required", 403)
    data = await request.json()
    code = str(data.get("code", "")).strip().upper()
    licenses = _load_json(LICENSE_FILE, [])
    changed = False
    for item in licenses:
        if item.get("code") == code:
            item["active"] = False
            changed = True
    if not changed:
        return _json_error("License not found", 404)
    _save_json(LICENSE_FILE, licenses)
    return web.json_response({"status": "ok"})


async def handle_get_stats(request):
    if not await access_required(request):
        return _json_error("Valid license required", 403)
    accounts_data = sorted(bot_state.accounts.values(), key=lambda x: x.get("gained_exp", 0), reverse=True)
    return web.json_response({
        "total_accounts": len(bot_state.accounts), "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp, "accounts": accounts_data,
        "logs": bot_state.logs[-60:], "uptime": int(time.time() - bot_state.start_time)
    })


async def handle_add_account(request):
    if not await access_required(request): return _json_error("Valid license required", 403)
    try:
        data = await request.json()
        existing = _load_json(ACCOUNTS_FILE, [])
        if "uid" in data and "password" in data:
            uid, pwd = str(data["uid"]).strip(), str(data["password"]).strip()
            if not uid or not pwd: return _json_error("UID and Password are required")
            existing = [a for a in existing if str(a.get("uid")) != uid]
            existing.append({"uid": uid, "password": pwd})
            log_name = uid
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token: return _json_error("Token is required")
            existing = [a for a in existing if a.get("token") != token]
            existing.append({"token": token})
            log_name = "Token"
        else:
            return _json_error("Invalid payload")
        _save_json(ACCOUNTS_FILE, existing)
        bot_state.log(f"New account added: {log_name}", "success")
        cb = bot_state.refresh_callbacks.get("on_account_added")
        if cb: asyncio.create_task(cb(data))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return _json_error(str(e))


async def handle_delete_account(request):
    if not await access_required(request): return _json_error("Valid license required", 403)
    try:
        data = await request.json(); uid = str(data.get("uid") or "").strip()
        if not uid: return _json_error("UID is required")
        existing = _load_json(ACCOUNTS_FILE, [])
        cred = bot_state.account_credentials.get(uid)
        auth_token = str(cred.get("auth_token")) if cred and cred.get("auth_token") else ""
        kept = []
        for acc in existing:
            if str(acc.get("uid") or "").strip() == uid: continue
            tok = str(acc.get("token") or "")
            if auth_token and tok and (tok == auth_token or tok[:20] == auth_token[:20]): continue
            kept.append(acc)
        _save_json(ACCOUNTS_FILE, kept)
        bot_state.accounts.pop(uid, None)
        keys = {uid}
        if cred:
            keys.update(filter(None, [str(cred.get("auth_uid") or ""), str(cred.get("auth_token") or "")[:10]]))
        for key in keys:
            task = bot_state.account_workers.pop(key, None)
            if task and not task.done(): task.cancel()
        for key, value in list(bot_state.account_credentials.items()):
            if value is cred or str(value.get("account_id", "")) == uid:
                bot_state.account_credentials.pop(key, None)
        bot_state.log(f"Account {uid} removed from rotation.", "warning", uid)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return _json_error(str(e))


async def handle_refresh_account(request):
    if not await access_required(request): return _json_error("Valid license required", 403)
    try:
        data = await request.json(); uid = str(data.get("uid") or "").strip()
        cb = bot_state.refresh_callbacks.get("on_refresh_account")
        if cb: asyncio.create_task(cb(uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return _json_error(str(e))


async def start_web_dashboard(host="0.0.0.0", port=5000):
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/auth/me", handle_auth_me)
    app.router.add_get("/health", lambda request: web.json_response({"status": "ok"}))
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_post("/owner/login", handle_owner_login_form)
    app.router.add_post("/api/auth/logout", handle_logout)
    app.router.add_post("/api/license/activate", handle_activate)
    app.router.add_post("/license/activate", handle_activate_form)
    app.router.add_post("/api/license/create", handle_create_license)
    app.router.add_get("/api/license/list", handle_list_licenses)
    app.router.add_post("/api/license/revoke", handle_revoke_license)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\033[92m[+] Web Dashboard listening on 0.0.0.0:{port}\033[0m")
