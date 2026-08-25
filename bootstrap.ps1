[CmdletBinding()]
param(
    [switch]$NoMessageLoop
)

$ErrorActionPreference = "Stop"

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Get-CommandPath([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return $null
    }
    return $command.Source
}

function Ensure-Command([string]$Name, [string]$PackageId) {
    $path = Get-CommandPath $Name
    if ($path) {
        return $path
    }

    if (-not (Get-CommandPath "winget")) {
        throw "'$Name' is not installed and Windows Package Manager (winget) is unavailable. Install $Name, then run this script again."
    }

    Write-Host "Installing $Name..." -ForegroundColor Cyan
    & winget install --id $PackageId --exact --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install $Name (exit code $LASTEXITCODE)."
    }

    Refresh-ProcessPath
    $path = Get-CommandPath $Name
    if (-not $path) {
        throw "$Name was installed but is not available in this PowerShell session. Open a new PowerShell window and run this script again."
    }
    return $path
}

$uv = Ensure-Command "uv" "astral-sh.uv"
$target = $PSScriptRoot

$agent = Join-Path $target "agent.py"
$exampleEnv = Join-Path $target ".env.example"
$envFile = Join-Path $target ".env"
if (-not (Test-Path $agent) -or -not (Test-Path $exampleEnv)) {
    throw "bootstrap.ps1 must be run from this project's repository checkout. agent.py or .env.example is missing."
}

if (-not (Test-Path $envFile)) {
    Copy-Item $exampleEnv $envFile
    Write-Host "Created .env from .env.example." -ForegroundColor Cyan
} else {
    Write-Host "Keeping existing .env unchanged." -ForegroundColor Cyan
}

Push-Location $target
try {
    Write-Host "Installing the isolated Python runtime and project dependencies..." -ForegroundColor Cyan
    & $uv run agent.py --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The client could not start (exit code $LASTEXITCODE)."
    }

    Write-Host "Creating a local Ed25519 identity when one is not already configured..." -ForegroundColor Cyan
    & $uv run agent.py --init
    if ($LASTEXITCODE -ne 0) {
        throw "The client could not initialise a local identity (exit code $LASTEXITCODE)."
    }

    if ($NoMessageLoop) {
        Write-Host "Setup complete. Send a message with: uv run agent.py --room lobby \"Your message\"" -ForegroundColor Green
        return
    }

    Write-Host "" 
    Write-Host "Setup complete. The private key is stored only in .env. Messages are public and cannot be recalled." -ForegroundColor Green
    Write-Host "Type /quit to exit. Choose 'lobby' or '`$FLOPPY'." -ForegroundColor Green
    while ($true) {
        $room = Read-Host "Room [lobby/`$FLOPPY]"
        if ($room -eq "/quit") { break }
        if ($room -ne "lobby" -and $room -ne "FLOPPY") {
            Write-Host "Use lobby or FLOPPY." -ForegroundColor Yellow
            continue
        }
        if ($room -eq "FLOPPY") {
            $room = "ca-cxxphyiwazuwwxd9agjca3l6gjjj4wmxogyyjczkpump"
        }

        $message = Read-Host "Message"
        if ($message -eq "/quit") { break }
        if ([string]::IsNullOrWhiteSpace($message)) {
            Write-Host "Message cannot be empty." -ForegroundColor Yellow
            continue
        }
        $confirm = Read-Host "Publish this signed message publicly? [y/N]"
        if ($confirm -notmatch "^(y|yes)$") {
            Write-Host "Not sent." -ForegroundColor Yellow
            continue
        }
        & $uv run agent.py --room $room -- $message
    }
} finally {
    Pop-Location
}
