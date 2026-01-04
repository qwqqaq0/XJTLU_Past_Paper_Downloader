@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo.
echo ==========================================
echo  XJTLU Past Paper Downloader - Setup ^& Run
echo ==========================================
echo.

REM Always run in the folder where this bat file is located
cd /d "%~dp0"

echo [0/4] 检查 Python 启动器（py）...
where py >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] 未检测到 Python 启动器 ^(py^).
  echo 请先安装 Python（建议 3.10+），并勾选“Add Python to PATH”。
  echo 安装完成后重新双击本文件。
  echo.
  pause
  exit /b 1
)
echo [OK] 已检测到 py
echo.

echo [1/4] 创建虚拟环境 .venv（若已存在会跳过）...
if not exist ".venv" (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo.
    echo [ERROR] 创建虚拟环境失败。
    echo.
    pause
    exit /b 1
  )
  echo [OK] 虚拟环境已创建
) else (
  echo [SKIP] 已存在 .venv，跳过
)
echo.

echo [2/4] 安装 Python 依赖（playwright）...
.\.venv\Scripts\python -m pip install -U pip
if errorlevel 1 (
  echo.
  echo [ERROR] pip 更新失败（可能网络问题）。
  echo.
  pause
  exit /b 1
)

if not exist "requirements.txt" (
  echo [WARN] 未找到 requirements.txt，将直接安装 playwright
  .\.venv\Scripts\python -m pip install playwright
) else (
  .\.venv\Scripts\python -m pip install -r requirements.txt
)
if errorlevel 1 (
  echo.
  echo [ERROR] 依赖安装失败。请检查网络/代理/VPN。
  echo.
  pause
  exit /b 1
)
echo [OK] 依赖已安装
echo.

echo [3/4] 安装 Playwright 浏览器（Chromium）...
echo （首次运行会下载浏览器文件，可能需要几分钟）
.\.venv\Scripts\python -m playwright install chromium
if errorlevel 1 (
  echo.
  echo [ERROR] Chromium 安装失败。可能是网络受限。
  echo 建议：换网络或使用学校 VPN 后重试。
  echo.
  pause
  exit /b 1
)
echo [OK] Chromium 已安装
echo.

echo [4/4] 启动下载器脚本...
echo.

REM >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
REM 这里改成你的脚本文件名
set SCRIPT_NAME=downloader.py
REM 例如： set SCRIPT_NAME=抓期末试卷pdf.py
REM >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

if not exist "%SCRIPT_NAME%" (
  echo [ERROR] 找不到脚本文件：%SCRIPT_NAME%
  echo 请确认脚本文件名，并在 run.bat 里修改 SCRIPT_NAME。
  echo.
  pause
  exit /b 1
)

.\.venv\Scripts\python "%SCRIPT_NAME%"

echo.
echo ==========================================
echo  程序已退出。按任意键关闭窗口。
echo ==========================================
pause >nul
endlocal
