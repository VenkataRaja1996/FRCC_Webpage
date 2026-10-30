$path = 'd:\16_06_2026\gmtools_rtostestautomation\RTOSAutomation\src\rtos\robot_scripts\Vcumate\FCC\sanity\Sanity.robot'
$lines = [System.IO.File]::ReadAllLines($path)
$updated = 0
$fixedMalformed = 0

for ($i = 0; $i -lt $lines.Length; $i++) {
    $line = $lines[$i]

    if ($line -match '^\s*Qnx Screenshot\s+screenshot_name=([^\s]+)\s*$') {
        $name = $matches[1]
        $indent = $null
        for ($j = $i + 1; $j -le [Math]::Min($i + 5, $lines.Length - 1); $j++) {
            if ($lines[$j] -match '^(\s*)(?:compare_screenshot|Compare Screenshot)\s+') {
                $indent = $matches[1]
                break
            }
        }
        if (-not $indent) { $indent = '    ' }
        $lines[$i] = "${indent}Take Qnx Screenshot    screenshot_name=$name"
        $fixedMalformed++
        continue
    }

    if ($line -match '^(\s*)Take Qnx Screenshot$') {
        $indent = $matches[1]
        $base = $null

        for ($j = $i + 1; $j -le [Math]::Min($i + 5, $lines.Length - 1); $j++) {
            if ($lines[$j] -match '^\s*(?:compare_screenshot|Compare Screenshot)\s+([^\s]+\.png)\b') {
                $png = $matches[1]
                $fname = [System.IO.Path]::GetFileName($png)
                $base = [System.IO.Path]::GetFileNameWithoutExtension($fname)
                break
            }
        }

        if ($base) {
            $lines[$i] = "${indent}Take Qnx Screenshot    screenshot_name=$base"
            $updated++
        }
    }
}

[System.IO.File]::WriteAllLines($path, $lines)

$remaining = @()
for ($k = 0; $k -lt $lines.Length; $k++) {
    if ($lines[$k] -match '^(\s*)Take Qnx Screenshot$') {
        $remaining += ($k + 1)
    }
}

"UPDATED=$updated"
"FIXED_MALFORMED=$fixedMalformed"
"REMAINING_COUNT=$($remaining.Count)"
"REMAINING_LINES=$([string]::Join(',', $remaining))"
