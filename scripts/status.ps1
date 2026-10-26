param([string]$ConfigPath)
. (Join-Path $PSScriptRoot 'common.ps1')
$controlArguments = @('status')
if ($ConfigPath) { $controlArguments += @('--config', $ConfigPath) }
Invoke-WorkbenchControl -ControlArguments $controlArguments
exit $script:WorkbenchExitCode
