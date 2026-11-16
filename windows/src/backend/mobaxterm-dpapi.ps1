$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Security
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$entropy = [Text.Encoding]::Default.GetBytes($request.entropy)
$header = [byte[]]@(1,0,0,0,208,140,157,223,1,21,209,17,140,122,0,192,79,194,151,235)
$headerText = [BitConverter]::ToString($header)
$sha = [Security.Cryptography.SHA512]::Create()
try {
    foreach ($record in $request.records) {
        try {
            $blob = [Convert]::FromBase64String($record)
            if ($blob.Length -lt 60) { continue }
            if ([BitConverter]::ToString($blob, 0, 20) -ne $headerText) { $blob = [byte[]]($header + $blob) }
            $clear = [Security.Cryptography.ProtectedData]::Unprotect($blob, $entropy, 'CurrentUser')
        } catch { continue }
        try {
            foreach ($encoding in @([Text.Encoding]::UTF8, [Text.Encoding]::Default)) {
                $digest = $sha.ComputeHash($encoding.GetBytes($request.master))
                $encoded = [Convert]::ToBase64String($digest)
                $expected = if ($clear.Length -eq 64) { $digest } else { [Text.Encoding]::ASCII.GetBytes($encoded) }
                if ($clear.Length -ne $expected.Length) { continue }
                $difference = 0
                for ($index = 0; $index -lt $clear.Length; $index++) {
                    $difference = $difference -bor ($clear[$index] -bxor $expected[$index])
                }
                if ($difference -eq 0) { [Console]::Write($encoded); exit 0 }
            }
        } finally { [Array]::Clear($clear, 0, $clear.Length) }
    }
} finally { $sha.Dispose() }
exit 1
