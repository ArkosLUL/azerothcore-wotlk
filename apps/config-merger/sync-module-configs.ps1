<#
.SYNOPSIS
    Sync AzerothCore module configs from modules/<mod>/conf/ into the live env config folder.

.DESCRIPTION
    When a module is updated its *.conf.dist gains new settings, but the copy under
    env/dist/etc/modules/ goes stale. This script refreshes each env *.conf.dist from its
    module source, then updates the active *.conf:
      - missing        -> created from the fresh .dist
      - unmodified     -> overwritten wholesale with the fresh .dist
      - customized     -> left in place; new keys are merged in by config_merger.py
                          (which preserves your custom values)

    Env dists with no matching source module (orphans) are reported and left untouched.

.PARAMETER EnvDir
    Target env module-config folder. Default: <repo>/env/dist/etc/modules.

.PARAMETER ModulesDir
    Module sources root. Default: <repo>/modules.

.PARAMETER DryRun
    Report every action without writing anything.

.PARAMETER NoMerge
    Stage 1 only (refresh dists / overwrite unmodified confs). Skip config_merger.py.

.PARAMETER KeepBackups
    Keep the config_merger *.bak backups instead of purging them from EnvDir after sync.

.EXAMPLE
    pwsh apps/config-merger/sync-module-configs.ps1 -DryRun
.EXAMPLE
    pwsh apps/config-merger/sync-module-configs.ps1
#>
[CmdletBinding()]
param(
    [string]$EnvDir,
    [string]$ModulesDir,
    [switch]$DryRun,
    [switch]$NoMerge,
    [switch]$KeepBackups
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Repo root = two levels up from this script (apps/config-merger/ -> repo).
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot  = (Resolve-Path (Join-Path $scriptDir '..\..')).Path

if (-not $EnvDir)     { $EnvDir     = Join-Path $repoRoot 'env\dist\etc\modules' }
if (-not $ModulesDir) { $ModulesDir = Join-Path $repoRoot 'modules' }

if (-not (Test-Path -LiteralPath $ModulesDir)) { throw "Modules dir not found: $ModulesDir" }
if (-not (Test-Path -LiteralPath $EnvDir))     { throw "Env config dir not found: $EnvDir" }

# Normalize file text for comparison: UTF-8, CRLF->LF, strip trailing blank lines.
function Get-NormalizedText([string]$path)
{
    $raw = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    return ($raw -replace "`r`n", "`n").TrimEnd("`n")
}

function Test-SameContent([string]$a, [string]$b)
{
    return (Get-NormalizedText $a) -eq (Get-NormalizedText $b)
}

$distsRefreshed = 0
$distsUpToDate  = 0
$confsCreated   = 0
$confsOverwritten = 0
$confsToMerge   = New-Object System.Collections.Generic.List[string]

$srcDists = @(Get-ChildItem -LiteralPath $ModulesDir -Recurse -Filter '*.conf.dist' -File |
            Where-Object { $_.Directory.Name -eq 'conf' })

Write-Host "Syncing $($srcDists.Count) module config(s) from '$ModulesDir' -> '$EnvDir'" -ForegroundColor Cyan
if ($DryRun) { Write-Host "[DRY RUN] no files will be written" -ForegroundColor Yellow }
Write-Host ""

foreach ($src in $srcDists)
{
    $baseName = $src.Name                                   # e.g. playerbots.conf.dist
    $confName = $baseName.Substring(0, $baseName.Length - 5) # drop ".dist" -> playerbots.conf
    $destDist = Join-Path $EnvDir $baseName
    $destConf = Join-Path $EnvDir $confName

    $distExists = Test-Path -LiteralPath $destDist
    $confExists = Test-Path -LiteralPath $destConf

    # Capture unmodified state against the OLD dist, before we overwrite it.
    $wasUnmodified = $confExists -and $distExists -and (Test-SameContent $destConf $destDist)

    # --- Stage 1: refresh the .dist ---
    if ($distExists -and (Test-SameContent $src.FullName $destDist))
    {
        $distsUpToDate++
        Write-Host "  [dist  up-to-date] $baseName"
    }
    else
    {
        Write-Host "  [dist  refresh   ] $baseName" -ForegroundColor Green
        if (-not $DryRun) { Copy-Item -LiteralPath $src.FullName -Destination $destDist -Force }
        $distsRefreshed++
    }

    # --- Stage 2: active .conf ---
    if (-not $confExists)
    {
        Write-Host "  [conf  create    ] $confName (from fresh dist)" -ForegroundColor Green
        if (-not $DryRun) { Copy-Item -LiteralPath $src.FullName -Destination $destConf -Force }
        $confsCreated++
    }
    elseif ($wasUnmodified)
    {
        # Was a verbatim copy of the old dist -> safe to replace wholesale.
        if (-not $DryRun) { Copy-Item -LiteralPath $src.FullName -Destination $destConf -Force }
        Write-Host "  [conf  overwrite ] $confName (was unmodified)" -ForegroundColor Green
        $confsOverwritten++
    }
    else
    {
        Write-Host "  [conf  customized] $confName -> will merge new keys" -ForegroundColor Yellow
        $confsToMerge.Add($confName) | Out-Null
    }
}

# --- Orphan detection: env dists with no source module ---
$srcNames = @{}
foreach ($s in $srcDists) { $srcNames[$s.Name] = $true }
$orphans = @(Get-ChildItem -LiteralPath $EnvDir -Filter '*.conf.dist' -File |
           Where-Object { -not $srcNames.ContainsKey($_.Name) } |
           ForEach-Object { $_.Name })

# --- Stage 3: merge customized confs via existing config_merger.py ---
$mergeInvoked = $false
if (-not $NoMerge -and $confsToMerge.Count -gt 0)
{
    $merger   = Join-Path $scriptDir 'python\config_merger.py'
    $mergeCfg = Split-Path -Parent $EnvDir   # config_merger expects <cfgDir>/modules/

    $py = $null
    foreach ($cand in @('python', 'python3'))
    {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) { $py = $cmd.Source; break }
    }

    Write-Host ""
    if (-not $py)
    {
        Write-Host "Python not found on PATH. Merge $($confsToMerge.Count) customized conf(s) manually:" -ForegroundColor Yellow
        Write-Host "  python `"$merger`" `"$mergeCfg`" modules -y"
    }
    elseif ($DryRun)
    {
        Write-Host "[DRY RUN] would merge new keys into: $($confsToMerge -join ', ')" -ForegroundColor Yellow
        Write-Host "          via: `"$py`" `"$merger`" `"$mergeCfg`" modules -y"
    }
    else
    {
        Write-Host "Merging new keys into customized conf(s) via config_merger.py ..." -ForegroundColor Cyan
        & $py $merger $mergeCfg 'modules' '-y'
        $mergeInvoked = $true
    }
}

# --- Cleanup: purge config_merger *.bak backups from EnvDir ---
$bakRemoved = 0
if (-not $KeepBackups)
{
    $baks = @(Get-ChildItem -LiteralPath $EnvDir -Filter '*.bak' -File)
    if ($baks.Count -gt 0)
    {
        Write-Host ""
        if ($DryRun)
        {
            Write-Host "[DRY RUN] would remove $($baks.Count) backup file(s) from '$EnvDir'" -ForegroundColor Yellow
        }
        else
        {
            foreach ($bak in $baks)
            {
                Remove-Item -LiteralPath $bak.FullName -Force
                Write-Host "  [bak   removed   ] $($bak.Name)" -ForegroundColor DarkGray
                $bakRemoved++
            }
        }
    }
}

# --- Summary ---
Write-Host ""
Write-Host "==================== Summary ====================" -ForegroundColor Cyan
Write-Host "  dists refreshed     : $distsRefreshed"
Write-Host "  dists up-to-date    : $distsUpToDate"
Write-Host "  confs created       : $confsCreated"
Write-Host "  confs overwritten   : $confsOverwritten"
Write-Host "  confs merged        : $($confsToMerge.Count)$(if (-not $mergeInvoked -and $confsToMerge.Count -gt 0) { ' (pending - run merge above)' })"
Write-Host "  orphan dists skipped: $($orphans.Count)"
Write-Host "  backups removed     : $bakRemoved$(if ($KeepBackups) { ' (kept - -KeepBackups)' })"
foreach ($o in $orphans) { Write-Host "      - $o" -ForegroundColor DarkGray }
if ($DryRun) { Write-Host "  (dry run - nothing written)" -ForegroundColor Yellow }
