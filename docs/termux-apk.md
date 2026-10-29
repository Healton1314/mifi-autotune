# 装 Termux 和 Termux:API（应用商店下不到时看这里）

应用商店里搜不到 Termux、或者不让装，用下面任意一条路都行。**本仓库自带一份固定版本的安装包，即使以后这个仓库不再更新，这些链接也照样能下。**

## 一、最快：从本仓库下载（固定版本，已校验）

下载页：<https://github.com/Healton1314/mifi-autotune/releases/tag/termux-apk-2025.10>

直链（用 tag 固定版本，不依赖「latest」，将来本仓库发别的版本也不会失效）：

| 文件 | 大小 | 用途 |
|---|---|---|
| [`com.termux-0.118.3-fdroid.apk`](https://github.com/Healton1314/mifi-autotune/releases/download/termux-apk-2025.10/com.termux-0.118.3-fdroid.apk) | 108.6 MB | Termux 主程序（通用包） |
| [`com.termux.api-0.53.0-fdroid.apk`](https://github.com/Healton1314/mifi-autotune/releases/download/termux-apk-2025.10/com.termux.api-0.53.0-fdroid.apk) | 3.8 MB | Termux:API 插件 |
| [`termux-app-0.118.3-github-arm64.apk`](https://github.com/Healton1314/mifi-autotune/releases/download/termux-apk-2025.10/termux-app-0.118.3-github-arm64.apk) | 33.5 MB | 主程序，GitHub 构建，arm64 |
| [`termux-app-0.118.3-github-armv7.apk`](https://github.com/Healton1314/mifi-autotune/releases/download/termux-apk-2025.10/termux-app-0.118.3-github-armv7.apk) | 30.8 MB | 主程序，GitHub 构建，32 位老机型 |
| [`termux-api-0.53.0-github.apk`](https://github.com/Healton1314/mifi-autotune/releases/download/termux-apk-2025.10/termux-api-0.53.0-github.apk) | 8.4 MB | 插件，GitHub 构建 |

> 手机上直连 GitHub 打不开或太慢时，在整条链接**前面**加一段加速前缀即可，例如
> `https://ghfast.top/https://github.com/Healton1314/mifi-autotune/releases/download/termux-apk-2025.10/com.termux.api-0.53.0-fdroid.apk`
> （另一个可用前缀：`https://gh-proxy.com/`）。

## 二、关键：两组必须配对，不能混装

Android 靠签名区分来源。**Termux 主程序和 Termux:API 必须同一来源**，混装的表现是「命令没反应 / 一直超时」——`mifi --scan` 扫不到信道、`termux-battery-status` 卡住，都可能是这个原因。

| 手机上的 Termux 来自 | 装哪个 Termux:API |
|---|---|
| F-Droid，或从没装过（**推荐**） | `com.termux.api-0.53.0-fdroid.apk` |
| GitHub 官方 Release | `termux-api-0.53.0-github.apk` |

装错来源时系统会提示「应用未安装 / 签名冲突」，**不会弄坏已有数据**，换另一组再试。

已核对：F-Droid 两个包的签名证书指纹相同（`228fb2cf…d41c42`，`O=fdroid.org`），可以互相配对。

## 三、长期地址（本仓库停更也能用）

| 用途 | 地址 |
|---|---|
| Termux 官方 Releases（永远最新） | <https://github.com/termux/termux-app/releases/latest> |
| Termux:API 官方 Releases | <https://github.com/termux/termux-api/releases/latest> |
| F-Droid 应用页 | <https://f-droid.org/packages/com.termux/> ・ <https://f-droid.org/packages/com.termux.api/> |
| F-Droid 国内镜像（南大，实测最快） | <https://mirrors.nju.edu.cn/fdroid/repo/> |
| GitHub 打不开时，在整条链接前加 | `https://ghfast.top/` 或 `https://gh-proxy.com/` |

F-Droid 直链按 `<镜像>/repo/<包名>_<versionCode>.apk` 拼。0.118.3 的 versionCode 是 1002：

- <https://mirrors.nju.edu.cn/fdroid/repo/com.termux_1002.apk>
- <https://mirrors.nju.edu.cn/fdroid/repo/com.termux.api_1002.apk>

## 四、安装步骤

1. 两个 APK 都下载到手机
2. 允许「安装未知来源应用」（只给浏览器或文件管理器）
3. **先装 Termux** → 打开一次，让它建好目录
4. **再装 Termux:API** → 打开一次 → 给它「定位」权限（安卓 12+ 可能还要「附近的设备」）
5. 回 Termux 装命令行那半边并验证：

```bash
pkg install -y termux-api
termux-battery-status     # 有 JSON 输出 = 通道通了；卡住没反应 = App 没装/没给权限
```

装完 Termux 就可以回到 [README 的手机安装那一步](../README.md#在手机上独立运行不依赖电脑)，把 `mifi` 装上。

## 五、校验

```text
e6265a57eb5ca363808488e3b01955958bed93bc0c8a0d281849b363b11027ec  com.termux-0.118.3-fdroid.apk
4497dbbf81906df52e59ed387a5223d225aa0de3aca817cc557a621e4dadda44  com.termux.api-0.53.0-fdroid.apk
72fdb596045116bf5ba1b5bdf5b26fddb9acc0bd074ad9f2da9eb0ae85e83a4e  termux-app-0.118.3-github-arm64.apk
89416397b70f9ff67a0044e8abe6ef82487cd48fcf543e2d23e02688cc541cf0  termux-app-0.118.3-github-armv7.apk
ecf916ff80ae751e65c092f51c055cce4de417ebeea8e449cd0f294afdbde39a  termux-api-0.53.0-github.apk
```

## 六、来源与许可

- F-Droid 组取自 F-Droid 官方仓库的南大镜像，sha256 与 F-Droid 签名索引登记的 hash 一致。
- GitHub 组取自 termux-app `v0.118.3`、termux-api `v0.53.0` 官方 Release，sha256 与官方 `sha256sums` 一致。
- 均为 **GPL-3.0-only**，源码：<https://github.com/termux/termux-app> ・ <https://github.com/termux/termux-api>。本仓库只搬运，未改一个字节。
