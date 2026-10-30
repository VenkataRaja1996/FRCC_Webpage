@echo off
setlocal enabledelayedexpansion

REM Unified runner:
REM 1) No args: runs default regression suite list.
REM 2) With args: first arg is test path/suite, remaining are extra robot args.

set "ROOT_DIR=%~dp0"
set "ROOT_DIR=%ROOT_DIR:~0,-1%"
set "REPO_DIR=%ROOT_DIR%\gmtools_rtostestautomation"
if not exist "%REPO_DIR%" set "REPO_DIR=%ROOT_DIR%"
set "SUITE_DIR=%REPO_DIR%\RTOSAutomation\src\rtos\robot_scripts\Vcumate"
set "OUTPUT_DIR=%ROOT_DIR%\results"
set "LISTENER=listener.ContinueOnFailureListener"
set "PRERUN_MODIFIER=continue_on_failure_modifier.ContinueOnFailureModifier"
set "CASEWISE_REPORT_SCRIPT=%ROOT_DIR%\generate_testcase_wise_report.py"

if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

if "%~1"=="" goto RUN_DEFAULT_REGRESSIONS

set "TEST_PATH=%~1"
shift
set "EXTRA_ARGS=%*"

echo Running target with Continue-On-Failure enabled...
echo Target: %TEST_PATH%
echo.

python "%ROOT_DIR%\run_robot_protected.py" ^
    --root-dir "%ROOT_DIR%" ^
    --output-dir "%OUTPUT_DIR%" ^
    --single-robot "%TEST_PATH%" ^
    --report-script "%CASEWISE_REPORT_SCRIPT%" ^
    --robot-args ^
    --listener "%LISTENER%" ^
    --prerunmodifier "%PRERUN_MODIFIER%" ^
    --outputdir "%OUTPUT_DIR%" ^
    --output output.xml ^
    --log log.html ^
    --report report.html ^
    %EXTRA_ARGS% ^
    "%TEST_PATH%"

goto DONE

:RUN_DEFAULT_REGRESSIONS
REM Build suite list filtered by vehicle_program in rtos_configuration.py
python "%ROOT_DIR%\get_suites_for_vehicle.py" "%SUITE_DIR%" > "%TEMP%\robot_suites.txt" 2>"%TEMP%\robot_suites_err.txt"
if %ERRORLEVEL% neq 0 (
    echo ERROR: Failed to resolve suites for vehicle program.
    type "%TEMP%\robot_suites_err.txt"
    exit /b 1
)
type "%TEMP%\robot_suites_err.txt"

REM Read vehicle_program for display purposes
for /f "tokens=2 delims='" %%V in ('findstr /r "^vehicle_program" "%REPO_DIR%\RTOSAutomation\src\rtos\rtos_configuration.py"') do set "VP=%%V"

echo Running suites for vehicle program: %VP%
echo Suite list:
type "%TEMP%\robot_suites.txt"
echo.

REM Build robot args from the file list
set "SUITE_ARGS="
for /f "usebackq delims=" %%S in ("%TEMP%\robot_suites.txt") do (
    set "SUITE_ARGS=!SUITE_ARGS! "%%S""
)

python "%ROOT_DIR%\run_robot_protected.py" ^
    --root-dir "%ROOT_DIR%" ^
    --output-dir "%OUTPUT_DIR%" ^
    --suite-list-txt "%TEMP%\robot_suites.txt" ^
    --report-script "%CASEWISE_REPORT_SCRIPT%" ^
    --robot-args ^
    --listener "%LISTENER%" ^
    --prerunmodifier "%PRERUN_MODIFIER%" ^
    --outputdir "%OUTPUT_DIR%" ^
    --output output.xml ^
    --log log.html ^
    --report report.html ^
    %SUITE_ARGS%

:DONE
echo.
echo Done. Results saved to %OUTPUT_DIR%
pause
