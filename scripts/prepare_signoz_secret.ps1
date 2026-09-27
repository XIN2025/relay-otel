[CmdletBinding()]
param(
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'runtime_paths.ps1')

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

$resolvedRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$secretPath = Get-RelaySignozSecretPath
$secretDirectory = Split-Path -Parent $secretPath
$legacySecretPath = Join-Path $resolvedRoot 'receipts\work\signoz-root.env'

[System.IO.Directory]::CreateDirectory($secretDirectory) | Out-Null

function Set-PrivateAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,
        [Parameter(Mandatory = $true)]
        [bool]$Directory
    )

    if ($env:OS -ne 'Windows_NT') {
        & chmod $(if ($Directory) { '700' } else { '600' }) -- $LiteralPath
        if ($LASTEXITCODE -ne 0) {
            throw "Could not restrict permissions on $LiteralPath"
        }
        return
    }

    $currentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $suffix = if ($Directory) { '(OI)(CI)F' } else { 'F' }
    $grants = @(
        ('*{0}:{1}' -f $currentUserSid, $suffix),
        ('*S-1-5-18:{0}' -f $suffix),
        ('*S-1-5-32-544:{0}' -f $suffix)
    )
    & icacls.exe $LiteralPath /inheritance:r /grant:r @grants | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restrict the Windows ACL on $LiteralPath"
    }

    $allowedSids = @($currentUserSid, 'S-1-5-18', 'S-1-5-32-544')
    $unexpectedRules = (Get-Acl -LiteralPath $LiteralPath).Access | Where-Object {
        try {
            $sid = $_.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
            $sid -notin $allowedSids
        }
        catch {
            $true
        }
    }
    if ($unexpectedRules.Count -ne 0) {
        throw "The restricted Windows ACL on $LiteralPath contains an unexpected principal."
    }
}

Set-PrivateAcl -LiteralPath $secretDirectory -Directory $true

function New-RandomToken {
    param([int]$ByteCount)

    $randomBytes = [byte[]]::new($ByteCount)
    $randomSource = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $randomSource.GetBytes($randomBytes)
    }
    finally {
        $randomSource.Dispose()
    }

    return [Convert]::ToBase64String($randomBytes).Replace('+', 'A').Replace('/', 'b').TrimEnd('=')
}

$values = @{}
$sourceSecretPaths = @()
if (Test-Path -LiteralPath $legacySecretPath -PathType Leaf) {
    $sourceSecretPaths += $legacySecretPath
}
if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
    # The destination is authoritative when both paths exist; legacy values only
    # fill keys that are absent from it during a one-time migration.
    $sourceSecretPaths += $secretPath
}
foreach ($sourceSecretPath in $sourceSecretPaths) {
    foreach ($line in [System.IO.File]::ReadAllLines($sourceSecretPath)) {
        $separator = $line.IndexOf('=')
        if ($separator -gt 0) {
            $values[$line.Substring(0, $separator)] = $line.Substring($separator + 1)
        }
    }
}

if (-not $values.ContainsKey('SIGNOZ_ROOT_PASSWORD')) {
    $values['SIGNOZ_ROOT_PASSWORD'] = "R1!$(New-RandomToken -ByteCount 24)"
}
if (-not $values.ContainsKey('SIGNOZ_JWT_SECRET')) {
    $values['SIGNOZ_JWT_SECRET'] = New-RandomToken -ByteCount 48
}

$lines = @(
    "SIGNOZ_ROOT_PASSWORD=$($values['SIGNOZ_ROOT_PASSWORD'])"
    "SIGNOZ_JWT_SECRET=$($values['SIGNOZ_JWT_SECRET'])"
)
if ($values.ContainsKey('SIGNOZ_API_KEY')) {
    $lines += "SIGNOZ_API_KEY=$($values['SIGNOZ_API_KEY'])"
}
$contents = $lines -join "`n"
$contents += "`n"

$temporaryPath = Join-Path $secretDirectory ('.signoz-root.{0}.tmp' -f [Guid]::NewGuid())
$backupPath = Join-Path $secretDirectory ('.signoz-root.{0}.bak' -f [Guid]::NewGuid())
try {
    [System.IO.File]::WriteAllText(
        $temporaryPath,
        $contents,
        [System.Text.UTF8Encoding]::new($false)
    )
    Set-PrivateAcl -LiteralPath $temporaryPath -Directory $false
    if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
        [System.IO.File]::Replace($temporaryPath, $secretPath, $backupPath, $true)
        Remove-Item -LiteralPath $backupPath -Force
    }
    else {
        [System.IO.File]::Move($temporaryPath, $secretPath)
    }
    Set-PrivateAcl -LiteralPath $secretPath -Directory $false
}
finally {
    if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
    if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
        Remove-Item -LiteralPath $backupPath -Force
    }
}

if (
    $legacySecretPath -ne $secretPath -and
    (Test-Path -LiteralPath $legacySecretPath -PathType Leaf)
) {
    Remove-Item -LiteralPath $legacySecretPath -Force
    Write-Output "Migrated and removed the legacy repository-local secret file: $legacySecretPath"
}

Write-Output "Prepared host-local runtime secrets with restricted access: $secretPath"
