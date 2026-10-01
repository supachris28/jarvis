# Build the Jarvis image and push it to registry.keeeys.uk (run from the repo root on the PC).
#   docker login registry.keeeys.uk        # once; Docker stores the credentials
#   .\scripts\build-push.ps1               # tags :latest and :<yyyyMMdd-HHmm>
#   .\scripts\build-push.ps1 -Platform linux/arm64   # if the server is ARM
param(
    [string]$Registry = "registry.keeeys.uk",
    [string]$Image = "jarvis",
    [string]$Tag = (Get-Date -Format "yyyyMMdd-HHmm"),
    [string]$Platform = "linux/amd64"
)
$ErrorActionPreference = "Stop"
$ref = "$Registry/$Image"
Write-Host "Building $ref`:$Tag ($Platform)"
docker buildx build --platform $Platform -t "$ref`:$Tag" -t "$ref`:latest" --push .
if ($LASTEXITCODE -ne 0) { throw "Build or push failed" }
Write-Host "Pushed $ref`:$Tag and $ref`:latest"
Write-Host "On the server: cd /opt/jarvis; docker compose pull; docker compose up -d"
