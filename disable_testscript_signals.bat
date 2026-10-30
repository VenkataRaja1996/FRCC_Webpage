@echo off
REM ============================================================
REM  disable_signals.bat  —  Universal Signal Disabler
REM
REM  Works with one or more .robot files or folders.
REM
REM  HOW TO USE:
REM    Option A) Drag-and-drop one/more .robot files or folders onto this bat
REM    Option B) Run from cmd:
REM              disable_testscript_signals.bat "C:\path\to\MyTest.robot" "C:\path\to\MyTest2.robot"
REM              disable_testscript_signals.bat "C:\path\to\robot_scripts\Vcumate\FCC\regressions"
REM    Option C) Double-click — you will be prompted to paste
REM              one robot file/folder path (or run from cmd for multiple)
REM
REM  What it does:
REM    1. Scans all given robot files/folders for vcu_send_signal_update calls
REM    2. Sends value 0 for every found signal via VehicleSimHelper
REM    3. Appends TC_Disable_Signals test case to each robot file
REM ============================================================

setlocal DisableDelayedExpansion

set "BAT_DIR=%~dp0"

REM ---- Resolve robot file path(s) ------------------------------
set "USE_INTERACTIVE=0"
set "ROBOT_FILE="
set "ROBOT_FILES="

if "%~1"=="" goto :interactive_input
set "ROBOT_FILES=%*"
goto :validate_inputs

:interactive_input
set "USE_INTERACTIVE=1"
echo.
echo  No robot file/folder provided.
echo  Please paste the full path to your .robot file or folder below.
echo  Example: d:\project\MyTest.robot
echo  Example: d:\project\robot_scripts\Vcumate\FCC\regressions
echo.
set /p "ROBOT_FILE=Robot file or folder path: "

:validate_inputs
if "%USE_INTERACTIVE%"=="1" if "%ROBOT_FILE%"=="" goto :input_error
if "%USE_INTERACTIVE%"=="0" if "%ROBOT_FILES%"=="" goto :input_error

REM Convert interactive single path into normal argument payload.
if "%USE_INTERACTIVE%"=="1" set "ROBOT_FILES="%ROBOT_FILE%""
goto :show_inputs

:input_error
echo [ERROR] No file path entered. Exiting.
pause
exit /b 1

:show_inputs

echo.
echo ============================================================
echo  Robot file/folder path(s): %ROBOT_FILES%
echo ============================================================
echo.

REM ---- Resolve Python launcher --------------------------------
set "PY_CMD="
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 set "PY_CMD=python"

if not defined PY_CMD (
    where py >nul 2>nul
    if %ERRORLEVEL% EQU 0 set "PY_CMD=py -3"
)

if not defined PY_CMD (
    echo [ERROR] Python was not found.
    echo         Install Python or add it to PATH.
    echo         You can also run with the Python launcher: py -3
    pause
    exit /b 1
)

REM ---- Run Python script --------------------------------------
%PY_CMD% "%BAT_DIR%disable_testscript_signals.py" %ROBOT_FILES%

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Script failed with exit code %ERRORLEVEL%.
    echo         Check that:
    echo           1. Python is on your PATH
    echo           2. GM Vehicle Simulator is running
    echo           3. The .robot file path is correct
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ============================================================
echo  Finished. Signals disabled + test case appended.
echo ============================================================
echo.
pause
endlocal
