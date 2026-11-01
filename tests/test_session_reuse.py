# -*- coding: utf-8 -*-
"""建会话这件事，一次状态查询里只该付一次钱。

背景（2026-11-01 实测，设备 192.168.0.1，PC 直连同一 WiFi）：

    get_state() 本身只要 3 个请求、约 2.8 秒（wan 0.9s + wlan 0.4s + status1 1.4s），
    但 status() 全程实测 4.8~7.6 秒、4~12 个请求。多出来的全是「建会话」：
      · identify() 建一个客户端认证好，问完就扔；
      · detect() 立刻又 new 一个，从零再认证一遍；
      · 会话刚建好时设备先回 89 字节的「还没就绪」，老代码 sleep 1 秒、
        丢掉 nonce、重新挑战、再走一次 login.cgi，然后三个页面重读一遍。

这里锁住三处修复，任何一处退回去，都会让「点一下先卡几秒」回来。
"""

import unittest
from unittest import mock

import drivers
from drivers.zte_rta import ZteRtaClient, ZteRtaDriver


# 设备回的那种「还没就绪」：200 + 一段 <login_status>，实测 89 字节。
# 注意不能拿随便 89 个字节冒充 —— 老的成功判定是「长度 > 40 就算读到」，
# 假数据不带上这两个关键词，等于在测另一件事。
NOT_READY = (200, "<html><head></head><body><login_status>UNAUTHORIZED"
                  "</login_status></body></html>", {})


class _FakeClient(ZteRtaClient):
    """把网络收发换成一串写好的回应，其余逻辑照跑。"""

    def __init__(self, script, **kw):
        super().__init__("127.0.0.1", "admin", "admin", 5, **kw)
        self.script = list(script)
        self.calls = []

    def _raw(self, path, method="GET", body=None, auth=None):
        self.calls.append(path)
        if not self.script:
            raise AssertionError(f"没预料到的请求：{path}")
        return self.script.pop(0)


def _probe_paths(client):
    """只数「读页面」的那几个请求，login.cgi 单独算。"""
    return [p for p in client.calls if "file=" in p]


class IdentifyHandsBackSession(unittest.TestCase):
    def setUp(self):
        drivers._DETECT_MEMO.clear()

    def tearDown(self):
        drivers._DETECT_MEMO.clear()

    def test_identify_still_returns_bool(self):
        """老接口不能变：命令行和别的驱动还在用它。"""
        real = ZteRtaDriver.identify_client
        try:
            ZteRtaDriver.identify_client = classmethod(
                lambda cls, *a, **kw: (True, None))
            self.assertIs(ZteRtaDriver.identify("h", "u", "p"), True)
        finally:
            ZteRtaDriver.identify_client = real

    def test_detect_reuses_probe_client(self):
        """探测时认证好的客户端要交给驱动，别再建第二个。"""
        sentinel = object()
        with mock.patch.object(
                ZteRtaDriver, "identify_client",
                side_effect=lambda *a, **kw: (True, sentinel)):
            dev = drivers.detect(host="10.0.0.1")
        self.assertIs(dev.c, sentinel, "又白建了一次会话")

    def test_driver_builds_own_client_when_none_handed_over(self):
        """记忆命中时没有会话可复用，驱动得自己建一个，不能是 None。"""
        drivers._DETECT_MEMO[("10.0.0.2", "admin", "admin")] = (
            __import__("time").time(), ZteRtaDriver)
        dev = drivers.detect(host="10.0.0.2")
        self.assertIsInstance(dev.c, ZteRtaClient)


class ChallengeStopsEarly(unittest.TestCase):
    def test_login_page_refusal_does_not_probe_other_pages(self):
        """file=login 明确回「没有会话」时，别再问另外三个页面。

        这台设备上 wan 要 0.9 秒、status1 要 1.4 秒，白问一遍就是 2.3 秒。
        """
        c = _FakeClient([NOT_READY, NOT_READY])   # 第二个留给 login.cgi
        with self.assertRaises(ConnectionError):
            c._challenge()
        self.assertEqual(_probe_paths(c),
                         ["/xml_action.cgi?method=get&module=duster&file=login"])

    def test_real_challenge_on_login_page_is_used(self):
        """能拿到挑战时一切照旧，别把正常路径改坏。"""
        www = {"WWW-Authenticate": 'Digest realm="r", nonce="n1", qop="auth"'}
        c = _FakeClient([(401, "", www)])
        self.assertTrue(c._challenge())
        self.assertEqual(c._nonce["nonce"], "n1")


class ShortReplyRetriesInPlace(unittest.TestCase):
    def setUp(self):
        # 预设一个 nonce，让 _auth() 不会自己去挑战 —— 这样才能验证
        # 「重新挑战」有没有被偷偷触发。
        self.nonce = {"realm": "r", "nonce": "n", "qop": "auth"}

    def _client(self, script):
        c = _FakeClient(script)
        c._nonce = dict(self.nonce)
        return c

    def test_one_short_reply_is_retried_without_new_session(self):
        """89 字节的「还没就绪」→ 原地重发一次就过，不该去重新挑战。"""
        c = self._client([
            NOT_READY,                       # 过渡态
            (200, "<wan>" + "y" * 400, {}),  # 重发就正常了
        ])
        with mock.patch.object(ZteRtaClient, "_challenge",
                               side_effect=AssertionError("不该重新挑战")), \
                mock.patch("drivers.zte_rta.time.sleep"):
            txt = c.get("wan")
        self.assertIn("<wan>", txt)
        self.assertEqual(len(c.calls), 2, "重发一次就该拿到，别多问")

    def test_persistent_failure_still_drops_session(self):
        """真失效时还得回到老路：丢 nonce、重新挑战。"""
        c = self._client([NOT_READY] * 6)
        # i=0 原地重发；i=1 丢掉 nonce；i=2 的 _auth() 会去重新挑战。
        with mock.patch.object(ZteRtaClient, "_challenge") as ch, \
                mock.patch("drivers.zte_rta.time.sleep"):
            ch.side_effect = ConnectionError("挑战失败")
            with self.assertRaises(ConnectionError):
                c.get("wan", retries=2)
        self.assertTrue(ch.called, "持续失败必须重新建立会话")


if __name__ == "__main__":
    unittest.main()
