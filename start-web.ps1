[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "bootstrap.ps1") -NoMessageLoop
if ($LASTEXITCODE -ne 0) {
    throw "Local setup failed (exit code $LASTEXITCODE)."
}

$arguments = @("run", "web.py", "--port", $Port)
if ($NoBrowser) {
    $arguments += "--no-browser"
}
& uv @arguments
