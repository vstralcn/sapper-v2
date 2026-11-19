"""SQLite-backed administrator authentication for the Web console (stdlib only)."""
from __future__ import annotations

import contextlib
import getpass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time

SESSION_SECONDS = 12 * 60 * 60
LOGIN_WINDOW = 60
LOGIN_LIMIT = 10
# OWASP's scrypt N=2^15, r=8, p=3 option uses about 32 MiB per derivation.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 32768, 8, 3
_HASH_SLOTS = threading.BoundedSemaphore(2)
_DUMMY_HASH = "scrypt$32768$8$3$" + "00" * 16 + "$" + "00" * 64


class AuthError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def username_text(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,64}", value.strip()):
        raise AuthError("用户名需为 1–64 个英文字母、数字或 _.@+- 字符")
    return value.strip()


def password_bytes(value, *, new=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        raise AuthError("密码需为 6–1024 个字符" if new else "用户名或密码错误", 400 if new else 401)
    if new and (len(value) < 6 or not value.strip()):
        raise AuthError("密码需为 6–1024 个字符，可使用长口令")
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise AuthError("密码包含无效字符") from exc


@contextlib.contextmanager
def hash_slot():
    if not _HASH_SLOTS.acquire(blocking=False):
        raise AuthError("登录请求繁忙，请稍后重试", 429)
    try:
        yield
    finally:
        _HASH_SLOTS.release()


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password_bytes(password, new=True), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def password_matches(password, encoded):
    raw = password_bytes(password)
    method, n, r, p, salt, expected = encoded.split("$")
    if (method, int(n), int(r), int(p)) != ("scrypt", SCRYPT_N, SCRYPT_R, SCRYPT_P):
        raise AuthError("密码哈希格式不支持，请通过本机命令重置密码", 503)
    actual = hashlib.scrypt(raw, salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                            maxmem=64 * 1024 * 1024)
    return hmac.compare_digest(actual.hex(), expected)


def session_digest(sid):
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


class AuthStore:
    """One administrator; sessions and rate limits survive Web process restarts."""

    def __init__(self, state: Path):
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state, 0o700)
        self.path = state / "auth.sqlite3"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise AuthError("认证数据库版本高于当前程序支持的版本", 503)
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'admin' CHECK (role = 'admin'),
                    credential_version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    credential_version INTEGER NOT NULL,
                    csrf TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    scope TEXT NOT NULL,
                    attempted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_attempts_scope ON login_attempts(scope, attempted_at);
                PRAGMA user_version = 1;
                COMMIT;
            """)

    @contextlib.contextmanager
    def connection(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        except sqlite3.Error as exc:
            raise AuthError("认证数据库不可用，请检查状态目录中的 auth.sqlite3", 503) from exc
        finally:
            if db is not None:
                db.close()

    def configured(self):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def initialize(self, username, password):
        username = username_text(username)
        with hash_slot():
            encoded = password_hash(password)
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users").fetchone():
                raise AuthError("管理员已初始化，请使用 password-reset 重置密码", 409)
            db.execute("INSERT INTO users (id, username, password_hash, created_at, updated_at) VALUES (1, ?, ?, ?, ?)",
                       (username, encoded, now, now))

    def reset_password(self, username, password):
        username = username_text(username)
        with hash_slot():
            encoded = password_hash(password)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE users SET password_hash=?, credential_version=credential_version+1, updated_at=? WHERE username=?",
                                 (encoded, time.time(), username))
            if not changed.rowcount:
                raise AuthError("管理员账号不存在，请核对用户名或先执行 auth-init", 404)
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM login_attempts")

    def reserve_attempt(self, ip, username):
        # Reserve before hashing so concurrent requests cannot bypass the limit.
        now = time.time()
        scopes = ["ip:" + session_digest(ip), "user:" + session_digest(username)]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE attempted_at <= ?", (now - LOGIN_WINDOW,))
            if any(db.execute("SELECT COUNT(*) FROM login_attempts WHERE scope=?", (scope,)).fetchone()[0]
                   >= LOGIN_LIMIT for scope in scopes):
                raise AuthError("登录尝试过多，请一分钟后重试", 429)
            db.executemany("INSERT INTO login_attempts (scope, attempted_at) VALUES (?, ?)",
                           [(scope, now) for scope in scopes])

    def login(self, username, password, ip):
        if not self.configured():
            raise AuthError("请先在服务器本机执行 ./sapperctl auth-init 初始化管理员", 503)
        # Malformed names still consume a source-address attempt.
        name = username.strip() if isinstance(username, str) and len(username) <= 64 else ""
        self.reserve_attempt(ip, name)
        with self.connection() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        with hash_slot():
            valid = password_matches(password, user["password_hash"] if user else _DUMMY_HASH)
        if not valid or user is None:
            raise AuthError("用户名或密码错误", 401)
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            inserted = db.execute("""INSERT INTO sessions (token_hash, user_id, credential_version, csrf, created_at, expires)
                SELECT ?, id, credential_version, ?, ?, ? FROM users WHERE id=? AND credential_version=?""",
                (session_digest(sid), csrf, now, now + SESSION_SECONDS, user["id"], user["credential_version"]))
            if not inserted.rowcount:
                raise AuthError("密码已更改，请重新登录", 401)
        return sid, {"authenticated": True, "username": user["username"], "role": user["role"], "csrf": csrf}

    def session(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", sid):
            return None
        with self.connection() as db:
            row = db.execute("""SELECT s.token_hash, s.csrf, s.user_id, s.credential_version, u.username, u.role
                FROM sessions s JOIN users u ON u.id=s.user_id AND u.credential_version=s.credential_version
                WHERE s.token_hash=? AND s.expires > ?""", (session_digest(sid), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, session):
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (session["token_hash"],))

    def change_password(self, session, current_password, new_password, ip):
        self.reserve_attempt(ip, session["username"])
        with self.connection() as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if user is None or user["credential_version"] != session["credential_version"]:
            raise AuthError("登录已失效，请重新登录", 401)
        with hash_slot():
            if not password_matches(current_password, user["password_hash"]):
                raise AuthError("当前密码错误", 400)
            encoded = password_hash(new_password)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE users SET password_hash=?, credential_version=credential_version+1,
                updated_at=? WHERE id=? AND credential_version=?""",
                (encoded, time.time(), user["id"], user["credential_version"]))
            if not changed.rowcount:
                raise AuthError("密码已更改，请重新登录", 401)
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))


def manage_credentials(state, action, username=None):
    """Passwords enter through a local terminal, never argv, logs, or HTTP setup."""
    if not sys.stdin.isatty():
        raise AuthError("账号管理需要交互式终端，请在本机终端执行此命令")
    store = AuthStore(state)
    if action == "auth-init" and store.configured():
        raise AuthError("管理员已初始化，请使用 password-reset 重置密码", 409)
    username = username_text(username or input("用户名 [admin]: ").strip() or "admin")
    password = getpass.getpass("新密码（至少 6 个字符）: ")
    if password != getpass.getpass("再次输入新密码: "):
        raise AuthError("两次输入的密码不一致")
    if action == "auth-init":
        store.initialize(username, password)
        print(f"管理员 {username} 已初始化，账号保存在 {store.path}")
    else:
        store.reset_password(username, password)
        print(f"管理员 {username} 密码已重置，旧登录会话已撤销。")
