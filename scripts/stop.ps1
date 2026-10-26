param([string]$ConfigPath)
. (Join-Path $PSScriptRoot 'common.ps1')
$controlArguments = @('stop')
if ($ConfigPath) { $controlArguments += @('--config', $ConfigPath) }
Invoke-WorkbenchControl -ControlArguments $controlArguments
exit $script:WorkbenchExitCode
