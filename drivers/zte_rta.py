"""ZTE-RTA 系固件驱动（飞猫 FM_M1 等）。

适配特征：web 后台走 /xml_action.cgi，Digest 认证，realm=Highwmg，
页面路径形如 http://<host>/#/pc/home。

接口上的两个坑（踩过）：
  1. Digest 的 uri 字段必须写死成 /cgi/xml_action.cgi，与实际请求路径
     /xml_action.cgi 不一致，按真实路径算会一直认证失败 / 空响应。
  2. 未认证的 POST 会被设备直接 reset 连接，必须先发 GET 拿到 nonce，
     再带认证发 POST。
"""

import hashlib
import re
import time
import urllib.error
import urllib.request

from .base import DeviceBusy, DeviceDriver, DeviceUnreachable, Unsupported


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _flat(xml):
    return {m.group(1): m.group(2) for m in re.finditer(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", xml)}


class ZteRtaClient:
    """裸接口客户端，只管收发。"""

    DIGEST_URI = "/cgi/xml_action.cgi"

    def __init__(self, host, user, password, timeout):
        self.host, self.user, self.password = host, user, password
        self.timeout = timeout
        self._nc = 0
        self._nonce = None

    def _raw(self, path, method="GET", body=None, auth=None):
        req = urllib.request.Request(f"http://{self.host}{path}", data=body, method=method)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("X-Requested-With", "XMLHttpRequest")
        req.add_header("Cache-Control", "no-store, no-cache, must-revalidate")
        if body is not None:
            req.add_header("Content-Type", "text/xml; charset=US-ASCII")
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "ignore"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore"), dict(e.headers)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            # 切频段时设备射频会重启，web 服务有 5~20 秒不响应，这是正常现象。
            # 这里必须转成可识别的异常，否则会以裸 traceback 冒到最外层，
            # 把整个体检中止在半路（设备就留在别人频段上了，实测踩过）。
            raise DeviceUnreachable(
                f"设备 {self.host} 暂时没有响应（{type(e).__name__}: {e}）。\n"
                "    切频段时设备射频会重启，web 后台通常 5~20 秒后恢复。") from e

    def _login(self):
        """用设备自己的 login.cgi 主动建会话（不需要人工开浏览器）。

        这套流程是抓设备前端**完整请求**得到的，有两个坑：

        坑 1：登录请求里有两份认证，缺一个就不算登录成功
          · URL 参数里的 response —— HA2 = md5("GET:/cgi/protected.cgi")
          · 请求头里的 Authorization: Digest —— HA2 = md5("GET:/cgi/xml_action.cgi")
          只发 URL 那份时，设备照样回 200，但同时再甩你一个新的 WWW-Authenticate
          （这其实是「没通过」），随后访问一律 UNAUTHORIZED / KICKOFF。
          这个现象骗了很久：只看状态码会以为登录成功。

        坑 2：判定成功的依据不是 200，而是「响应里不再带 WWW-Authenticate」。
        """
        code, _txt, hdrs = self._raw("/login.cgi")
        www = hdrs.get("WWW-Authenticate") or hdrs.get("Www-Authenticate")
        if not www:
            return False
        info = {}
        for k in ("realm", "nonce", "qop"):
            m = re.search(k + r'="([^"]*)"', www)
            info[k] = m.group(1) if m else None
        if not info.get("nonce"):
            return False
        qop = (info.get("qop") or "auth").split(",")[0]

        # ① URL 里的 response（protected.cgi）
        cnonce_q = _md5(str(time.time()))[:16]
        ha1 = _md5(f"{self.user}:{info['realm']}:{self.password}")
        q_resp = _md5(f"{ha1}:{info['nonce']}:00000001:{cnonce_q}:{qop}:"
                      f"{_md5('GET:/cgi/protected.cgi')}")
        path = (f"/login.cgi?Action=Digest&userName={self.user}&realm={info['realm']}"
                f"&nonce={info['nonce']}&response={q_resp}&qop={qop}"
                f"&cnonce={cnonce_q}&temp=asr")

        # ② 请求头里的 Digest（xml_action.cgi）—— 少了这一份登录不生效
        self._nonce = info
        self._nc = 0
        auth_header = self._auth("GET")      # 内部把 _nc 加到 1

        code2, _body, hdrs2 = self._raw(path, "GET", None, auth_header)
        still_challenged = bool(hdrs2.get("WWW-Authenticate") or hdrs2.get("Www-Authenticate"))
        if code2 != 200 or still_challenged:
            self._nonce = None
            return False

        # 真的建立了吗？再实测一次（设备状态机偶尔会有延迟）
        try:
            c3, t3, _h3 = self._raw("/xml_action.cgi?method=get&module=duster&file=wan",
                                    "GET", None, self._auth("GET"))
        except DeviceUnreachable:
            return True           # 网络抖动，先当成功，让上层重试决定
        if c3 == 200 and "login_status" not in t3 and len(t3) > 40:
            return True
        self._nonce = None
        return False

    def _challenge(self):
        unauthorized = False
        # 顺序有意义：file=login 是最稳定的挑战来源（前端登录页也走它）。
        # 某些状态下设备会对 wan/status1 直接回 200 + login_status=UNAUTHORIZED，
        # 却仍然对 login 正常发挑战，所以把它排在前面。
        for f in ("login", "wan", "status1", "uapxb_wlan_basic_settings"):
            code, txt, hdrs = self._raw(f"/xml_action.cgi?method=get&module=duster&file={f}")
            www = hdrs.get("WWW-Authenticate") or hdrs.get("Www-Authenticate")
            if not www:
                # 设备可达但拒答：正常固件会先回 401 + 挑战；某些状态下它会直接
                # 回 200 + <login_status>...</login_status>，一个挑战都不给。
                # 已观测到两种值：UNAUTHORIZED（无有效会话）、KICKOFF（会话被顶掉）。
                m = re.search(r"<login_status>([A-Za-z_]+)</login_status>", txt or "")
                if m:
                    unauthorized = m.group(1)
                continue
            info = {}
            for k in ("realm", "nonce", "qop"):
                m = re.search(k + r'="([^"]*)"', www)
                info[k] = m.group(1) if m else None
            if info["qop"]:
                info["qop"] = info["qop"].split(",")[0]
            self._nonce = info
            self._nc = 0
            return True

        # xml_action 一个挑战都不给（KICKOFF / UNAUTHORIZED）→ 主动用 login.cgi 登录。
        # 这条通路在会话被踢时依然有效，所以工具不再需要人工去开浏览器登录。
        if self._login():
            return True
        if unauthorized:
            hint = {
                "KICKOFF": "管理员会话被其他登录顶掉了（KICKOFF）",
                "UNAUTHORIZED": "当前没有有效的管理员会话（UNAUTHORIZED）",
            }.get(unauthorized, f"设备要求重新登录（login_status={unauthorized}）")
            raise DeviceBusy(
                f"{hint}，且设备没有发出 Digest 认证挑战。\n"
                "    自动登录（login.cgi）也没成功，通常是设备正忙或密码不是默认的 admin。\n"
                "    处理：① 确认 --password 与设备后台密码一致；\n"
                "          ② 用浏览器打开后台登录一次，再重试本命令。")

        # 连 login_status 都没有：设备在切换频段时重启射频，这段时间它什么都不回。
        # 这是**临时**状态，必须让上层重试，而不是报「检查地址与网段」——
        # 之前把它当永久错误，导致测完一个死频段后，后面每个频段都被判「通信中断」，
        # 连自动还原都失败，设备被留在没信号的频段上。
        raise DeviceUnreachable("设备暂时不应答（可能在重启射频）")

    def _auth(self, method):
        if not self._nonce:
            # 拿不到会自己抛 DeviceUnreachable / DeviceBusy，不会静默返回 False
            self._challenge()
        i = self._nonce
        self._nc += 1
        nc = format(self._nc, "08x")
        cnonce = "9f8e7d6c5b4a3210"
        ha1 = _md5(f"{self.user}:{i['realm']}:{self.password}")
        ha2 = _md5(f"{method}:{self.DIGEST_URI}")
        if i["qop"]:
            resp = _md5(f"{ha1}:{i['nonce']}:{nc}:{cnonce}:{i['qop']}:{ha2}")
            return (f'Digest username="{self.user}", realm="{i["realm"]}", nonce="{i["nonce"]}", '
                    f'uri="{self.DIGEST_URI}", response="{resp}", qop={i["qop"]}, '
                    f'nc={nc}, cnonce="{cnonce}"')
        resp = _md5(f"{ha1}:{i['nonce']}:{ha2}")
        return (f'Digest username="{self.user}", realm="{i["realm"]}", nonce="{i["nonce"]}", '
                f'uri="{self.DIGEST_URI}", response="{resp}"')

    def get(self, file, retries=3):
        path = f"/xml_action.cgi?method=get&module=duster&file={file}"
        last = (0, "")
        last_err = None
        for i in range(retries + 1):
            try:
                # 不再每次请求都挑战：设备只允许一个管理员会话，
                # 每条请求都重新登录会把浏览器（和工具自己）互相踢掉，
                # 是 KICKOFF 循环的直接来源。_auth() 在缺 nonce 时会自己挑战。
                code, txt, _ = self._raw(path, "GET", None, self._auth("GET"))
            except DeviceUnreachable as e:
                # 设备射频重启期间连不上、或暂时不发挑战：退避重试，
                # 别把临时故障当永久错误（这是把设备留在死频段的根源）
                last_err = e
                time.sleep(min(3 + i * 3, 12))
                continue
            last = (code, txt)
            if code == 200 and len(txt) > 40 and "UNAUTHORIZED" not in txt and "KICKOFF" not in txt:
                return txt
            self._nonce = None          # 会话失效，下次重试重新拿 nonce
            time.sleep(1.0)
        if last_err is not None and last[1] == "":
            raise last_err
        raise ConnectionError(f"读取 {file} 失败（code={last[0]}）")

    def post(self, file, inner_xml, retries=4):
        path = f"/xml_action.cgi?method=set&module=duster&file={file}"
        body = ('<?xml version="1.0" encoding="US-ASCII"?><RGW>' + inner_xml + "</RGW>").encode("ascii")
        last_err = None
        for i in range(retries + 1):
            try:
                return self._raw(path, "POST", body, self._auth("POST"))
            except DeviceUnreachable as e:
                # set_band 是最关键的一步：改不动就没法测、也没法还原。
                # 设备切频段时自己会重启射频，这一刻它不回任何请求，所以多给点时间。
                last_err = e
                self._nonce = None
                time.sleep(min(3 + i * 3, 12))
        raise last_err


class ZteRtaDriver(DeviceDriver):
    name = "zte_rta"
    description = "ZTE-RTA 固件（/xml_action.cgi + Digest），如飞猫 FM_M1"

    BANDS = [3, 8, 38, 39, 40, 41]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.c = ZteRtaClient(self.host, self.user, self.password, self.timeout)

    @classmethod
    def identify(cls, host, user, password, timeout=5) -> bool:
        try:
            c = ZteRtaClient(host, user, password, timeout)
            txt = c.get("wan", retries=0)
            return "<wan>" in txt
        except DeviceBusy:
            raise                      # 设备在但拒答，交给上层说清楚，别当成"不是这个驱动"
        except Exception:  # noqa: BLE001
            return False

    def capabilities(self):
        try:
            w = _flat(self.c.get("uapxb_wlan_basic_settings"))
            lo = int(w.get("first_channel") or 1)
            hi = int(w.get("last_channel") or 13)
            channels = list(range(lo, hi + 1))
            single = w.get("dual_band_support") == "0"
        except Exception:  # noqa: BLE001
            channels, single = list(range(1, 14)), True
        return {
            "bands": list(self.BANDS),
            "channels": channels,
            "can_lock_band": True,
            "can_set_channel": True,
            "wifi_band": "2.4GHz" if single else "2.4GHz + 5GHz",
            "vendor": "ZTE-RTA",
            "model_hint": "飞猫 FM_M1 及其他 ZTE-RTA 系随身WiFi/CPE",
        }

    def get_state(self):
        wan = _flat(self.c.get("wan"))
        try:
            wlan = _flat(self.c.get("uapxb_wlan_basic_settings"))
        except Exception:  # noqa: BLE001
            wlan = {}
        try:
            st = _flat(self.c.get("status1"))
        except Exception:  # noqa: BLE001
            st = {}
        return {
            "band": int(wan.get("Prefer_Band") or 0),
            "channel": int(wlan.get("channel") or 0),
            "bandwidth": {"1": "20MHz", "2": "40MHz"}.get(str(wlan.get("bandwidth")), wlan.get("bandwidth")),
            "mtu": wan.get("mtu"),
            "signal": {
                "band": st.get("lte_band", ""),
                "rsrp": st.get("lte_rsrp", ""),
                "sinr": st.get("lte_sinr", ""),
                "rsrq": st.get("lte_rsrq", ""),
                "pci": st.get("lte_pci", ""),
                "operator": st.get("network_name", ""),
            },
            "raw": {"wan": wan, "wlan": wlan},
        }

    def set_band(self, band: int):
        wan = _flat(self.c.get("wan"))
        cellular = (
            f"<NW_mode>{wan.get('NW_mode', '2')}</NW_mode><NW_mode_action>1</NW_mode_action>"
            f"<prefer_mode>{wan.get('prefer_mode', '0')}</prefer_mode><prefer_mode_action>1</prefer_mode_action>"
            f"<prefer_lte_type>{wan.get('prefer_lte_type', '0')}</prefer_lte_type>"
            f"<prefer_lte_type_action>1</prefer_lte_type_action>"
            f"<Prefer_Band>{int(band)}</Prefer_Band><Prefer_Band_Action>1</Prefer_Band_Action>"
        )
        code, txt, _hdrs = self.c.post(
            "wan",
            f"<wan><cellular>{cellular}</cellular>"
            f"<mtu>{wan.get('mtu', '1500')}</mtu><mtu_action>0</mtu_action></wan>")
        if code != 200:
            raise Unsupported(f"设置频段失败（HTTP {code}）")

    def set_channel(self, channel: int):
        w = _flat(self.c.get("uapxb_wlan_basic_settings"))
        keep = []
        for k in ("wlan_enable", "rf_band", "net_mode", "bandwidth", "max_clients",
                  "wifi_sleep_time", "wifi_sleep_action", "beacon_period", "dtim_interval",
                  "sec_channel", "ap_isolate", "only_20m"):
            if w.get(k) not in (None, ""):
                keep.append(f"<{k}>{w[k]}</{k}>")
        keep.append(f"<channel>{int(channel)}</channel>")
        code, _txt, _hdrs = self.c.post("uapxb_wlan_basic_settings",
                              "<wlan_settings>" + "".join(keep) + "</wlan_settings>")
        if code != 200:
            raise Unsupported(f"设置信道失败（HTTP {code}）")
