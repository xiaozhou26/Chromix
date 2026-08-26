[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$RunId,
  [Parameter(Mandatory)] [int]$StageIndex
)
$ErrorActionPreference = 'Stop'
$headers = @{
  Authorization = "Bearer $env:GH_TOKEN"
  Accept = 'application/vnd.github+json'
}
$repo = $env:GITHUB_REPOSITORY
if (-not $repo) { throw 'GITHUB_REPOSITORY is not set' }
if ($StageIndex -lt 1 -or $StageIndex -gt 11) { throw "invalid source stage: $StageIndex" }
$api = "https://api.github.com/repos/$repo/actions/runs/$RunId/artifacts?per_page=100"
$artifacts = Invoke-RestMethod -Headers $headers -Uri $api
$prefix = "tree-s$StageIndex-attempt-"
$parts = @($artifacts.artifacts | Where-Object {
  $_.name -like "$prefix*part*" -and -not $_.expired
})
if ($parts.Count -eq 0) { throw "No non-expired stage $StageIndex artifacts found in run $RunId" }
New-Item -ItemType Directory -Force -Path C:\restore | Out-Null
foreach ($artifact in $parts) {
  $zip = Join-Path $env:TEMP "$($artifact.id).zip"
  try {
    Invoke-WebRequest -Headers $headers -Uri $artifact.archive_download_url -OutFile $zip
    Expand-Archive -LiteralPath $zip -DestinationPath C:\restore -Force
  } finally {
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
  }
}
if (-not (Test-Path C:\restore\tree.7z.001)) {
  throw "Stage $StageIndex archive is incomplete in run $RunId"
}
Write-Host "==> restored stage $StageIndex artifacts from run $RunId"
