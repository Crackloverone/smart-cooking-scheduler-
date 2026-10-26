@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Smart Cooking Scheduler Agent

rem ===========================================================================
rem  Smart Cooking Scheduler Agent  --  one-click launcher
rem
rem  Usage, options are combinable:
rem    start.bat                full: check env, install deps if needed,
rem                             offline self-test, start server, open browser
rem    start.bat --fast         skip dependency check and self-test
rem    start.bat --offline      force FT_LLM=off, fully offline, no network
rem    start.bat --no-browser   do not auto-open the browser
rem    start.bat --port 8138    use another port, default is 8137
rem    start.bat --host 0.0.0.0 listen on all interfaces, for LAN / phone
rem                             access. DEFAULT is 127.0.0.1 (local only) --
rem                             binding 0.0.0.0 exposes the app to everyone
rem                             on the network, and there is NO auth.
rem
rem ---------------------------------------------------------------------------
rem  RULES FOR EDITING THIS FILE  --  please read before changing anything
rem ---------------------------------------------------------------------------
rem  1. Keep this file pure ASCII, and keep CRLF line endings.
rem
rem  2. CRLF is mandatory. With LF-only endings cmd.exe mis-parses even the
rem     chcp line and prints:   'p65001' is not recognized ...
rem
rem  3. Do NOT put round brackets in any echo or rem text. Inside an
rem     "if ... then block" syntax, cmd treats a stray closing round bracket as
rem     the end of the block and silently swallows or corrupts the surrounding
rem     lines. Use square brackets instead. This file therefore uses goto-style
rem     flow throughout instead of bracketed blocks.
rem
rem  4. Do NOT put non-ASCII text here. cmd.exe reads a .bat in small chunks
rem     using whatever codepage is active at the moment of the read. Because
rem     "chcp 65001" only takes effect part-way through the file, multibyte
rem     characters near the top are decoded with the old codepage, cmd loses
rem     byte alignment, and fragments of a line get run as commands, e.g.
rem         '...' is not recognized as an internal or external command
rem     It is erratic: it depends on each line's byte offset, so the problem
rem     can appear or vanish when an unrelated line is added or removed.
rem     A UTF-8 BOM, a few padding lines, or an extra goto all "fixed" it in
rem     one test and broke it in another. Only ASCII is actually reliable.
rem
rem  5. Chinese output is still fine, it just has to come from Python, which is
rem     UTF-8 clean here. chcp 65001 is kept so Python's UTF-8 logs and the
rem     app's Chinese banner render correctly in this console.
rem ===========================================================================

cd /d "%~dp0"

set "MODE=full"
set "OFFLINE=0"
set "OPENBROWSER=1"
set "PORT=8137"
rem Default to loopback. Use --host 0.0.0.0 only when you really need LAN access.
set "HOST=127.0.0.1"
set "PY=.venv\Scripts\python.exe"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--fast"       set "MODE=fast"
if /i "%~1"=="--offline"    set "OFFLINE=1"
if /i "%~1"=="--no-browser" set "OPENBROWSER=0"
if /i "%~1"=="--port"       set "PORT=%~2"
if /i "%~1"=="--host"       set "HOST=%~2"
shift
goto parse_args
:args_done

echo ============================================================
echo    Smart Cooking Scheduler Agent  -  launcher
echo ============================================================
echo.

rem ---------- 0. virtual environment ----------
if exist "%PY%" goto venv_ok
echo [0/4] no .venv found, creating one ...
py -3 -m venv .venv 2>nul
if exist "%PY%" goto venv_created
python -m venv .venv 2>nul
if not exist "%PY%" goto fail_venv
:venv_created
echo       virtual environment created.
goto deps
:venv_ok
echo [0/4] .venv found, skipping creation.

:deps
rem ---------- 1. dependencies ----------
if /i "%MODE%"=="fast" goto deps_skipped
"%PY%" -c "import fastapi,uvicorn,pydantic,ortools" >nul 2>nul
if not errorlevel 1 goto deps_ready
echo [1/4] dependencies missing, installing ...
"%PY%" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail_deps
echo       dependencies installed.
goto selftest
:deps_ready
echo [1/4] dependencies already satisfied, skipping install.
goto selftest
:deps_skipped
echo [1/4] fast mode: skip dependency check.

:selftest
rem ---------- 2. offline self-test ----------
if /i "%MODE%"=="fast" goto selftest_skipped
echo [2/4] running offline self-test - 40 core cases ...
"%PY%" tests\test_core.py
if errorlevel 1 goto selftest_warn
echo       all self-tests passed.
goto model_sw
:selftest_warn
echo.
echo       [WARN] some self-tests failed. The server will still start;
echo              please review the output above.
echo.
goto model_sw
:selftest_skipped
echo [2/4] fast mode: skip self-test.

:model_sw
rem ---------- 3. model switches ----------
if not "%OFFLINE%"=="1" goto online_mode
set "FT_LLM=off"
set "FT_AGENT_LLM=off"
set "FT_NOTIFY_LLM=off"
set "FT_WARM_LLM=off"
echo [3/4] offline mode: FT_LLM=off - rules plus local cache, no network
goto port_check
:online_mode
echo [3/4] online mode: model service defaults to LOCAL OLLAMA.
echo       First run needs the model pulled once:/r/necho           ollama pull qwen2.5:7b
echo       If Ollama is not running the agent silently falls back to
echo       rules - the schedule is still fully usable. Use --offline
echo       to skip the model probe entirely.

:port_check
rem force UTF-8 so Python's Chinese log lines render in this console
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ---------- 4. port availability ----------
set "HOLDER="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /i "LISTENING" ^| findstr /c:":%PORT%"') do set "HOLDER=%%p"
if not defined HOLDER goto port_free
echo.
echo       [WARN] port %PORT% is already held by PID !HOLDER!
echo              probably a server that was not shut down cleanly
echo.
choice /c YN /n /m "      kill it and continue? [Y/N] "
if errorlevel 2 goto abort_port
taskkill /F /PID !HOLDER! >nul 2>nul
ping -n 2 127.0.0.1 >nul
:port_free

rem ---------- 5. start server ----------
echo.
echo ============================================================
echo    server      :  http://127.0.0.1:%PORT%
echo    web UI      :  http://127.0.0.1:%PORT%/app
echo    score API   :  POST http://127.0.0.1:%PORT%/api/plan
echo    API docs    :  http://127.0.0.1:%PORT%/docs
echo    press Ctrl+C to stop
echo ============================================================
echo.

if not "%OPENBROWSER%"=="1" goto no_browser
start "" /b cmd /c "ping -n 4 127.0.0.1 >nul & start http://127.0.0.1:%PORT%/app"
:no_browser

if not "%HOST%"=="127.0.0.1" (
  echo [WARN] listening on %HOST% -- the app is reachable from the network,
  echo        and it has NO authentication. Use 127.0.0.1 unless you mean it.
  echo.
)

"%PY%" -m uvicorn app.api.main:app --host %HOST% --port %PORT%
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" goto stopped_ok
echo [ERROR] server exited abnormally, exit code %RC%
echo         usual causes: port in use, missing dependency,
echo         or the app.api.main import failing.
goto the_end
:stopped_ok
echo server stopped normally.

:the_end
echo.
pause
exit /b %RC%

:fail_venv
echo.
echo [ERROR] failed to create the virtual environment.
echo         Make sure Python 3.11+ is installed and on PATH, then retry.
echo         Manual command: python -m venv .venv
echo.
pause
exit /b 1

:fail_deps
echo.
echo [ERROR] dependency installation failed. Check your network and retry:
echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:abort_port
echo.
echo aborted: port %PORT% is still in use.
echo try another port:  start.bat --port 8138
echo.
pause
exit /b 1
