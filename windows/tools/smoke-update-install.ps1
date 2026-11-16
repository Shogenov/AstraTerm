# Exercise the real replacement helper with two locally compiled harmless fixtures.
$ErrorActionPreference='Stop'
$project=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$fixture=Join-Path $project ('artifacts/website/update-helper-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture | Out-Null
$compiler=Join-Path $env:SystemRoot 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$oldSource=Join-Path $fixture 'old.cs';$newSource=Join-Path $fixture 'new.cs'
'using System; using System.IO; class Fixture {static void Main(string[] args){File.WriteAllText(Environment.GetEnvironmentVariable("ASTRA_UPDATE_TEST_MARKER"),String.Join(" ", args));}}' | Set-Content -LiteralPath $newSource -Encoding ASCII
$target=Join-Path $fixture 'AstraTerm-test.exe';$source=Join-Path $fixture 'verified-update.exe'
'using System.Threading; class Fixture {static void Main(){Thread.Sleep(2000);}}' | Set-Content -LiteralPath $oldSource -Encoding ASCII
& $compiler /nologo /target:winexe "/out:$target" $oldSource
if($LASTEXITCODE){throw 'Could not build old fixture'}
& $compiler /nologo /target:winexe "/out:$source" $newSource
if($LASTEXITCODE){throw 'Could not build new fixture'}
$originalHash=(Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
$newHash=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
$runner=Join-Path $fixture 'handoff.cjs'
@'
const fs = require('node:fs');
const { handoffUpdate } = require(process.argv[2]);
(async () => {
  const [config, source, target, sha256, mode, marker] = process.argv.slice(3);
  process.env.ASTRA_UPDATE_TEST_MARKER = marker;
  fs.writeFileSync(config, JSON.stringify({ pid: process.pid, source, target, sha256, mode, bytes: fs.statSync(source).size }));
  await handoffUpdate(config);
  if (fs.existsSync(marker)) throw Error('Update started before AstraTerm exited');
})().catch(error => { console.error(error.message); process.exitCode = 1; });
'@ | Set-Content -LiteralPath $runner -Encoding ASCII
$module=Join-Path $PSScriptRoot '../src/update-ui.cjs'
function Invoke-AstraFixtureUpdate($mode,$file,$hash) {
    $config=Join-Path $fixture ($mode+'.json')
    $marker=Join-Path $fixture ($mode+'.txt')
    & node $runner $module $config $file $target $hash $mode $marker
    if($LASTEXITCODE){throw "Could not hand off $mode update"}
    for($attempt=0;$attempt -lt 120 -and ((Test-Path -LiteralPath $config) -or -not(Test-Path -LiteralPath $marker));$attempt++){Start-Sleep -Milliseconds 100}
    if(-not(Test-Path -LiteralPath $marker)){throw "$mode executable did not launch"}
    if(Test-Path -LiteralPath $config){throw "$mode update config was not cleaned"}
    return Get-Content -LiteralPath $marker -Raw
}
$setup=Join-Path $fixture 'verified-setup.exe'
Copy-Item -LiteralPath $source -Destination $setup
Invoke-AstraFixtureUpdate 'portable' $source $newHash | Out-Null
if((Get-FileHash -LiteralPath ($target+'.previous') -Algorithm SHA256).Hash -ne $originalHash){throw 'Previous executable was not preserved'}
if((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $newHash){throw 'Target bytes differ from the verified update'}
if((Invoke-AstraFixtureUpdate 'installer' $setup $newHash) -ne '--updated'){throw 'Installer did not receive the expected arguments'}
$broken=Join-Path $fixture 'broken.json'
& node $runner $module $broken $target $target ('0'*64) 'installer' (Join-Path $fixture 'unexpected.txt') 2>$null
if($LASTEXITCODE -eq 0 -or (Test-Path -LiteralPath ($broken+'.commit')) -or (Test-Path -LiteralPath (Join-Path $fixture 'unexpected.txt'))){
    throw 'Invalid update was committed'
}
'UPDATE_HELPER_PASS launch / preflight / detached wait / backup / replace / restart / installer'
