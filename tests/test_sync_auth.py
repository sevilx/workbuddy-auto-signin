"""sync-auth.py 的契约测试。

重点不是覆盖率，而是锁住两条硬约束：
1. 生成的明文凭据必须仍被 signin.py 接受（上游改 _validate_session 时立刻暴露）
2. 明文里不能夹带 refreshToken 之类不需要的凭据

用 unittest 而非 pytest：上游 CI 跑的是 `python -m unittest discover -s tests`，
不装第三方测试框架。
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import signin  # noqa: E402


def _load_sync_auth():
    # 文件名带连字符，不能直接 import
    spec = importlib.util.spec_from_file_location("sync_auth", ROOT / "sync-auth.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync_auth = _load_sync_auth()

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"


def _meta(**over):
    meta = {
        "kind": "sym-v1",
        "source": os.path.join("fake", "workbuddy-desktop.info"),
        "domain": "www.workbuddy.cn",
        "endpoint": signin.DEFAULT_ENDPOINT,
        "uid": "f0e33edd-f57b-4022-ab3a-7ecd8e98e798",
        "enterpriseId": "",
        "expiresAt": 1796262562984,
        "refreshExpiresAt": 1798854561984,
    }
    meta.update(over)
    return meta


class IsolatedConfigMixin:
    """隔离掉开发机上的 sync-config.json 与 SYNC_* 环境变量。"""

    def setUp(self):
        self._orig_config = sync_auth.CONFIG_FILE
        self._orig_cwd = os.getcwd()
        sync_auth.CONFIG_FILE = os.path.join(
            tempfile.gettempdir(), "sync-config-should-not-exist.json")
        self._orig_env = {k: v for k, v in os.environ.items() if k.startswith("SYNC_")}
        for key in list(self._orig_env):
            os.environ.pop(key, None)
        os.chdir(tempfile.gettempdir())

    def tearDown(self):
        sync_auth.CONFIG_FILE = self._orig_config
        os.chdir(self._orig_cwd)
        for key in [k for k in os.environ if k.startswith("SYNC_")]:
            os.environ.pop(key, None)
        os.environ.update(self._orig_env)


def _args(**over):
    base = {"host": None, "user": None, "port": None, "identity": None,
            "remote_dir": None, "remote_python": None, "ssh": None}
    base.update(over)
    return type("Args", (), base)()


class TestPayloadContract(unittest.TestCase):
    def test_payload_accepted_by_signin(self):
        """最小凭据必须能通过 signin.py 的结构校验。"""
        payload = sync_auth.build_payload(TOKEN, _meta())
        signin._validate_session(payload)   # 不抛异常即通过

    def test_payload_builds_headers(self):
        """校验通过还不够，还得能拼出正确的请求头。"""
        payload = sync_auth.build_payload(TOKEN, _meta())
        headers = signin.build_headers(payload)
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["X-User-Id"], "f0e33edd-f57b-4022-ab3a-7ecd8e98e798")
        self.assertEqual(headers["X-Domain"], "www.workbuddy.cn")

    def test_enterprise_headers_only_when_present(self):
        without = signin.build_headers(sync_auth.build_payload(TOKEN, _meta()))
        self.assertNotIn("X-Enterprise-Id", without)
        with_ent = sync_auth.build_payload(TOKEN, _meta(enterpriseId="ent-123"))
        self.assertEqual(signin.build_headers(with_ent)["X-Enterprise-Id"], "ent-123")

    def test_no_refresh_token_leaks(self):
        """明文里只放 access token，绝不带 refresh token。"""
        raw = json.dumps(sync_auth.build_payload(TOKEN, _meta()), ensure_ascii=False)
        self.assertNotIn("refreshToken", raw)
        self.assertNotIn("refresh", raw.lower())

    def test_export_is_minimal(self):
        payload = sync_auth.build_payload(TOKEN, _meta())
        self.assertEqual(set(payload), {"auth", "account", "_sync"})
        self.assertEqual(set(payload["auth"]), {"accessToken", "endpoint", "domain"})
        self.assertEqual(set(payload["account"]), {"uid"})
        self.assertEqual(json.loads(json.dumps(payload))["auth"]["accessToken"], TOKEN)

    def test_domain_omitted_when_absent(self):
        payload = sync_auth.build_payload(TOKEN, _meta(domain=""))
        self.assertNotIn("domain", payload["auth"])
        signin._validate_session(payload)   # 没 domain 也必须仍可用


class TestTokenFormatGuards(unittest.TestCase):
    def test_invalid_token_rejected_by_signin(self):
        """脚本不自己发明校验，非法 token 交给上游判定。

        注意分工：_validate_session 只管字段是否齐全，token 字符集与长度
        由 build_headers -> _valid_token 把关。
        """
        for bad in ("has space", "new\nline", "中文", "a" * 32769):
            with self.subTest(token=bad[:20]):
                payload = sync_auth.build_payload(bad, _meta())
                with self.assertRaises(signin.AuthError):
                    signin.build_headers(payload)

    def test_missing_uid_rejected(self):
        payload = sync_auth.build_payload(TOKEN, _meta(uid=""))
        with self.assertRaises(signin.AuthError):
            signin._validate_session(payload)


class TestHelpers(unittest.TestCase):
    def test_shell_quote(self):
        self.assertEqual(sync_auth._shell_quote("/opt/x"), "'/opt/x'")
        for bad in ("", "a'b", "a\nb", "a\x00b"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    sync_auth._shell_quote(bad)

    def test_days_left(self):
        # _days_left 向下取整，所以留出余量避免浮点边界把 5 算成 4；
        # 过期侧同理：-2.0014 天会被算成 -3（floor 除法），只要判负即可。
        now_ms = int(time.time()) * 1000
        self.assertEqual(sync_auth._days_left(now_ms + (5 * 86400 + 120) * 1000), 5)
        self.assertLessEqual(sync_auth._days_left(now_ms - (2 * 86400 + 120) * 1000), -2)
        self.assertIsNone(sync_auth._days_left(None))
        self.assertIsNone(sync_auth._days_left("not-a-number"))

    def test_ms_to_iso(self):
        self.assertTrue(sync_auth._ms_to_iso(1796262562984).startswith("2026-12-03"))
        self.assertEqual(sync_auth._ms_to_iso(None), "")
        self.assertEqual(sync_auth._ms_to_iso("x"), "")

    def test_ts_to_text(self):
        self.assertNotEqual(sync_auth._ts_to_text(0), "未知")
        self.assertEqual(sync_auth._ts_to_text(None), "未知")

    def test_token_fingerprint(self):
        a = sync_auth._token_fingerprint(TOKEN)
        self.assertEqual(a, sync_auth._token_fingerprint(TOKEN))
        self.assertEqual(len(a), 16)
        self.assertNotIn(TOKEN, a)


class TestConfig(IsolatedConfigMixin, unittest.TestCase):
    def test_defaults_are_placeholders(self):
        """仓库里不能有真实内网地址。"""
        self.assertEqual(sync_auth.DEFAULTS["host"], "192.168.1.100")
        self.assertEqual(sync_auth.DEFAULTS["user"], "root")

    def test_remote_auth_derived_from_dir(self):
        cfg = sync_auth._merge_config(_args(remote_dir="/srv/wb/"))
        self.assertEqual(cfg["remote_auth"], "/srv/wb/auth.json")
        self.assertEqual(cfg["remote_dir"], "/srv/wb")   # 尾部斜杠被去掉

    def test_env_overrides(self):
        os.environ["SYNC_HOST"] = "10.0.0.9"
        os.environ["SYNC_PORT"] = "2222"
        cfg = sync_auth._merge_config(_args())
        self.assertEqual(cfg["host"], "10.0.0.9")
        self.assertEqual(cfg["port"], 2222)

    def test_cli_overrides_env(self):
        os.environ["SYNC_HOST"] = "10.0.0.9"
        cfg = sync_auth._merge_config(_args(host="192.0.2.50"))
        self.assertEqual(cfg["host"], "192.0.2.50")


class TestSshDiscovery(unittest.TestCase):
    def test_avoids_msys_ssh_on_windows(self):
        if os.name != "nt":
            self.skipTest("仅 Windows")
        found = sync_auth.find_ssh()
        self.assertIsNotNone(found)
        # 必须避开 MSYS/Git 版：它会把远端 /opt/... 改写成 Windows 路径
        self.assertNotIn("PortableGit", found)


if __name__ == "__main__":
    unittest.main()
