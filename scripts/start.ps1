param(
    [string]$ConfigPath,
    [switch]$NoBrowser
)
. (Join-Path $PSScriptRoot 'common.ps1')
$controlArguments = @('start')
if ($ConfigPath) { $controlArguments += @('--config', $ConfigPath) }
if ($NoBrowser) { $controlArguments += '--no-browser' }
Invoke-WorkbenchControl -ControlArguments $controlArguments
exit $script:WorkbenchExitCode
