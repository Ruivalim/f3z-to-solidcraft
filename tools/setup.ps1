<#
.SYNOPSIS
Installs everything the converter needs, without make.

.DESCRIPTION
Does what `make setup` does: creates the Python environment and installs the
dependencies, builds cadmpeg if it is missing, then checks that every tool is
usable. Everything is installed under %LOCALAPPDATA%\f3z-to-solidcraft, because
the project sits on a network share and a Python environment there cannot load
numpy's DLLs.

.PARAMETER Step
The step to run: venv, cadmpeg, check, or all three (the default).

.PARAMETER Recreate
Delete the Python environment and build it again from scratch. Only with the
venv step or all of them.

.EXAMPLE
.\tools\setup.ps1

.EXAMPLE
.\tools\setup.ps1 -Recreate

.EXAMPLE
.\tools\setup.ps1 -Step check
#>
[CmdletBinding()]
param(
    [ValidateSet('all', 'venv', 'cadmpeg', 'check')][string]$Step = 'all',
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $PSScriptRoot
$AppDir = Join-Path $env:LOCALAPPDATA 'f3z-to-solidcraft'
$VenvDir = Join-Path $AppDir 'venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$CadmpegDir = Join-Path $AppDir 'cadmpeg'
$Cadmpeg = Join-Path $CadmpegDir 'bin\cadmpeg.exe'
$CadmpegVersion = '0.5.5'
$LogFile = Join-Path $AppDir 'logs\setup.log'

# ezf3d needs Python 3.13; uv fetches it if it is not installed.
$PythonVersion = '3.13'
# The default Rust toolchain here is MSVC, which has no linker installed; the
# GNU toolchain builds cadmpeg with the mingw gcc that is.
$RustToolchain = 'stable-x86_64-pc-windows-gnu'

function Write-Log {
    <#
    .SYNOPSIS
    Writes one line to the log file and to the console.

    .PARAMETER Message
    The text of the line.

    .PARAMETER Level
    INFO, DEBUG or ERROR.
    #>
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('INFO', 'DEBUG', 'ERROR')][string]$Level = 'INFO'
    )
    $line = '[{0}] {1} [setup]: {2}' -f $Level, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss,fff'), $Message
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Initialize-Log {
    <#
    .SYNOPSIS
    Creates the folder the log file is written to.
    #>
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null
}

function Assert-Command {
    <#
    .SYNOPSIS
    Stops the script if a program is not on PATH.

    .PARAMETER Name
    The program to look for.

    .PARAMETER Hint
    Where to get the program, shown when it is missing.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Hint
    )
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is not on PATH. $Hint"
    }
}

function Invoke-Tool {
    <#
    .SYNOPSIS
    Runs an external program and stops the script if it fails.

    .PARAMETER FilePath
    The program to run.

    .PARAMETER Arguments
    The arguments to pass to it.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    Write-Log -Level DEBUG -Message "Running: $FilePath $($Arguments -join ' ')"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE"
    }
}

function Remove-Venv {
    <#
    .SYNOPSIS
    Deletes the Python environment if there is one.
    #>
    if (Test-Path -LiteralPath $VenvDir) {
        Write-Log "Removing the Python environment at $VenvDir"
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    }
}

function Install-Venv {
    <#
    .SYNOPSIS
    Creates the Python environment if it is missing and installs the dependencies.
    #>
    Assert-Command -Name 'uv' -Hint 'Install it from https://docs.astral.sh/uv/'
    if (Test-Path -LiteralPath $VenvPython) {
        Write-Log "Python environment already exists at $VenvDir"
    }
    else {
        Write-Log "Creating the Python environment at $VenvDir"
        Invoke-Tool -FilePath 'uv' -Arguments @('venv', '--python', $PythonVersion, $VenvDir)
    }
    Write-Log 'Installing the Python dependencies'
    Invoke-Tool -FilePath 'uv' -Arguments @('pip', 'install', '--python', $VenvDir, '-r', (Join-Path $ProjectDir 'requirements.txt'))
}

function Install-Cadmpeg {
    <#
    .SYNOPSIS
    Builds and installs cadmpeg from crates.io if it is missing (about 10 minutes).
    #>
    if (Test-Path -LiteralPath $Cadmpeg) {
        Write-Log "cadmpeg already installed at $Cadmpeg"
        return
    }
    Assert-Command -Name 'cargo' -Hint 'Install Rust from https://rustup.rs/'
    Write-Log "Building cadmpeg $CadmpegVersion (about 10 minutes)"
    Invoke-Tool -FilePath 'cargo' -Arguments @("+$RustToolchain", 'install', 'cadmpeg', '--version', $CadmpegVersion, '--locked', '--root', $CadmpegDir)
}

function Test-Setup {
    <#
    .SYNOPSIS
    Checks that Python, ezf3d, cadmpeg and SolidCraft are all usable.
    #>
    Write-Log 'Checking the installation'
    # load_config reads config.yaml and imports utils, both relative to the project folder.
    Push-Location -LiteralPath $ProjectDir
    try {
        Invoke-Tool -FilePath $VenvPython -Arguments @('--version')
        Invoke-Tool -FilePath $VenvPython -Arguments @('-c', "import ezf3d, numpy, rich, yaml; print('ezf3d, numpy, rich, yaml: ok')")
        Invoke-Tool -FilePath $VenvPython -Arguments @('-c', "from pathlib import Path; from utils.solvecraft_session import load_config; c = load_config(Path('config.yaml')); print('SolidCraft:', c.cli); print('cadmpeg:   ', c.cadmpeg)")
        Invoke-Tool -FilePath $Cadmpeg -Arguments @('--version')
    }
    finally {
        Pop-Location
    }
}

function Invoke-Setup {
    <#
    .SYNOPSIS
    Runs the chosen setup steps in order.
    #>
    Initialize-Log
    Write-Log "Setup started for $ProjectDir (step: $Step)"
    try {
        if ($Recreate) {
            if ($Step -notin 'all', 'venv') {
                throw "-Recreate only applies to the venv step, not $Step"
            }
            Remove-Venv
        }
        if ($Step -in 'all', 'venv') {
            Install-Venv
        }
        if ($Step -in 'all', 'cadmpeg') {
            Install-Cadmpeg
        }
        if ($Step -in 'all', 'check') {
            Test-Setup
        }
    }
    catch {
        Write-Log -Level ERROR -Message "Setup failed: $($_.Exception.Message)"
        Write-Log -Level ERROR -Message $_.ScriptStackTrace
        exit 1
    }
    Write-Log 'Setup finished'
}

$env:PYTHONUTF8 = '1'
Invoke-Setup
