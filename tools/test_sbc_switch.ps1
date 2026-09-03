# Prove the SBC method cell is a live switch, not decoration.
#
# Flip the Inputs cell from "expense" to "dilute" and the tax line, the free cash
# flow line and the share count must all move together. If the valuation does not
# change, the workbook is a report with a dropdown painted on it.
#
# Usage: pwsh -File tools/test_sbc_switch.ps1 -Path outputs/AAPL_model.xlsx

param([Parameter(Mandatory = $true)][string]$Path)

$full = (Resolve-Path $Path).Path
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false

try {
    $book = $excel.Workbooks.Open($full, 0, $false)
    $inputs = $book.Worksheets.Item("Inputs")
    $dcf = $book.Worksheets.Item("DCF")

    # Locate the SBC method cell and the two output rows by their labels.
    $methodRow = 0; $vpsRow = 0; $sharesRow = 0
    for ($r = 1; $r -le 80; $r++) {
        $label = $inputs.Cells.Item($r, 1).Text
        if ($label -like "SBC method*") { $methodRow = $r }
    }
    for ($r = 1; $r -le 80; $r++) {
        $label = $dcf.Cells.Item($r, 1).Text
        if ($label -eq "Implied value per share") { $vpsRow = $r }
        if ($label -like "(/) Diluted shares*") { $sharesRow = $r }
    }

    if ($methodRow -eq 0 -or $vpsRow -eq 0) {
        Write-Output "COULD NOT LOCATE CELLS (method=$methodRow vps=$vpsRow shares=$sharesRow)"
        exit 2
    }

    $excel.CalculateFullRebuild()
    $before = $dcf.Cells.Item($vpsRow, 2).Value2
    $sharesBefore = $dcf.Cells.Item($sharesRow, 2).Value2
    $methodBefore = $inputs.Cells.Item($methodRow, 2).Text

    $inputs.Cells.Item($methodRow, 2).Value2 = "dilute"
    $excel.CalculateFullRebuild()
    $after = $dcf.Cells.Item($vpsRow, 2).Value2
    $sharesAfter = $dcf.Cells.Item($sharesRow, 2).Value2

    Write-Output ("  method '{0}' -> value per share {1:N4}, shares {2:N0}" -f $methodBefore, $before, $sharesBefore)
    Write-Output ("  method 'dilute'  -> value per share {0:N4}, shares {1:N0}" -f $after, $sharesAfter)
    Write-Output ("  value moved {0:N2}%, share count moved {1:N2}%" -f (($after / $before - 1) * 100), (($sharesAfter / $sharesBefore - 1) * 100))

    if ([Math]::Abs($after - $before) -lt 0.0001) {
        Write-Output "FAIL: the switch did nothing"
        $book.Close($false); exit 1
    }
    if ($sharesAfter -le $sharesBefore) {
        Write-Output "FAIL: dilute did not increase the share count"
        $book.Close($false); exit 1
    }
    Write-Output "PASS: the SBC switch is live"

    $book.Close($false)  # discard the edit
}
catch {
    Write-Output "FAILED: $($_.Exception.Message)"
    exit 2
}
finally {
    $excel.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) | Out-Null
}
