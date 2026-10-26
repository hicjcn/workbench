$ErrorActionPreference = 'Stop'
$script:WorkbenchRoot = Split-Path $PSScriptRoot -Parent
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$OutputEncoding = [Console]::OutputEncoding

function Invoke-WorkbenchControl {
    param([string[]]$ControlArguments)
    $scriptPath = Join-Path $script:WorkbenchRoot 'scripts\control.py'
    $configIndex = [Array]::IndexOf($ControlArguments, '--config')
    if ($configIndex -ge 0 -and $ControlArguments[$configIndex + 1] -ne '-') {
        $ControlArguments[$configIndex + 1] = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ControlArguments[$configIndex + 1])
    }
    Push-Location $script:WorkbenchRoot
    try {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 $scriptPath @ControlArguments
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python $scriptPath @ControlArguments
        } else {
            throw 'Install Python 3.11+ (recommended: 3.12) and enable its launcher or PATH entry.'
        }
        $script:WorkbenchExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
}
