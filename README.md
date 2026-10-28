# fm-autotune

飞猫 / ZTE-RTA 系 4G 随身WiFi 的**自动调优工具**：扫描周边 2.4G 信道占用、实测各 LTE 频段表现，自动选出当前环境下最优的组合。

纯 Python 标准库实现，**零第三方依赖**，不需要刷机、不需要连厂商云平台。

```
$ python autotune.py
========================================================
当前状态：频段 BAND 3 | 信道 13 | 带宽 1 | MTU 1500
========================================================

[1/2] 扫描周边 2.4G 信道（3 轮合并）…
      发现 15 个邻居 AP
      当前信道 13 评分 1.65 ｜ 最优信道 7 评分 0.87
      最优信道的重叠源：DUFE(ch6,50%,重叠15MHz)；Bunbunii(ch6,88%,重叠15MHz)
[2/2] LTE 当前频段：BAND 3  延迟 138ms  丢包 0%
--------------------------------------------------------
建议：信道 13 -> 7 ｜ 频段 BAND 3 -> 保持
（只检测模式，未改动设备。加 --apply 才会应用）
```

## 为什么需要它

随身WiFi 的实际体验高度依赖**所处位置**：同一个设备在宿舍能跑 51 Mbps，换个教室可能只有 5 Mbps，因为

- 2.4G 信道是公共资源，邻居的路由器一开，你原来的"最干净信道"就不干净了；
- LTE 各频段的覆盖和拥塞程度随地而异，设备"自动"选的频段往往是信号最强而不是最快的那个。

手动试一遍要十几分钟，而且环境一变就白测。这个工具把「扫描 → 实测 → 排序 → 应用」自动化，每到一个新环境跑一次即可。

## 工作原理

1. **WiFi 信道**：多轮 `netsh wlan show networks mode=bssid` 合并扫描（单轮一定漏），把每个邻居 AP 的信道和信号强度换算成对 13 条候选信道的干扰权重，取总分最低的。
2. **LTE 频段**：逐个锁定频段 → 等重新注册 → 测延迟和 4 路并发下行带宽 → 排序取最优。无服务的频段会被标记跳过。
3. **应用**：只改「频段锁定」和「信道」两项，改完校验是否生效、能否重新上网，失败自动回滚。

### 为什么要多轮测量

单次测量会被瞬时抖动带偏——一次刚好撞上基站调度空档，就会得出完全错误的结论。所以：

- **信道扫描默认 5 轮**：同一 AP 只在出现 ≥2 轮时计入（滤掉一闪而过的幽灵 AP），信号强度取多轮中位数；
- **频段实测默认 3 轮**：带宽和延迟都取中位数，同时输出 `min~max` 区间，让你看出这个频段稳不稳；
- **改动阈值**：信道评分改善不足 0.30、或频段带宽改善不足 3 Mbps，就不改——避免为了噪声频繁折腾。

轮数可调：`--scan-rounds 7`、`--rounds 5`。

## 安装

无需 `pip install`，Python 3.8+ 即可：

```bash
git clone https://github.com/<you>/fm-autotune.git
cd fm-autotune
python autotune.py            # 只体检
```

要求：

- 电脑已连上这台随身WiFi（USB 共享或 WiFi 均可）
- Windows（信道扫描依赖 `netsh wlan`；Linux/macOS 需自行替换扫描实现）
- 设备 web 后台可访问（默认 `http://192.168.0.1`，账号 `admin/admin`）

非默认地址或密码：

```bash
python autotune.py --host 192.168.1.1 --user admin --password 你的密码
```

## 用法

```bash
python autotune.py                      # 只检测，输出建议，绝不改设备
python autotune.py --apply              # 体检并应用最优设置
python autotune.py --apply --bands all  # 六个频段全测（慢，约 5 分钟）
python autotune.py --wifi-only          # 只优化 WiFi 信道
python autotune.py --lte-only           # 只优化 LTE 频段
python autotune.py --restore            # 还原为「频段不锁定 + 信道自动」
```

### 安全设计

| 措施 | 说明 |
|---|---|
| 默认只读 | 不加 `--apply` 绝不写设备 |
| 改动范围最小 | 只碰 `Prefer_Band` 和 `channel`，其余字段回填原值 |
| 自动回滚 | 换频段后 26 秒内无法上网，自动切回原频段 |
| 状态留档 | 改动前写入 `state.json`，`--restore` 一键还原 |
| 趋势记录 | 每次运行追加 `autotune-log.jsonl`，可用来观察时段规律 |

**注意**：`--apply` 期间会短暂断网（换频段需要重新注册，每个频段约 26 秒），正在下载或开会时别跑。

## 架构：引擎与设备解耦

这个项目**不绑定任何具体型号**。三层结构：

```
autotune.py          引擎：扫描、测量、决策、回滚 —— 完全不知道设备是什么牌子
      ↓ 只依赖抽象接口
drivers/base.py      DeviceDriver 抽象基类（get_state / set_band / set_channel / capabilities）
      ↓ 具体实现
drivers/zte_rta.py   已验证：ZTE-RTA 固件（飞猫 FM_M1 等）
drivers/template.py  新机型从这里抄
```

引擎只用 `DeviceDriver` 定义的 5 个方法，所以：

- 你的设备**不支持锁频段**？把 `capabilities()["can_lock_band"]` 设成 `False`，引擎自动跳过频段部分，只优化信道。
- 你的设备**不支持改信道**？同理。
- 你的设备频段是 **B1/B5/B7 等其他组合**？在 `capabilities()["bands"]` 里声明即可，引擎不写死频段表。

### 已适配

| 驱动 | 适配范围 | 状态 |
|---|---|---|
| `zte_rta` | ZTE-RTA 固件（`/xml_action.cgi` + Digest，realm=`Highwmg`），如飞猫 FM_M1 | ✅ 已验证 |
| — | 其他 ZTE-RTA 系 CPE / 随身WiFi | 理论可用，欢迎反馈 |

```bash
python autotune.py --list-drivers     # 看当前装了哪些驱动
python autotune.py --driver zte_rta   # 跳过自动探测，指定驱动
```

### 怎么适配新机型

判断能不能做，只看一件事：抓一下设备 web 后台的请求，**有没有「读当前频段/信道」和「写频段/信道」的接口**。两个都有就能做，只是字段名不同。

```bash
cp drivers/template.py drivers/yourbrand.py   # 1. 复制模板
# 2. 实现 5 个方法
# 3. 在 drivers/__init__.py 的 DRIVERS 里加一行
python autotune.py --driver yourbrand          # 4. 验证
```

已经知道的、不走这套接口的路线：

- **Qualcomm MSM8916 平台**（UZ801 / UFI-001 等可刷 OpenWrt 的随身WiFi）：用 [msm89xx](https://github.com/ev63nii/msm89xx)
- **华为 HiLink**（E3372 等）：走 `api/hilink` 系列接口
- **已刷 OpenWrt 的设备**：直接用 `uci` / LuCI，本工具的价值在于「测量+决策」这层，可以只复用 `autotune.py` 的评分逻辑

## 目录结构

```
fm-autotune/
├── autotune.py            引擎（命令行入口）
├── server.py              本地 Web 面板服务
├── web/                   手机端面板（PWA）
│   ├── index.html
│   ├── manifest.webmanifest
│   └── icon.svg
└── drivers/
    ├── base.py            设备抽象基类
    ├── zte_rta.py         ZTE-RTA 实现
    ├── template.py        新机型模板
    └── __init__.py        注册表 + 自动探测
```

## 手机面板

```bash
python server.py --lan       # 手机和电脑连同一个 Wi-Fi
```

手机浏览器打开终端里打印的 `http://<电脑IP>:8787`，iOS/Android 都可以「添加到主屏幕」，用起来跟 App 一样。面板上可以看实时信号、扫描信道（带柱状图）、一键体检/优化/恢复。

> 面板本身不含密码，但它能操作你家的路由器，**别把 8787 端口映射到公网**。

## 接口备忘

给想适配其他机型的开发者：

```
GET  /xml_action.cgi?method=get&module=duster&file=wan
GET  /xml_action.cgi?method=get&module=duster&file=uapxb_wlan_basic_settings
POST /xml_action.cgi?method=set&module=duster&file=wan
POST /xml_action.cgi?method=set&module=duster&file=uapxb_wlan_basic_settings
```

两个坑：

1. Digest 认证的 `uri` 字段必须写成 **`/cgi/xml_action.cgi`**，与实际请求路径 `/xml_action.cgi` 不一致，按实际路径算会一直 401/空响应。
2. **未认证的 POST 会被设备直接 reset 连接**，必须先发一个 GET 取到 `nonce`，再带认证发 POST。

频段字段：`Prefer_Band`（0 = 不锁定，否则填 LTE 频段号如 3/40）。  
信道字段：`channel`（0 = 自动，1–13）。

## Roadmap

- [ ] 手机端面板（PWA，局域网访问）
- [ ] 按时段自动切换（白天/晚高峰用不同频段）
- [ ] 长期趋势图（从 `autotune-log.jsonl` 生成）
- [ ] 更多机型适配

## License

MIT
