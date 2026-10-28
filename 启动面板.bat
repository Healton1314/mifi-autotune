@echo off
chcp 65001 >nul
title mifi-autotune 面板
cd /d "%~dp0"

echo ============================================================
echo   随身WiFi 调优面板
echo ============================================================
echo.

netstat -ano | findstr ":8787" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [!] 8787 端口已被占用，说明面板已经在跑了。
    echo     直接打开： http://127.0.0.1:8787/?k=mifi2026
    echo     手机：连同一个 WiFi 后，把上面的 127.0.0.1 换成电脑的局域网 IP。
    echo.
    pause
    exit /b 0
)

echo 正在启动面板。保持这个窗口开着（关掉窗口 = 停止面板）。
echo 第一次运行时 Windows 防火墙会询问，请点「允许访问」。
echo.
echo   下面的「电脑/手机」地址由面板自己打印，以它为准。
echo.

python server.py --lan --port 8787 --token mifi2026

echo.
echo 面板已停止。
pause
