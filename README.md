# mifi-autotune

给 4G 随身WiFi / CPE 用的**自动调优工具**：扫一遍周围 2.4G 信道占用，逐个实测 LTE 频段，算出当前环境下最快的组合，然后一键应用。

纯 Python 标准库，**零第三方依赖**，不刷机、不连厂商云、不需要 root。

```
$ python autotune.py --apply
========================================================
设备：ZTE-RTA 固件（/xml_action.cgi + Digest），如飞猫 FM_M1
当前状态：频段 BAND 40 | 信道 6 | 带宽 20MHz | MTU 1500
信号：RSRP -84dBm | SINR 24dB | PCI 441
========================================================

[1/2] 扫描周边 2.4G 信道（5 轮合并，出现不足 2 轮的 AP 视为幽灵丢弃）…
      已排除自己的 AP「月亮糖」
      发现 8 个邻居 AP
      当前信道 6 评分 2.18 ｜ 最优信道 13 评分 0.00
      最优信道无重叠
[2/2] 实测 LTE 频段 [3, 40]（每频段 3 轮取中位数，约 96 秒/个，期间会短暂断网）…
      BAND 3  可用   48.6 Mbps [45.1~51.2]   延迟 56ms  峰值 78ms   丢包 0%
      BAND 40 可用   22.9 Mbps [21.4~24.0]   延迟 57ms  峰值 332ms  丢包 0%

      最优频段：BAND 3（中位 48.6 Mbps，单轮 [45.1, 51.2, 49.4]）

--------------------------------------------------------
建议：信道 6 -> 13 ｜ 频段 BAND 40 -> BAND 3

[应用] 锁定 BAND 3 …
[√] 已生效，延迟 56ms
[应用] 切换 WiFi 信道到 13 …
[√] 已生效
```

## 为什么需要它

随身WiFi 的实际体验高度依赖**所处位置**，而设备自己的「自动」往往选得很糟：

- **2.4G 信道是公共资源。** 邻居的路由器一开，你昨天"最干净的信道"今天就废了。绝大多数设备出厂默认信道 6，于是几十户人家挤在同一条 20MHz 上。
- **LTE 频段的覆盖和拥塞随地而异。** 设备选频段看的是信号强度，不是速度——信号满格但只有 20 Mbps 是常态，因为那个频段容量被占满了。

手动试一轮要十几分钟，换个地方又要重来。这个工具把「扫描 → 实测 → 排序 → 应用」自动化，每到一个新环境跑一次就行。

## 工作原理

### 一、信道：把邻居折算成分数

对 13 条候选信道各算一个干扰分：

```
score(信道) = Σ  邻居  (信号%/100)² × (重叠宽度 / 20MHz)
```

- 重叠宽度 = `20 - |两者中心频率差|`，只有正重叠才计入；
- 信号项取**平方**而不是线性，因为 90% 的邻居造成的影响远大于 50% 的；
- 分数最低的信道胜出。

### 二、频段：直接实测，不看信号格

逐个锁定频段 → 等重新注册（26 秒）→ 4 路并发下载测带宽 + 10 包延迟 → 排序。无服务的频段直接标记跳过。

### 三、为什么必须多轮

单次测量会被瞬时抖动带偏：一次刚好撞上基站调度空档，就会得出完全错误的结论。所以：

| 项目 | 默认 | 处理方式 |
|---|---|---|
| 信道扫描 | **5 轮** | 同一 AP 出现 ≥2 轮才计入（滤掉幽灵 AP）；信号取多轮**中位数** |
| 频段实测 | **3 轮** | 带宽、延迟取**中位数**，同时输出 `min~max` 看波动 |
| 是否切换 | — | 信道评分改善 <0.30、或带宽提升 <3 Mbps，**就不改** |

最后一行是关键：环境噪声带来的 0.01 分差不会被当成"更优"，否则工具会为了噪声反复折腾你的网络。

轮数可调：`--scan-rounds 7`、`--rounds 5`。

## 平台支持

| 系统 | 信道扫描 | 状态 |
|---|---|---|
| Windows 10/11 | `netsh wlan show networks mode=bssid` | ✅ 已验证。字段名多语言适配（中/英/日/韩/德/法/西/意/葡/俄/繁） |
| Linux | `nmcli` → 失败回落 `iw` | ⚠️ 已实现，未实机验证。`iw` 需 root 才能主动扫描 |
| macOS | `airport -s` | ⚠️ macOS 14+ 已移除 `airport`，届时需自行替换扫描实现 |

系统语言不在支持列表里时，工具会**明确跳过信道优化**而不是拿空数据乱改。

## 安装

无需 `pip install`，Python 3.8+ 即可：

```bash
git clone https://github.com/Healton1314/mifi-autotune.git
cd mifi-autotune
python autotune.py            # 只体检，不改设备
```

前置条件：

- 电脑已连上这台随身WiFi（USB 共享或 WiFi 均可）
- 设备 web 后台可访问（默认 `http://192.168.0.1`，账号 `admin/admin`）
- 有 `curl`（Windows 10+ 自带；无 curl 会自动回落 urllib）

非默认地址或密码：

```bash
python autotune.py --host 192.168.1.1 --user admin --password 你的密码
```

## 用法

```bash
python autotune.py                      # 只检测，输出建议，绝不改设备
python autotune.py --apply              # 体检并应用最优设置
python autotune.py --apply --bands all  # 六个频段全测（慢，约 8 分钟）
python autotune.py --wifi-only          # 只优化 WiFi 信道
python autotune.py --lte-only           # 只优化 LTE 频段
python autotune.py --restore            # 还原为「频段不锁定 + 信道自动」
python autotune.py --list-drivers       # 看当前装了哪些设备驱动
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--ssid 名称` | 指定自己设备的 WiFi 名，扫描时排除（见下方说明） |
| `--ping-host` | 延迟探测目标，默认 `223.5.5.5` |
| `--speed-url` | 测速下载地址，默认 Cloudflare；国内建议换成国内大文件镜像 |
| `--rounds` / `--scan-rounds` | 频段测量轮数 / 信道扫描轮数 |

### 关于「排除自己的 AP」

扫信道时如果把自己设备的 AP 也算成邻居，就会得出"我当前这条信道很挤"的错误结论——于是把你赶到一条更差的信道上。所以必须排除。

识别顺序：

1. `--ssid` 参数
2. `config.json` 里记住的 `own_ssid`
3. 当前电脑连着的 WiFi 名（走 WiFi 时自动识别）
4. 都没有（例如电脑走 USB）→ 退化为「信号最强的那台大概率是自己」，并会**明确提示你猜的是谁**

第 4 步只是兜底，猜错会明显影响结论。知道自己 SSID 的话，跑一次 `--ssid 你的WiFi名`，之后会记在 `config.json` 里。

## 安全设计

| 措施 | 说明 |
|---|---|
| **默认只读** | 不加 `--apply` 绝不写设备 |
| **改动范围最小** | 只碰 `Prefer_Band` 和 `channel`，其余字段一律回填原值 |
| **自动回滚** | 换频段后 26 秒内无法上网，自动切回原频段 |
| **状态留档** | 改动前写入 `state.json`，`--restore` 一键还原 |
| **失败不改** | 信道写入后读回校验，未生效则恢复原值 |
| **趋势记录** | 每次运行追加 `autotune-log.jsonl`，可用来观察时段规律 |

**注意**：`--apply` 期间会短暂断网（换频段需要重新注册，每个频段约 26 秒；默认测 2 个频段，合计约 4 分钟）。正在下载或开会时别跑。

## ⚠️ 带出门之前请先解锁频段

**这是使用本工具最容易踩的坑，务必读完。**

锁定频段是把设备**钉死在一个频段**上。在宿舍/家里它更快，但换个地方，当地基站可能根本不用那个频段，结果就是：**信号满格却上不了网**，或者直接无服务。而且这个现象很难自己联想到是频段锁的问题。

```bash
python autotune.py --restore     # 出门前跑一次，回到「自动 + 不锁定」
```

回来后想再锁定，跑一次 `--apply` 即可。

不想来回切的话也可以一直不锁——代价是设备自动选的那个频段可能只有锁定方案的 40~50%（看当地哪个频段拥塞）。

其他几项设置（信道、带宽、加密）跟地理位置无关，可以一直留着。

## 架构：引擎与设备解耦

这个项目**不绑定任何具体型号**：

```
autotune.py          引擎：扫描、测量、决策、回滚 —— 不知道设备是什么牌子
scanner.py           2.4G 扫描层：Windows / Linux / macOS 三种来源
probe.py             测速与延迟层：与系统语言无关的解析
      ↓ 只依赖抽象接口
drivers/base.py      DeviceDriver 抽象基类（get_state / set_band / set_channel / capabilities / identify）
      ↓ 具体实现
drivers/zte_rta.py   已验证：ZTE-RTA 固件（飞猫 FM_M1 等）
drivers/template.py  新机型从这里抄
```

引擎只用 `DeviceDriver` 定义的 5 个方法，所以：

- 设备**不支持锁频段**？`capabilities()["can_lock_band"] = False`，引擎自动跳过频段部分，只优化信道。
- 设备**不支持改信道**？同理。
- 设备频段是 **B1/B5/B7** 等其他组合？在 `capabilities()["bands"]` 里声明，引擎不写死频段表。

### 已适配

| 驱动 | 适配范围 | 状态 |
|---|---|---|
| `zte_rta` | ZTE-RTA 固件（`/xml_action.cgi` + Digest，realm=`Highwmg`），如飞猫 FM_M1 | ✅ 已验证 |
| — | 其他 ZTE-RTA 系 CPE / 随身WiFi | 理论可用，欢迎反馈 |

### 怎么适配新机型

判断能不能做，只看一件事：抓一下设备 web 后台的请求，**有没有「读当前频段/信道」和「写频段/信道」的接口**。两个都有就能做，只是字段名不同。

```bash
cp drivers/template.py drivers/yourbrand.py   # 1. 复制模板
# 2. 实现 5 个方法
# 3. 在 drivers/__init__.py 的 DRIVERS 里加一行
python autotune.py --driver yourbrand          # 4. 验证
```

已经知道、但不走这套接口的路线：

- **Qualcomm MSM8916 平台**（UZ801 / UFI-001 等可刷 OpenWrt 的随身WiFi）：用 [msm89xx](https://github.com/ev63nii/msm89xx)
- **华为 HiLink**（E3372 等）：走 `api/hilink` 系列接口
- **已刷 OpenWrt 的设备**：直接用 `uci` / LuCI；本工具的价值在「测量 + 决策」这层，可以只复用 `scanner.py` 与评分逻辑

## 手机面板（PWA）

```bash
python server.py --lan       # 手机和电脑连同一个 Wi-Fi
```

手机浏览器打开终端里打印的 `http://<电脑IP>:8787`，iOS/Android 都能「添加到主屏幕」，用起来跟 App 一样。面板上可以看实时信号、扫描信道（带柱状图）、一键体检 / 优化 / 恢复。

> 面板不含登录态，但它能操作你的路由器，**别把 8787 端口映射到公网**。

## 目录结构

```
mifi-autotune/
├── autotune.py            引擎（命令行入口）
├── scanner.py             2.4G 扫描层（跨平台 + 多语言）
├── probe.py               带宽 / 延迟测量层
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

运行期产物（`state.json`、`config.json`、`autotune-log.jsonl`）已在 `.gitignore` 里。

## 接口备忘

给想适配其他机型的开发者：

```
GET  /xml_action.cgi?method=get&module=duster&file=wan
GET  /xml_action.cgi?method=get&module=duster&file=uapxb_wlan_basic_settings
GET  /xml_action.cgi?method=get&module=duster&file=status1
POST /xml_action.cgi?method=set&module=duster&file=wan
POST /xml_action.cgi?method=set&module=duster&file=uapxb_wlan_basic_settings
```

三个坑（都踩过）：

1. Digest 认证的 `uri` 字段必须写成 **`/cgi/xml_action.cgi`**，与实际请求路径 `/xml_action.cgi` 不一致，按实际路径算会一直 401 / 空响应。
2. **未认证的 POST 会被设备直接 reset 连接**，必须先发一个 GET 取到 `nonce`，再带认证发 POST。
3. 公开接口（如 `file=locale`）**不返回认证挑战**，不能用它取 `nonce`，要用需要认证的 `wan` / `status1` 探测。

字段：`Prefer_Band`（0 = 不锁定，否则填 LTE 频段号如 3/40）、`channel`（0 = 自动，1–13）、`bandwidth`（1 = 20MHz，2 = 40MHz）。

## 局限（说清楚，别抱错期待）

- **只能优化到硬上限为止。** 4G CAT4 设备的空口上限就那么大，晚高峰基站拥塞属于运营商侧问题，任何设备设置都救不了。
- **不绕过运营商限速。** 签约速率（QoS）绑在 SIM 卡上，设备侧改不了；本工具也不做这类事。
- **不处理 5G WiFi。** 只扫 2.4G，因为多数随身WiFi 是单频 2.4G 设备。
- **不改信道带宽和加密。** 属于策略问题，交给用户自己决定。
- **切换会断网。** 这是切频段的物理代价，无法避免。

## Roadmap

- [x] 多轮测量 + 中位数 + 切换阈值
- [x] 驱动化架构，不绑定型号
- [x] 手机端面板（PWA，局域网访问）
- [ ] 按时段自动切换（白天 / 晚高峰用不同频段）
- [ ] 从 `autotune-log.jsonl` 生成长期趋势图
- [ ] 更多机型驱动（欢迎 PR）
- [ ] Linux / macOS 实机验证

## English

**mifi-autotune** automatically finds the best LTE band and 2.4GHz WiFi channel for your 4G dongle / MiFi / CPE — it scans neighbouring APs, measures each band with multiple rounds, and applies the winner.

- Zero dependencies (Python 3.8+ standard library only)
- Read-only by default; `--apply` to write. Auto-rollback if the new band can't get online
- Driver-based: the engine knows nothing about brands. `drivers/zte_rta.py` is verified; copy `drivers/template.py` to add yours
- Ships with a local PWA dashboard for your phone (`python server.py --lan`)

```bash
python autotune.py            # report only
python autotune.py --apply    # measure and apply
python autotune.py --restore  # unlock band + auto channel  ← do this before travelling
```

## License

MIT
