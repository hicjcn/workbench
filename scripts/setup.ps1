param(
    [string]$ConfigPath,
    [switch]$SkipImageBuild,
    [switch]$RebuildImage
)
. (Join-Path $PSScriptRoot 'common.ps1')
$controlArguments = @('setup')
if ($ConfigPath) { $controlArguments += @('--config', $ConfigPath) }
if ($SkipImageBuild) { $controlArguments += '--skip-image-build' }
if ($RebuildImage) { $controlArguments += '--rebuild-image' }
Invoke-WorkbenchControl -ControlArguments $controlArguments
exit $script:WorkbenchExitCode
