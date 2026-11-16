[CmdletBinding()]
param(
    [string]$Python = '',
    [switch]$SkipTests,
    [switch]$Smoke,
    [switch]$InstallDependencies,
    [switch]$SourceOnly,
    [switch]$Plan,
    [switch]$Deploy,
    [string]$DeployHost = $env:ASTRATERM_DEPLOY_HOST,
    [string]$DeployUser = $env:ASTRATERM_DEPLOY_USER,
    [string]$DeployRoot = $env:ASTRATERM_DEPLOY_ROOT,
    [string]$SigningKey = '',
    [string]$ReleaseNotes = '',
    [string]$Version = ''
)
$ErrorActionPreference = 'Stop'
if (-not $Python) {
    $buildRuntime = Join-Path $PSScriptRoot '.venv/build/Scripts/python.exe'
    $Python = if (Test-Path -LiteralPath $buildRuntime) { $buildRuntime } else { 'python' }
}
$buildArgs = @((Join-Path $PSScriptRoot 'tools/build.py'), 'windows')
if ($PSBoundParameters.ContainsKey('Version')) { $buildArgs += @('--version', $Version) }
if ($SkipTests) { $buildArgs += '--skip-tests' }
if ($Smoke) { $buildArgs += '--smoke' }
if ($InstallDependencies) { $buildArgs += '--install-dependencies' }
if ($SourceOnly) { $buildArgs += '--source-only' }
if ($Plan) { $buildArgs += '--plan' }
if ($Deploy) { $buildArgs += '--deploy' }
if ($DeployHost) { $buildArgs += @('--deploy-host', $DeployHost) }
if ($DeployUser) { $buildArgs += @('--deploy-user', $DeployUser) }
if ($DeployRoot) { $buildArgs += @('--deploy-root', $DeployRoot) }
if ($SigningKey) { $buildArgs += @('--signing-key', $SigningKey) }
if ($ReleaseNotes) { $buildArgs += @('--release-notes', $ReleaseNotes) }
& $Python @buildArgs
exit $LASTEXITCODE
