@echo off
chcp 65001 >nul
title mifi-autotune 面板
cd /d "%~dp0"

echo ============================================================
echo   随身WiFi 调优面板
echo ============================================================
echo.

REM 如果 8787 已被占用（上次的面板还在跑），先提示
netstat -ano | findstr ":8787 .*LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [!] 8787 端口已被占用，说明面板可能已经在跑了。
    echo     直接用手机打开： http://192.168.0.100:8787/?k=mifi2026
    echo.
    pause
    exit /b 0
)

echo 正在启动面板，保持这个窗口开着（关掉窗口 = 停止面板）...
echo.
echo   电脑访问： http://127.0.0.1:8787/?k=mifi2026
echo   手机访问： http://192.168.0.100:8787/?k=mifi2026
echo             （手机要连「月亮糖」这个 WiFi）
echo.

REM 第一次运行时，Windows 防火墙可能弹窗询问，点「允许访问」即可
python server.py --lan --port 8787 --token mifi2026

echo.
echo 面板已停止。
pause
