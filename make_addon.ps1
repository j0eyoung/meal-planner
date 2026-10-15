# Publishes the add-on: first run sets up the git repo, then commits and pushes.
# GitHub Actions (.github/workflows/build.yml) then builds the aarch64 + amd64 images that Home Assistant pulls.
# Bump `version` in config.yaml for every release: HA only offers an update when it changes.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path .git)) {
    git init -b master
    if ($LASTEXITCODE) { throw 'git init failed' }
    git remote add origin https://github.com/j0eyoung/meal-planner.git
}
git add .
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "Update Add-on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    if ($LASTEXITCODE) { throw 'git commit failed' }
}
git push -u origin master
if ($LASTEXITCODE) { throw 'git push failed (does github.com/j0eyoung/meal-planner exist?)' }
Write-Host "Pushed. Wait for the 'Build add-on image' action on GitHub to finish, then Check for updates in Home Assistant."
