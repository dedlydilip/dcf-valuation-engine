# Open a generated workbook in Excel, force a full recalculation, and report both
# any formula errors and the headline results.
#
# openpyxl writes formulas but never evaluates them, so a workbook can look perfect
# in Python and still open with #NAME? in every cell -- or trigger Excel's repair
# dialog and lose content silently. This is the only check that proves otherwise.
#
# Usage: pwsh -File tools/verify_excel.ps1 -Path outputs/AAPL_model.xlsx

param([Parameter(Mandatory = $true)][string]$Path)

$full = (Resolve-Path $Path).Path
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
$excel.AskToUpdateLinks = $false

$exitCode = 0
try {
    $book = $excel.Workbooks.Open($full, 0, $false)
    $excel.CalculateFullRebuild()

    $errors = New-Object System.Collections.ArrayList
    $narrow = New-Object System.Collections.ArrayList
    $labelled = @{}

    foreach ($sheet in $book.Worksheets) {
        $used = $sheet.UsedRange
        if ($null -eq $used) { continue }
        $rows = $used.Rows.Count
        $cols = $used.Columns.Count
        if ($rows -gt 400 -or $cols -gt 40) { $rows = [Math]::Min($rows, 400); $cols = [Math]::Min($cols, 40) }

        for ($r = 1; $r -le $rows; $r++) {
            for ($c = 1; $c -le $cols; $c++) {
                $cell = $used.Cells.Item($r, $c)
                $text = $cell.Text
                # "###" only means the column is too narrow, not that anything is wrong.
                # Match the actual Excel error values instead of everything starting with #.
                if ($text -is [string] -and $text -match '^#(REF!|VALUE!|NAME\?|DIV/0!|N/A|NULL!|NUM!|GETTING_DATA|SPILL!|CALC!|FIELD!|BLOCKED!|CONNECT!|UNKNOWN!|PYTHON!)') {
                    [void]$errors.Add("$($sheet.Name)!$($cell.Address($false,$false)) = $text  <-  $($cell.Formula)")
                }
                elseif ($text -is [string] -and $text -match '^#+$') {
                    [void]$narrow.Add("$($sheet.Name)!$($cell.Address($false,$false))")
                }
            }
        }

        # Capture labelled scalars from column A / column B pairs.
        for ($r = 1; $r -le $rows; $r++) {
            $label = $sheet.Cells.Item($r, 1).Text
            if ($label -and $label -is [string] -and $label.Length -gt 3) {
                $val = $sheet.Cells.Item($r, 2).Text
                if ($val) { $labelled["$($sheet.Name)|$label"] = $val }
            }
        }
    }

    Write-Output "=== SHEETS ==="
    foreach ($sheet in $book.Worksheets) { Write-Output ("  " + $sheet.Name) }

    Write-Output ""
    Write-Output "=== HEADLINE VALUES (Excel-computed) ==="
    $wanted = @(
        'Summary|Implied value per share',
        'Summary|Current share price',
        'Summary|Upside / (downside)',
        'Summary|Enterprise value',
        'Summary|Equity value',
        'Summary|Diluted shares',
        'Summary|WACC',
        'Summary|Terminal value as % of EV',
        'WACC|Cost of equity',
        'WACC|WACC',
        'DCF|Equity value',
        'DCF|Implied value per share',
        'DCF|Terminal value used',
        'DCF|Implied exit multiple from perpetuity method',
        'DCF|Implied perpetuity growth from exit multiple',
        'DCF|(/) Diluted shares'
    )
    foreach ($key in $wanted) {
        if ($labelled.ContainsKey($key)) { Write-Output ("  {0,-52} {1}" -f $key, $labelled[$key]) }
    }

    Write-Output ""
    if ($errors.Count -eq 0) {
        Write-Output "=== FORMULA ERRORS: none ==="
        if ($narrow.Count -gt 0) {
            Write-Output "    (note: $($narrow.Count) cell(s) render as ### - column too narrow, not an error)"
        }
    }
    else {
        Write-Output "=== FORMULA ERRORS: $($errors.Count) ==="
        $errors | Select-Object -First 40 | ForEach-Object { Write-Output ("  " + $_) }
        $exitCode = 1
    }

    $book.Close($false)
}
catch {
    Write-Output "EXCEL FAILED TO OPEN OR CALCULATE: $($_.Exception.Message)"
    $exitCode = 2
}
finally {
    $excel.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) | Out-Null
}
exit $exitCode
