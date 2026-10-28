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

from .base import DeviceBusy, DeviceDriver, Unsupported


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

    def _challenge(self):
        unauthorized = False
        for f in ("wan", "status1", "uapxb_wlan_basic_settings"):
            code, txt, hdrs = self._raw(f"/xml_action.cgi?method=get&module=duster&file={f}")
            www = hdrs.get("WWW-Authenticate") or hdrs.get("Www-Authenticate")
            if not www:
                # 设备可达但拒答：正常固件会先回 401 + 挑战；某些状态下它会直接
                # 回 200 + <login_status>UNAUTHORIZED</login_status>。
                # 这时换驱动/换地址都没用，要把原因说清楚。
                if "UNAUTHORIZED" in (txt or ""):
                    unauthorized = True
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
        if unauthorized:
            raise DeviceBusy(
                "设备返回 login_status=UNAUTHORIZED，且没有发出 Digest 认证挑战。\n"
                "    常见原因：① 后台被浏览器占用（同一时间只允许一个管理员会话）；\n"
                "             ② 登录失败次数过多被临时锁定；③ 设备刚重启还没就绪。\n"
                "    处理：关掉浏览器里的设备后台，或拔插一次设备电源，等 30 秒后重试。")
        return False

    def _auth(self, method):
        if not self._nonce and not self._challenge():
            raise ConnectionError("设备没有返回认证挑战，检查地址与网段")
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

    def get(self, file, retries=2):
        path = f"/xml_action.cgi?method=get&module=duster&file={file}"
        last = (0, "")
        for _ in range(retries + 1):
            self._challenge()
            code, txt, _ = self._raw(path, "GET", None, self._auth("GET"))
            last = (code, txt)
            if code == 200 and len(txt) > 40:
                return txt
            time.sleep(1.0)
        raise ConnectionError(f"读取 {file} 失败（code={last[0]}）")

    def post(self, file, inner_xml):
        path = f"/xml_action.cgi?method=set&module=duster&file={file}"
        body = ('<?xml version="1.0" encoding="US-ASCII"?><RGW>' + inner_xml + "</RGW>").encode("ascii")
        self._challenge()
        return self._raw(path, "POST", body, self._auth("POST"))


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
        code, txt = self.c.post(
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
        code, _ = self.c.post("uapxb_wlan_basic_settings",
                              "<wlan_settings>" + "".join(keep) + "</wlan_settings>")
        if code != 200:
            raise Unsupported(f"设置信道失败（HTTP {code}）")
