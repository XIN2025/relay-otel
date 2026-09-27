function Get-RelaySignozSecretPath {
    [CmdletBinding()]
    param()

    if ($env:OS -eq 'Windows_NT') {
        $localStateRoot = [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::LocalApplicationData
        )
        if ([string]::IsNullOrWhiteSpace($localStateRoot)) {
            if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
                $localStateRoot = $env:LOCALAPPDATA
            }
            elseif (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
                $localStateRoot = Join-Path $env:USERPROFILE 'AppData\Local'
            }
            else {
                throw 'A per-user local application-data directory could not be resolved.'
            }
        }
    }
    else {
        if (
            -not [string]::IsNullOrWhiteSpace($env:XDG_STATE_HOME) -and
            [System.IO.Path]::IsPathRooted($env:XDG_STATE_HOME)
        ) {
            $localStateRoot = $env:XDG_STATE_HOME
        }
        else {
            $userProfile = [Environment]::GetFolderPath(
                [Environment+SpecialFolder]::UserProfile
            )
            if ([string]::IsNullOrWhiteSpace($userProfile)) {
                throw 'A per-user home directory could not be resolved.'
            }
            $localStateRoot = Join-Path (Join-Path $userProfile '.local') 'state'
        }
    }

    return Join-Path (
        Join-Path ([System.IO.Path]::GetFullPath($localStateRoot)) 'relay-otel-poc'
    ) 'signoz-root.env'
}
