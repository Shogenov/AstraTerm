param(
    [Parameter(Mandatory=$true)][string]$Helper,
    [Parameter(Mandatory=$true)][string]$Config
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$powershell = Join-Path $PSHOME 'powershell.exe'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Config "{1}"' -f $Helper, $Config
$child = Start-Process -FilePath $powershell -ArgumentList $arguments -WindowStyle Hidden -PassThru
$ready = $Config + '.ready'
$deadline = [DateTime]::UtcNow.AddSeconds(25)
while ([DateTime]::UtcNow -lt $deadline) {
    if (Test-Path -LiteralPath $ready) { exit 0 }
    if ($child.HasExited) { throw ('Update helper exited before becoming ready. Code ' + $child.ExitCode) }
    Start-Sleep -Milliseconds 100
}
throw 'Update helper did not become ready'
