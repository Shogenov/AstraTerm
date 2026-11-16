param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$ready = $Config + '.ready'
$commit = $Config + '.commit'
$staged = $null
function Get-UpdateHash([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}
try {
    $update = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    $source = [IO.Path]::GetFullPath([string]$update.source)
    if ([IO.Path]::GetExtension($source) -ne '.exe') { throw 'Invalid update source' }
    if ((Get-Item -LiteralPath $source).Length -ne [long]$update.bytes -or (Get-UpdateHash $source) -ne $update.sha256) { throw 'Update checksum mismatch' }
    if ($update.mode -ne 'installer') {
        $target = [IO.Path]::GetFullPath([string]$update.target)
        if ([IO.Path]::GetExtension($target) -ne '.exe' -or $source -eq $target) { throw 'Invalid update target' }
    }
    $process = Get-Process -Id ([int]$update.pid) -ErrorAction SilentlyContinue
    Set-Content -LiteralPath $ready -Value 'ready' -Encoding ASCII
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not (Test-Path -LiteralPath $commit)) {
        if ([DateTime]::UtcNow -gt $deadline) { return }
        Start-Sleep -Milliseconds 100
    }
    if ($process -and -not $process.WaitForExit(90000)) { throw 'AstraTerm did not exit' }
    if ($update.mode -eq 'installer') {
        # Display the NSIS wizard and its elevation/finish UI to the user.
        $installer = Start-Process -FilePath $source -ArgumentList '--updated' -WindowStyle Normal -PassThru
        $installer.WaitForExit()
        if ($installer.ExitCode -notin @(0, 1)) { throw ('Installer exit code ' + $installer.ExitCode) }
        Remove-Item -LiteralPath $source
        Remove-Item -LiteralPath $Config
        return
    }
    $staged = $target + '.update-' + [Guid]::NewGuid().ToString('N') + '.exe'
    Copy-Item -LiteralPath $source -Destination $staged
    if ((Get-UpdateHash $staged) -ne $update.sha256) { throw 'Staging checksum mismatch' }
    $backup = $target + '.previous'
    if (Test-Path -LiteralPath $backup) { $backup += '.' + [Guid]::NewGuid().ToString('N') }
    # Retry while the portable launcher releases its executable.
    for ($attempt=0; ; $attempt++) {
        try { Move-Item -LiteralPath $target -Destination $backup; break }
        catch { if ($attempt -ge 59) { throw }; Start-Sleep -Milliseconds 500 }
    }
    try { Move-Item -LiteralPath $staged -Destination $target } catch { Move-Item -LiteralPath $backup -Destination $target; throw }
    Start-Process -FilePath $target -WindowStyle Hidden
    Remove-Item -LiteralPath $source
    Remove-Item -LiteralPath $Config
} catch {
    $message = 'AstraTerm update failed. ' + $_.Exception.Message
    $message | Set-Content -LiteralPath ($Config + '.error.txt') -Encoding UTF8
    if (Test-Path -LiteralPath $ready) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($message, 'AstraTerm') | Out-Null
    }
    exit 1
} finally {
    Remove-Item -LiteralPath $ready, $commit -ErrorAction SilentlyContinue
    if ($staged) { Remove-Item -LiteralPath $staged -ErrorAction SilentlyContinue }
}
