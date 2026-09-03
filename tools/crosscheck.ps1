# Compare Excel's computed answers against the Python engine's, case by case.
#
# Run tools/build_crosscheck.py first: it writes the workbooks and an expected.json
# holding what Python computed. This script opens each workbook, forces a full
# recalculation, and checks Excel agrees.
#
# It compares the equity bridge (value per share, equity value, share count), the
# terminal value, and every cell of both sensitivity grids. The grids matter most:
# they are the most formula-dense part of the workbook, and until the second audit
# nothing compared them -- the README claimed they were checked on the strength of a
# manual pass that was never committed to the tooling. The terminal value was worse
# than unchecked: build_crosscheck.py wrote the expected figure and this script never
# read it, so the number sat in expected.json as dead data.
#
# Requires Excel, so it is a local acceptance gate rather than a CI step. The pytest
# suite covers the same ground with openpyxl, minus the actual evaluation.
#
# Usage: pwsh -File tools/crosscheck.ps1

param(
    [string]$Expected = "outputs/crosscheck/expected.json",
    [double]$Tolerance = 0.001,      # 0.1%, matching the solver's own convergence tolerance
    [double]$GridTolerance = 0.001
)

$cases = Get-Content (Resolve-Path $Expected) -Raw | ConvertFrom-Json
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

$failures = 0
$checks = 0

function Get-LabelledValue($sheet, $label) {
    for ($r = 1; $r -le 90; $r++) {
        if ($sheet.Cells.Item($r, 1).Text -eq $label) { return $sheet.Cells.Item($r, 2).Value2 }
    }
    return $null
}

# Find the row carrying a grid's column headers, by its corner label in column A.
function Get-GridHeaderRow($sheet, $corner) {
    for ($r = 1; $r -le 120; $r++) {
        if ($sheet.Cells.Item($r, 1).Text -eq $corner) { return $r }
    }
    return $null
}

function Compare-Value($name, $excelValue, $pythonValue, $tolerance) {
    # Messages go to the HOST, never the output stream.
    #
    # PowerShell returns everything a function writes, not just its final expression.
    # With Write-Output here, a mismatch returned @("...message...", $false) -- an
    # array, which is truthy -- so `if (-not (Compare-Value ...))` never fired and the
    # script reported PASS while printing an 80% disagreement two lines above. The
    # cross-check could not fail. Write-Host keeps the return value a bare boolean.
    if ($null -eq $excelValue) {
        Write-Host ("    {0}: not found in the workbook - the comparison did not run" -f $name) -ForegroundColor Red
        return $false
    }
    if ($pythonValue -eq 0) {
        # Never skip silently: a zero expected value still has to be matched.
        if ([Math]::Abs($excelValue) -gt 1e-6) {
            Write-Host ("    {0} mismatch: python 0  excel {1:N4}" -f $name, $excelValue) -ForegroundColor Red
            return $false
        }
        return $true
    }
    $d = [Math]::Abs($excelValue / $pythonValue - 1)
    if ($d -gt $tolerance) {
        Write-Host ("    {0} mismatch: python {1:N4}  excel {2:N4}  ({3:P4})" -f $name, $pythonValue, $excelValue, $d) -ForegroundColor Red
        return $false
    }
    return $true
}

try {
    Write-Output ("{0,-6} {1,-9} {2,-14} {3,-14} {4,-9} {5,-7} {6}" -f "TICKER", "METHOD", "PYTHON VPS", "EXCEL VPS", "DIFF", "CELLS", "RESULT")
    Write-Output ("-" * 86)

    foreach ($case in $cases) {
        $path = (Resolve-Path $case.file).Path
        $book = $excel.Workbooks.Open($path, 0, $false)
        $excel.CalculateFullRebuild()
        $dcf = $book.Worksheets.Item("DCF")
        $sens = $book.Worksheets.Item("Sensitivity")

        $ok = $true
        $caseChecks = 0

        # ---- equity bridge and terminal value ------------------------------
        $vps = Get-LabelledValue $dcf "Implied value per share"
        $diff = if ($case.value_per_share -ne 0) { [Math]::Abs($vps / $case.value_per_share - 1) } else { 0 }

        foreach ($pair in @(
                @{ name = "value per share"; excel = $vps; py = $case.value_per_share },
                @{ name = "equity"; excel = (Get-LabelledValue $dcf "Equity value"); py = $case.equity_value },
                @{ name = "shares"; excel = (Get-LabelledValue $dcf "(/) Diluted shares"); py = $case.shares },
                @{ name = "terminal value"; excel = (Get-LabelledValue $dcf "Terminal value used"); py = $case.terminal_value }
            )) {
            $caseChecks++
            if (-not (Compare-Value $pair.name $pair.excel $pair.py $Tolerance)) { $ok = $false }
        }

        # ---- both sensitivity grids, cell by cell --------------------------
        $grids = @(
            @{ corner = "WACC \ growth"; expected = $case.sensitivity.growth_grid },
            @{ corner = "WACC \ multiple"; expected = $case.sensitivity.multiple_grid }
        )

        foreach ($grid in $grids) {
            $headerRow = Get-GridHeaderRow $sens $grid.corner
            if ($null -eq $headerRow) {
                Write-Output ("    grid '{0}' not found - the comparison did not run" -f $grid.corner)
                $ok = $false
                continue
            }
            for ($i = 0; $i -lt $grid.expected.Count; $i++) {
                $row = $headerRow + 1 + $i
                for ($j = 0; $j -lt $grid.expected[$i].Count; $j++) {
                    $py = $grid.expected[$i][$j]
                    $cell = $sens.Cells.Item($row, 2 + $j)
                    $caseChecks++
                    if ($null -eq $py) {
                        # Python declined this cell (WACC <= growth); Excel must agree.
                        if ($cell.Text -ne "n/a") {
                            Write-Output ("    {0} R{1}C{2}: python n/a, excel '{3}'" -f $grid.corner, $row, (2 + $j), $cell.Text)
                            $ok = $false
                        }
                        continue
                    }
                    $name = "{0} R{1}C{2}" -f $grid.corner, $row, (2 + $j)
                    if (-not (Compare-Value $name $cell.Value2 $py $GridTolerance)) { $ok = $false }
                }
            }
        }

        $checks += $caseChecks
        $verdict = if ($ok) { "PASS" } else { "FAIL" }
        if (-not $ok) { $failures++ }
        Write-Output ("{0,-6} {1,-9} {2,-14:N4} {3,-14:N4} {4,-9:P3} {5,-7} {6}" -f $case.ticker, $case.method, $case.value_per_share, $vps, $diff, $caseChecks, $verdict)

        $book.Close($false)
    }
}
catch {
    Write-Output "ERROR: $($_.Exception.Message)"
    $failures++
}
finally {
    $excel.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) | Out-Null
}

Write-Output ""
if ($failures -eq 0) {
    Write-Output "CROSS-CHECK PASSED: $checks comparisons, Excel agrees with Python on every case."
    exit 0
}
Write-Output "CROSS-CHECK FAILED: $failures case(s) disagree."
exit 1
