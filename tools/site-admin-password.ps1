$ErrorActionPreference = 'Stop'
$siteAdminLocal = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$siteAdminSecret = Join-Path $siteAdminLocal 'AstraTermBuild/site-admin.dpapi'
if (-not (Test-Path -LiteralPath $siteAdminSecret -PathType Leaf)) {
    throw "AstraTerm site admin password is missing at '$siteAdminSecret'. Run this script on the Windows PC and under the account where the password was set up."
}

$siteAdminProtected = (Get-Content -LiteralPath $siteAdminSecret -Raw).Trim()
try {
    $siteAdminSecure = ConvertTo-SecureString $siteAdminProtected
} catch {
    throw 'Windows could not decrypt the AstraTerm site admin password for this account.'
}
([pscredential]::new('AstraTerm', $siteAdminSecure)).GetNetworkCredential().Password | Set-Clipboard
Write-Output 'AstraTerm site admin password copied to the clipboard.'
