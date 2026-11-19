"""Authentication persistence, revocation and concurrent rate-limit regressions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper_auth
from sapper_auth import AuthError, AuthStore, manage_credentials

PASSWORD = "fixture password with spaces 123"
NEW_PASSWORD = "  新的测试密码 with spaces 456  "


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.auth = AuthStore(self.state)

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self):
        self.auth.initialize("admin", PASSWORD)

    def test_unconfigured_store_denies_login_and_requires_local_initialization(self):
        self.assertFalse(self.auth.configured())
        with self.assertRaises(AuthError) as error:
            self.auth.login("admin", PASSWORD, "local")
        self.assertEqual(error.exception.status, 503)
        with mock.patch("sapper_auth.sys.stdin.isatty", return_value=False), self.assertRaises(AuthError):
            manage_credentials(self.state, "auth-init", "admin")

    def test_private_database_contains_hashes_and_survives_reopening(self):
        self.initialize()
        sid, result = self.auth.login("admin", PASSWORD, "local")
        other = AuthStore(self.state)
        self.assertTrue(other.configured())
        self.assertEqual(other.session(sid)["csrf"], result["csrf"])
        self.assertEqual(other.session(sid)["username"], "admin")
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.auth.path.stat().st_mode), 0o600)
        self.assertFalse((self.state / "token").exists())
        with closing(sqlite3.connect(self.auth.path)) as db:
            encoded = db.execute("SELECT password_hash FROM users").fetchone()[0]
            stored_token = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertTrue(encoded.startswith("scrypt$32768$8$3$"))
        self.assertNotEqual(stored_token, sid)
        for secret in (PASSWORD, sid):
            self.assertNotIn(secret.encode(), self.auth.path.read_bytes())

    def test_initialization_is_single_admin_and_does_not_overwrite_account(self):
        self.initialize()
        with self.assertRaises(AuthError) as error:
            self.auth.initialize("another", NEW_PASSWORD)
        self.assertEqual(error.exception.status, 409)
        self.auth.login("admin", PASSWORD, "local")

    def test_wrong_credentials_and_sql_input_are_rejected_uniformly(self):
        self.initialize()
        for username, password in [("admin", "wrong"), ("unknown", PASSWORD), ("' OR 1=1 --", PASSWORD), (None, None)]:
            with self.subTest(username=username), self.assertRaises(AuthError) as error:
                self.auth.login(username, password, "local")
            self.assertEqual((error.exception.status, str(error.exception)), (401, "用户名或密码错误"))

    def test_logout_and_expiry_are_enforced_after_restart(self):
        self.initialize()
        sid, _ = self.auth.login("admin", PASSWORD, "local")
        self.auth.logout(self.auth.session(sid))
        self.assertIsNone(AuthStore(self.state).session(sid))
        sid, _ = self.auth.login("admin", PASSWORD, "local")
        with mock.patch("sapper_auth.time.time", return_value=sapper_auth.time.time() + sapper_auth.SESSION_SECONDS + 1):
            self.assertIsNone(AuthStore(self.state).session(sid))
        for malformed in ("", "bad", "é" * 43, None):
            self.assertIsNone(self.auth.session(malformed))

    def test_password_change_revokes_all_sessions_and_preserves_unicode_and_spaces(self):
        self.initialize()
        first, _ = self.auth.login("admin", PASSWORD, "one")
        second, _ = self.auth.login("admin", PASSWORD, "two")
        session = self.auth.session(first)
        with self.assertRaises(AuthError):
            self.auth.change_password(session, "wrong", NEW_PASSWORD, "one")
        self.assertIsNotNone(self.auth.session(first))
        self.auth.change_password(session, PASSWORD, NEW_PASSWORD, "one")
        other = AuthStore(self.state)
        self.assertIsNone(other.session(first))
        self.assertIsNone(other.session(second))
        for password in (PASSWORD, NEW_PASSWORD.strip()):
            with self.assertRaises(AuthError):
                other.login("admin", password, "local")
        other.login("admin", NEW_PASSWORD, "local")

    def test_local_password_reset_invalidates_sessions_in_running_store(self):
        self.initialize()
        sid, _ = self.auth.login("admin", PASSWORD, "local")
        AuthStore(self.state).reset_password("admin", NEW_PASSWORD)
        self.assertIsNone(self.auth.session(sid))
        self.auth.login("admin", NEW_PASSWORD, "local")
        with self.assertRaises(AuthError):
            self.auth.reset_password("missing", PASSWORD)
        self.auth.login("admin", NEW_PASSWORD, "local")

    def test_password_reset_between_verification_and_session_creation_denies_old_login(self):
        self.initialize()
        original = sapper_auth.password_matches

        def reset_during_verification(password, encoded):
            valid = original(password, encoded)
            AuthStore(self.state).reset_password("admin", NEW_PASSWORD)
            return valid

        with mock.patch("sapper_auth.password_matches", side_effect=reset_during_verification), self.assertRaises(AuthError):
            self.auth.login("admin", PASSWORD, "local")
        with closing(sqlite3.connect(self.auth.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_rate_limits_are_atomic_persistent_and_cover_ip_and_account(self):
        def attempt(_):
            try:
                self.auth.reserve_attempt("local", "admin")
                return True
            except AuthError as exc:
                self.assertEqual(exc.status, 429)
                return False

        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(sum(pool.map(attempt, range(20))), sapper_auth.LOGIN_LIMIT)
        reopened = AuthStore(self.state)
        for ip, name in (("local", "different"), ("different", "admin")):
            with self.assertRaises(AuthError) as error:
                reopened.reserve_attempt(ip, name)
            self.assertEqual(error.exception.status, 429)
        with mock.patch("sapper_auth.time.time", return_value=sapper_auth.time.time() + 61):
            reopened.reserve_attempt("local", "admin")

    def test_password_policy_and_local_prompts(self):
        for password in ("short", " " * 20, "x" * 1025, None):
            with self.subTest(password=type(password)), self.assertRaises(AuthError):
                self.auth.initialize("admin", password)
        with mock.patch("sapper_auth.sys.stdin.isatty", return_value=True), \
                mock.patch("sapper_auth.getpass.getpass", side_effect=["abc123", "abc123"]), \
                mock.patch("builtins.print"):
            manage_credentials(self.state, "auth-init", "admin")
        self.auth.login("admin", "abc123", "local")
        with mock.patch("sapper_auth.sys.stdin.isatty", return_value=True), \
                mock.patch("sapper_auth.getpass.getpass", side_effect=["def456", "def456"]), \
                mock.patch("builtins.print"):
            manage_credentials(self.state, "password-reset", "admin")
        self.auth.login("admin", "def456", "local")

    def test_original_cli_forwards_local_account_management(self):
        import sapper
        for flag, action in (("--init-admin", "auth-init"), ("--reset-password", "password-reset")):
            args = sapper.build_parser().parse_args(["web", "--state", str(self.state), flag, "--username", "admin"])
            with mock.patch("sapper_web.manage_credentials") as manage:
                self.assertEqual(args.func(args), 0)
            manage.assert_called_once_with(self.state.resolve(), action, "admin")


if __name__ == "__main__":
    unittest.main()
