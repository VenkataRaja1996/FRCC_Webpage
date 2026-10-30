# Robot Framework Test Runner with Continue-On-Failure
# Usage: .\run_tests.ps1 -TestPath <path> [-ExtraArgs <args>]

param(
    [Parameter(Mandatory=$true)]
    [string]$TestPath,
    
    [Parameter(Mandatory=$false)]
    [string[]]$ExtraArgs
)

$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$casewiseReportScript = Join-Path $rootDir "generate_testcase_wise_report.py"
$signalDisableScript = Join-Path $rootDir "disable_testscript_signals.py"
$outputXmlPath = Join-Path $rootDir "results\output.xml"

Write-Host "Running tests with Continue-On-Failure enabled..." -ForegroundColor Green
Write-Host ""

Write-Host "Disabling test-script signals before starting Robot..." -ForegroundColor Yellow
& python $signalDisableScript $TestPath
if ($LASTEXITCODE -ne 0) {
    Write-Error "Signal disable step failed with exit code $LASTEXITCODE. Robot will not be started."
    exit $LASTEXITCODE
}

# Build the robot command
$robotArgs = @(
    "--pythonpath", $rootDir,
    "--listener", "listener.ContinueOnFailureListener",
    "--prerunmodifier", "robot_startup_modifier.RobotStartupModifier",
    "--outputdir", "results",
    "--log", "results/log.html",
    "--report", "results/report.html"
)

# Add extra arguments if provided
if ($ExtraArgs) {
    $robotArgs += $ExtraArgs
}

# Add test path
$robotArgs += $TestPath

# Run robot
& robot $robotArgs

if ((Test-Path $casewiseReportScript) -and $TestPath.ToLower().EndsWith(".robot")) {
    Write-Host ""
    Write-Host "Generating testcase-wise report..." -ForegroundColor Yellow
    & python $casewiseReportScript $TestPath $outputXmlPath
}

Write-Host ""
Write-Host "Test execution completed. Results saved to results\ folder" -ForegroundColor Cyan
