[CmdletBinding()]
param(
    [ValidateRange(0, 10000)]
    [int]$Seed = 23,
    [string]$CandidateInputs
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime_paths.ps1')
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$composePath = Join-Path $projectRoot 'deploy\pours\deployment\compose.yaml'
$overridePath = Join-Path $projectRoot 'deploy\root-user.override.yaml'
$secretPath = Get-RelaySignozSecretPath
$dockerConfig = Join-Path $projectRoot 'receipts\work\docker-config'
$dockerConfigFile = Join-Path $dockerConfig 'config.json'
$setupReceiptPath = Join-Path $projectRoot 'receipts\work\reproduce-setup-latest.json'
$script:setupStarted = [DateTimeOffset]::UtcNow
$script:completedSetupStages = [System.Collections.ArrayList]::new()

function ConvertTo-ProcessArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes += 1
            continue
        }
        if ($character -eq [char]34) {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append($character)
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Write-SetupReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$FailedStage,
        [string]$Code,
        [Nullable[int]]$ExitCode,
        [string]$CauseType
    )

    $payload = [ordered]@{
        schema_version = 1
        status = $Status
        generated_at = [DateTimeOffset]::UtcNow.ToString('o')
        elapsed_milliseconds = [int](
            ([DateTimeOffset]::UtcNow - $script:setupStarted).TotalMilliseconds
        )
        completed_stages = @($script:completedSetupStages)
    }
    if ($Status -eq 'failed') {
        $payload['error'] = [ordered]@{
            code = $Code
            stage = $FailedStage
            exit_code = $ExitCode
            cause_type = $CauseType
        }
    }
    $directory = Split-Path -Parent $setupReceiptPath
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    $temporaryPath = Join-Path $directory (
        '.reproduce-setup.{0}.tmp' -f [Guid]::NewGuid()
    )
    $backupPath = Join-Path $directory (
        '.reproduce-setup.{0}.bak' -f [Guid]::NewGuid()
    )
    try {
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            (($payload | ConvertTo-Json -Depth 8) + "`n"),
            [System.Text.UTF8Encoding]::new($false)
        )
        if (Test-Path -LiteralPath $setupReceiptPath -PathType Leaf) {
            [System.IO.File]::Replace(
                $temporaryPath,
                $setupReceiptPath,
                $backupPath,
                $true
            )
            Remove-Item -LiteralPath $backupPath -Force
        }
        else {
            [System.IO.File]::Move($temporaryPath, $setupReceiptPath)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            Remove-Item -LiteralPath $backupPath -Force
        }
    }
}

function Stop-BoundedProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $children = Get-CimInstance Win32_Process `
        -Filter "ParentProcessId = $ProcessId" `
        -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-BoundedProcessTree -ProcessId ([int]$child.ProcessId)
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Invoke-BoundedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)][ValidateRange(1, 900)][int]$TimeoutSeconds,
        [switch]$PropagateExitCode
    )

    $arguments = ($ArgumentList | ForEach-Object {
        ConvertTo-ProcessArgument -Value $_
    }) -join ' '
    $started = [DateTimeOffset]::UtcNow
    $process = $null
    $stdoutTask = $null
    $stderrTask = $null
    try {
        try {
            $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
            $startInfo.FileName = $FilePath
            $startInfo.Arguments = $arguments
            $startInfo.WorkingDirectory = $projectRoot
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
            $startInfo.RedirectStandardOutput = $true
            $startInfo.RedirectStandardError = $true
            $process = [System.Diagnostics.Process]::new()
            $process.StartInfo = $startInfo
            if (-not $process.Start()) {
                throw "The operating system refused to start $Stage."
            }
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()
        }
        catch {
            Write-SetupReceipt `
                -Status 'failed' `
                -FailedStage $Stage `
                -Code 'setup_command_start_failed' `
                -CauseType $_.Exception.GetType().Name
            throw
        }

        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            Stop-BoundedProcessTree -ProcessId $process.Id
            $process.WaitForExit()
            Write-SetupReceipt `
                -Status 'failed' `
                -FailedStage $Stage `
                -Code 'setup_command_timeout' `
                -CauseType 'TimeoutException'
            throw "$Stage exceeded its $TimeoutSeconds-second deadline."
        }
        $process.WaitForExit()
        $actualExitCode = [int]$process.ExitCode

        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($stdout.Length -gt 0) {
            [Console]::Out.Write($stdout)
        }
        if ($stderr.Length -gt 0) {
            [Console]::Error.Write($stderr)
        }
        if ($actualExitCode -ne 0) {
            Write-SetupReceipt `
                -Status 'failed' `
                -FailedStage $Stage `
                -Code 'setup_command_failed' `
                -ExitCode $actualExitCode `
                -CauseType 'ProcessExitCode'
            if ($PropagateExitCode) {
                return $actualExitCode
            }
            throw "$Stage failed with exit code $actualExitCode."
        }
        [void]$script:completedSetupStages.Add([ordered]@{
            stage = $Stage
            elapsed_milliseconds = [int](
                ([DateTimeOffset]::UtcNow - $started).TotalMilliseconds
            )
            exit_code = $actualExitCode
        })
        if ($PropagateExitCode) {
            return $actualExitCode
        }
    }
    finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
    }
}

function Wait-LocalEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [DateTimeOffset]$Deadline
    )

    while ([DateTimeOffset]::UtcNow -lt $Deadline) {
        try {
            $response = Invoke-WebRequest `
                -Uri $Uri `
                -UseBasicParsing `
                -TimeoutSec 15
            if ($response.StatusCode -eq 200) {
                return
            }
        }
        catch {
            # Startup failures are retried until the shared deadline.
        }
        Start-Sleep -Seconds 2
    }
    throw "$Name did not become ready before the four-minute deadline."
}

$uvx = (Get-Command uvx -ErrorAction Stop).Source
Invoke-BoundedProcess `
    -FilePath $uvx `
    -ArgumentList @('uv@0.12.5', 'sync', '--frozen', '--project', $projectRoot) `
    -Stage 'python_environment_sync' `
    -TimeoutSeconds 180
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The project Python executable was not created.'
}
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($null -ne $dockerCommand) {
    $docker = $dockerCommand.Source
}
else {
    $docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
}
if (-not (Test-Path -LiteralPath $docker -PathType Leaf)) {
    throw 'Docker Desktop CLI was not found.'
}

& (Join-Path $PSScriptRoot 'prepare_signoz_secret.ps1') -ProjectRoot $projectRoot

[System.IO.Directory]::CreateDirectory($dockerConfig) | Out-Null
if (-not (Test-Path -LiteralPath $dockerConfigFile)) {
    [System.IO.File]::WriteAllText(
        $dockerConfigFile,
        "{}`n",
        [System.Text.UTF8Encoding]::new($false)
    )
}

Invoke-BoundedProcess `
    -FilePath $docker `
    -ArgumentList @(
        '--config', $dockerConfig,
        'compose',
        '--env-file', $secretPath,
        '-f', $composePath,
        '-f', $overridePath,
        'up', '-d'
    ) `
    -Stage 'pinned_stack_start' `
    -TimeoutSeconds 300

$deadline = [DateTimeOffset]::UtcNow.AddMinutes(4)
$healthStarted = [DateTimeOffset]::UtcNow
try {
    Wait-LocalEndpoint `
        -Uri 'http://127.0.0.1:13133/' `
        -Name 'The OTLP collector' `
        -Deadline $deadline
    Wait-LocalEndpoint `
        -Uri 'http://127.0.0.1:8080/api/v1/health' `
        -Name 'SigNoz' `
        -Deadline $deadline
    [void]$script:completedSetupStages.Add([ordered]@{
        stage = 'stack_health_readiness'
        elapsed_milliseconds = [int](
            ([DateTimeOffset]::UtcNow - $healthStarted).TotalMilliseconds
        )
        exit_code = 0
    })
}
catch {
    Write-SetupReceipt `
        -Status 'failed' `
        -FailedStage 'stack_health_readiness' `
        -Code 'setup_health_deadline_exceeded' `
        -CauseType $_.Exception.GetType().Name
    throw
}

$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $projectRoot 'src'
try {
    Invoke-BoundedProcess `
        -FilePath $python `
        -ArgumentList @(
            (Join-Path $projectRoot 'scripts\bootstrap_signoz_access.py')
        ) `
        -Stage 'signoz_access_bootstrap' `
        -TimeoutSeconds 60
    $reproduceArguments = @(
        (Join-Path $projectRoot 'scripts\reproduce.py'),
        '--seed',
        $Seed
    )
    if (-not [string]::IsNullOrWhiteSpace($CandidateInputs)) {
        $reproduceArguments += @('--candidate-inputs', $CandidateInputs)
    }
    $productExitCode = Invoke-BoundedProcess `
        -FilePath $python `
        -ArgumentList $reproduceArguments `
        -Stage 'crash_resume_export_queryback' `
        -TimeoutSeconds 110 `
        -PropagateExitCode
    if ($productExitCode -ne 0) {
        exit $productExitCode
    }
    Write-SetupReceipt -Status 'passed'
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}
