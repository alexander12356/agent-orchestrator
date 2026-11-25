@echo off
rem =====================================================================
rem  agent-orchestrator : control the FastAPI server in Ubuntu WSL2
rem
rem    orchestrator.bat start     start the server on port 8000
rem    orchestrator.bat stop      stop it (SIGTERM, SIGKILL after 20s)
rem    orchestrator.bat restart   stop + start
rem    orchestrator.bat status    process and health check
rem    orchestrator.bat log       last 40 lines of uvicorn.log
rem
rem  The server runs detached inside WSL2:
rem    /root/projects/agent-orchestrator   (clone of this repository)
rem  Web UI: http://127.0.0.1:8000
rem  Override the distro:  set ORCHESTRATOR_WSL_DISTRO=Ubuntu-24.04
rem
rem  Notes:
rem  * keep this file CRLF: cmd.exe cannot resolve goto labels in an
rem    LF-only batch file;
rem  * wsl.exe expands "$name" in its own arguments, so this script uses
rem    only "$(command)" substitutions and never shell variables.
rem =====================================================================
setlocal

set "DISTRO=Ubuntu-24.04"
if not "%ORCHESTRATOR_WSL_DISTRO%"=="" set "DISTRO=%ORCHESTRATOR_WSL_DISTRO%"
set "PROJDIR=/root/projects/agent-orchestrator"
set "PORT=8000"
set "HEALTH=http://127.0.0.1:%PORT%/api/health"
set "PATTERN=[u]vicorn app.main:app"
set "WSLX=wsl.exe -d %DISTRO% -u root --"

if "%~1"=="" goto usage
if /i "%~1"=="start" goto cmd_start
if /i "%~1"=="stop" goto cmd_stop
if /i "%~1"=="restart" goto cmd_restart
if /i "%~1"=="status" goto cmd_status
if /i "%~1"=="log" goto cmd_log
if /i "%~1"=="help" goto usage
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
goto usage

rem --------------------------------------------------------------- commands
:cmd_start
call :start_impl
set "RC=%errorlevel%"
call :maybe_pause
exit /b %RC%

:cmd_stop
call :stop_impl
set "RC=%errorlevel%"
call :maybe_pause
exit /b %RC%

:cmd_restart
call :stop_impl
call :sleep1
call :start_impl
set "RC=%errorlevel%"
call :maybe_pause
exit /b %RC%

:cmd_status
call :status_impl
set "RC=%errorlevel%"
call :maybe_pause
exit /b %RC%

:cmd_log
%WSLX% bash -c "tail -n 40 %PROJDIR%/uvicorn.log"
set "RC=%errorlevel%"
call :maybe_pause
exit /b %RC%

rem ------------------------------------------------------------ subroutines
:start_impl
call :is_running
if not errorlevel 1 goto start_already

echo [*] Starting orchestrator in WSL2 (%DISTRO%) ...
%WSLX% bash -c "cd %PROJDIR% && setsid nohup ./.venv/bin/uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port %PORT% >> uvicorn.log 2>&1 < /dev/null & sleep 2"
if errorlevel 1 goto start_failed

call :wait_healthy
if errorlevel 1 goto start_timeout

echo [ok] Orchestrator is running: http://127.0.0.1:%PORT%
call :print_pid
call :check_from_windows
exit /b 0

:start_already
echo [i] Already running.
call :print_pid
exit /b 0

:start_failed
echo [x] Could not launch uvicorn.
echo     Inspect the log:  orchestrator.bat log
exit /b 1

:start_timeout
echo [x] uvicorn was launched but the health check failed within 60s.
echo     Inspect the log:  orchestrator.bat log
exit /b 1

:stop_impl
call :is_running
if errorlevel 1 goto stop_already

echo [*] Stopping orchestrator ...
%WSLX% bash -c "kill -TERM -- -$(pgrep -f '%PATTERN%' | tail -1) 2>/dev/null; pkill -TERM -f '%PATTERN%' 2>/dev/null; true"
call :wait_stopped
if not errorlevel 1 goto stop_ok

echo [w] Graceful stop timed out, sending SIGKILL ...
%WSLX% bash -c "kill -KILL -- -$(pgrep -f '%PATTERN%' | tail -1) 2>/dev/null; pkill -KILL -f '%PATTERN%' 2>/dev/null; true"
call :sleep1
call :is_running
if errorlevel 1 goto stop_ok
echo [x] Stop failed: the process is still alive.
exit /b 1

:stop_already
echo [i] Not running.
exit /b 0

:stop_ok
echo [ok] Stopped.
exit /b 0

:status_impl
call :is_running
if errorlevel 1 goto status_down
echo [i] Process: RUNNING
call :print_pid
goto status_health
:status_down
echo [i] Process: STOPPED
:status_health
echo [i] Health inside WSL:
%WSLX% bash -c "curl -s -m 3 %HEALTH% || echo unreachable; echo"
echo [i] Health from Windows:
where curl.exe >nul 2>&1
if errorlevel 1 goto status_nocurl
curl.exe -s -m 3 --noproxy "*" %HEALTH%
if errorlevel 1 goto status_winfail
echo.
goto status_log
:status_winfail
echo.
echo     unreachable - WSL port forwarding or system proxy in the way
goto status_log
:status_nocurl
echo     curl.exe not found, skipped
:status_log
echo [i] Log: %PROJDIR%/uvicorn.log
exit /b 0

:is_running
%WSLX% bash -c "pgrep -f '%PATTERN%' >/dev/null"
exit /b %errorlevel%

:print_pid
%WSLX% bash -c "ps -o pid=,etime=,args= -p $(pgrep -f '%PATTERN%' | tail -1) 2>/dev/null"
exit /b 0

:sleep1
ping -n 2 127.0.0.1 >nul 2>&1
exit /b 0

:wait_healthy
setlocal
set /a __n=0
:wait_healthy_loop
%WSLX% bash -c "curl -sf -m 3 -o /dev/null %HEALTH%"
if not errorlevel 1 (
  endlocal
  exit /b 0
)
set /a __n+=1
if %__n% GEQ 60 (
  endlocal
  exit /b 1
)
ping -n 2 127.0.0.1 >nul 2>&1
goto wait_healthy_loop

:wait_stopped
setlocal
set /a __n=0
:wait_stopped_loop
call :is_running
if errorlevel 1 (
  endlocal
  exit /b 0
)
set /a __n+=1
if %__n% GEQ 20 (
  endlocal
  exit /b 1
)
ping -n 2 127.0.0.1 >nul 2>&1
goto wait_stopped_loop

:check_from_windows
where curl.exe >nul 2>&1
if errorlevel 1 exit /b 0
curl.exe -s -m 5 --noproxy "*" -o nul %HEALTH% >nul 2>&1
if errorlevel 1 goto check_winfail
echo [ok] Reachable from Windows: %HEALTH%
exit /b 0
:check_winfail
echo [w] Not reachable from Windows on 127.0.0.1:%PORT%.
echo     WSL port forwarding or the system proxy is in the way; use 127.0.0.1, not localhost.
exit /b 0

rem Pause only when the script was launched by a double click (cmd /c "...bat")
:maybe_pause
echo %cmdcmdline% | find /i "%~nx0" >nul
if errorlevel 1 exit /b 0
pause
exit /b 0

:usage
echo.
echo  agent-orchestrator control   (WSL2 distro: %DISTRO%)
echo.
echo    orchestrator.bat start     start the server on port %PORT%
echo    orchestrator.bat stop      stop it (SIGTERM, SIGKILL after 20s)
echo    orchestrator.bat restart   stop + start
echo    orchestrator.bat status    process and health check
echo    orchestrator.bat log       last 40 lines of uvicorn.log
echo.
echo    Web UI  : http://127.0.0.1:%PORT%
echo    WSL path: %PROJDIR%
echo    Log     : %PROJDIR%/uvicorn.log
echo.
pause
exit /b 0
