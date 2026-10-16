# Publishes the add-on: first run sets up the git repo, then commits and pushes.
# GitHub Actions (.github/workflows/build.yml) then builds the aarch64 + amd64 images that Home Assistant pulls.
# Bump `version` in config.yaml for every release: HA only offers an update when it changes.
# Always pushes as j0eyoung (token from `gh`), even when another GitHub account (work) is the active login.
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
$token = gh auth token --user j0eyoung
if ($LASTEXITCODE -or -not $token) { throw 'No j0eyoung login in gh. Run: gh auth login (as j0eyoung), then retry.' }
$basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$token"))
git -c credential.helper= -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $basic" push -u origin master
$code = $LASTEXITCODE
Remove-Variable token, basic
if ($code) { throw 'git push failed' }
Write-Host "Pushed. Wait for the 'Build add-on image' action on GitHub to finish, then Check for updates in Home Assistant."
