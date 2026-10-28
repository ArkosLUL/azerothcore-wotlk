<#
.SYNOPSIS
    Dumps the WoW 3.3.5a client when it freezes, and saves the server-side evidence for every freeze or
    crash.

.DESCRIPTION
    Once the client window has not answered for -FreezeSeconds, ProcDump writes a mini and a full dump,
    then a mini dump every -FollowUpSeconds while the client stays frozen, up to -FollowUps more, then
    the script beeps: from then on closing the game loses nothing. Each freeze gets a folder in
    <client>\Errors\Hangs holding the dumps, a copy of the client's Logs folder and freeze.txt.

    When the game exits, the botobs traces overlapping each freeze or new client crash report, and the
    worldserver console log around it, are copied next to it, since traces age out after a few days.
    Only the newest -Keep freeze folders and crash evidence folders are kept.

.EXAMPLE
    .\watch-freezes.ps1 -Launch 'A:\WOW\world of warcraft 3.3.5a hd\wow_loot64.exe'

    Starts the game and watches it until it exits.

.EXAMPLE
    .\watch-freezes.ps1 -Launch 'A:\WOW\world of warcraft 3.3.5a hd\wow_loot64.exe' -CreateShortcut

    Writes 'WoW + freeze catcher.lnk' next to the exe, which runs the line above minimized.

.EXAMPLE
    .\watch-freezes.ps1

    Waits for any running 3.3.5 client whose process name matches -ProcessName, watches it, then waits
    for the next one.

.EXAMPLE
    .\watch-freezes.ps1 -ProcessId 1234

    Watches a client that is already running.
#>
[CmdletBinding()]
param(
    [string]$Launch,
    [int]$ProcessId,
    [string]$ProcessName = 'wow*',
    [string]$ClientDir,
    [int]$FreezeSeconds = 5,
    [int]$FollowUps = 8,
    [int]$FollowUpSeconds = 2,
    [int]$ArmAfterSeconds = 30,
    [int]$Keep = 5,
    [string]$ProcDump,
    [string]$Traces,
    [string]$Container = 'ac-worldserver',
    [switch]$CreateShortcut
)

if (-not ('ClientCrash.Native' -as [type])) {
    Add-Type -Namespace ClientCrash -Name Native -MemberDefinition @'
[DllImport("user32.dll")]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam, IntPtr lParam,
    uint flags, uint timeoutMs, out UIntPtr result);
'@
}

function Write-Status([string]$Text) {
    Write-Host ('{0:HH:mm:ss}  {1}' -f (Get-Date), $Text)
}

function Resolve-ProcDump([string]$Path) {
    if ($Path) {
        if (Test-Path -LiteralPath $Path) { return (Resolve-Path -LiteralPath $Path).Path }
        throw "ProcDump not found at $Path"
    }
    $default = Join-Path $env:LOCALAPPDATA 'Programs\Sysinternals\procdump.exe'
    if (Test-Path -LiteralPath $default) { return $default }
    $command = Get-Command procdump.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw ('ProcDump not found: unzip https://download.sysinternals.com/files/Procdump.zip ' +
        'into %LOCALAPPDATA%\Programs\Sysinternals, or pass -ProcDump')
}

function Test-Client($Process) {
    if (-not $Process.Path) { return $false }
    $version = (Get-Item -LiteralPath $Process.Path).VersionInfo
    return ($version.FileMajorPart -eq 3 -and $version.FileMinorPart -eq 3 -and $version.FileBuildPart -eq 5)
}

function Get-GameWindow($Process) {
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) { return $Process.MainWindowHandle }
    } catch [System.InvalidOperationException] {
    }
    return [IntPtr]::Zero
}

# Same test as Windows' "not responding": the window must answer a no-op message in time.
# Flag 2 is SMTO_ABORTIFHUNG, which fails at once when Windows already marks the window hung.
function Test-Responsive([IntPtr]$Window, [int]$TimeoutMs) {
    $result = [UIntPtr]::Zero
    $answer = [ClientCrash.Native]::SendMessageTimeout($Window, 0, [UIntPtr]::Zero, [IntPtr]::Zero, 2,
        $TimeoutMs, [ref]$result)
    return ($answer -ne [IntPtr]::Zero)
}

function Write-Dump([int]$ProcessId, [string]$Type, [string]$Path) {
    # run from ProcDump's folder: started from the client folder it loads the client's 2005 dbghelp.dll
    # and every dump fails with 0x80070057
    Push-Location -LiteralPath (Split-Path -Parent $script:ProcDumpExe)
    try {
        # its output is UTF-16 and arrives with a NUL between the letters
        $output = & $script:ProcDumpExe -accepteula $Type $ProcessId $Path 2>&1 |
            ForEach-Object { "$_" -replace "`0", '' } | Where-Object { $_.Trim() }
    } finally {
        Pop-Location
    }
    $log = Join-Path (Split-Path -Parent $Path) 'procdump.log'
    Add-Content -LiteralPath $log -Value (@("> procdump $Type $ProcessId $Path") + @($output))
    # ProcDump exits with 1 even after writing the dump, so check the file instead
    return (Test-Path -LiteralPath $Path)
}

function Save-Freeze($Process, [IntPtr]$Window, [string]$ClientDir, [string]$Exe) {
    $detected = Get-Date
    $folder = Join-Path $ClientDir ('Errors\Hangs\{0:yyyy-MM-dd HH.mm.ss} {1}' -f $detected, $Exe)
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    Write-Status "freeze detected, dumping into $folder"

    $null = Write-Dump $Process.Id '-mm' (Join-Path $folder 'mini-00.dmp')
    if (-not (Write-Dump $Process.Id '-ma' (Join-Path $folder 'full.dmp'))) { Write-Status 'full dump failed' }
    Copy-Item -LiteralPath (Join-Path $ClientDir 'Logs') -Destination (Join-Path $folder 'Logs') -Recurse `
        -ErrorAction SilentlyContinue

    $taken = 0
    for ($i = 1; $i -le $FollowUps; $i++) {
        Start-Sleep -Seconds $FollowUpSeconds
        if ($Process.HasExited -or (Test-Responsive $Window 500)) { break }
        if (Write-Dump $Process.Id '-mm' (Join-Path $folder ('mini-{0:D2}.dmp' -f $i))) { $taken++ }
    }
    1..3 | ForEach-Object { [console]::Beep(880, 200) }
    Write-Status "dumps done ($taken follow-ups), safe to close the game"

    $utc = $detected.ToUniversalTime()
    $note = @(
        "exe: $Exe",
        "pid: $($Process.Id)",
        ('detected_utc: {0:yyyy-MM-ddTHH:mm:ssZ}' -f $utc),
        "freeze_seconds: $FreezeSeconds",
        "follow_ups: $taken"
    )
    Set-Content -LiteralPath (Join-Path $folder 'freeze.txt') -Value $note -Encoding ASCII
    return [pscustomobject]@{ Folder = $folder; WhenUtc = $utc; Detected = $detected }
}

function Wait-FreezeOutcome($Process, [IntPtr]$Window, $Freeze) {
    while ($true) {
        if ($Process.HasExited) { $outcome = 'game closed while frozen'; break }
        if (Test-Responsive $Window 500) {
            $seconds = ((Get-Date) - $Freeze.Detected).TotalSeconds + $FreezeSeconds
            $outcome = 'recovered after {0:N0} s' -f $seconds
            break
        }
        Start-Sleep -Seconds 1
    }
    Add-Content -LiteralPath (Join-Path $Freeze.Folder 'freeze.txt') -Value "outcome: $outcome" -Encoding ASCII
    Write-Status $outcome
}

function Save-ServerLog([string]$Folder, [datetime]$WhenUtc) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Status 'docker not found, no server log saved'
        return
    }
    $since = $WhenUtc.AddMinutes(-5).ToString('yyyy-MM-ddTHH:mm:ssZ')
    $until = $WhenUtc.AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ssZ')
    $stdout = [IO.Path]::GetTempFileName()
    $stderr = [IO.Path]::GetTempFileName()
    $run = Start-Process docker -ArgumentList 'logs', '--timestamps', '--since', $since, '--until', $until,
        $Container -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    if ($run.ExitCode -eq 0) {
        # the console log arrives split over stdout and stderr; the timestamps restore the order
        $colors = [string][char]27 + '\[[0-9;]*m'
        Get-Content -LiteralPath $stdout, $stderr -Encoding UTF8 | Sort-Object |
            ForEach-Object { $_ -replace $colors, '' } |
            Set-Content -LiteralPath (Join-Path $Folder 'worldserver.log') -Encoding UTF8
    } else {
        Write-Status ('docker logs failed: ' + (Get-Content -LiteralPath $stderr -TotalCount 1))
    }
    Remove-Item -LiteralPath $stdout, $stderr -Force
}

function Save-Traces([string]$Folder, [datetime]$WhenUtc) {
    if (-not (Test-Path -LiteralPath $script:TraceDir)) {
        Write-Status "trace folder $($script:TraceDir) not found, no traces saved"
        return
    }
    $from = $WhenUtc.AddMinutes(-15)
    $to = $WhenUtc.AddMinutes(1)
    $copied = 0
    foreach ($file in Get-ChildItem -LiteralPath $script:TraceDir -Filter '*.ndjson') {
        if ($file.Name -notmatch '^\d+_\d+_.+_(\d+)\.ndjson$') { continue }
        $start = [DateTimeOffset]::FromUnixTimeSeconds([long]$Matches[1]).UtcDateTime
        if ($start -gt $to -or $file.LastWriteTimeUtc -lt $from) { continue }
        $target = Join-Path $Folder 'traces'
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target
        $copied++
    }
    Write-Status "$copied trace(s) saved"
}

function Save-Evidence([string]$Folder, [datetime]$WhenUtc) {
    $wait = ($WhenUtc.AddMinutes(1) - (Get-Date).ToUniversalTime()).TotalSeconds
    if ($wait -gt 0) {
        Write-Status ('waiting {0:N0} s so the server log covers the minute after' -f $wait)
        Start-Sleep -Seconds ([math]::Ceiling($wait))
    }
    New-Item -ItemType Directory -Force -Path $Folder | Out-Null
    Save-ServerLog $Folder $WhenUtc
    Save-Traces $Folder $WhenUtc
}

function Remove-OldEvidence([string]$ClientDir) {
    $errors = Join-Path $ClientDir 'Errors'
    $old = @()
    $hangs = Join-Path $errors 'Hangs'
    if (Test-Path -LiteralPath $hangs) {
        # recovered stalls are pruned on their own, so a run of loading-screen stalls can't push out real freezes
        $folders = Get-ChildItem -LiteralPath $hangs -Directory | Sort-Object Name -Descending
        $recovered = @($folders | Where-Object {
                Select-String -LiteralPath (Join-Path $_.FullName 'freeze.txt') -Pattern '^outcome: recovered' -Quiet `
                    -ErrorAction SilentlyContinue
            })
        $real = @($folders | Where-Object { $recovered -notcontains $_ })
        $old += @($real | Select-Object -Skip $Keep) + @($recovered | Select-Object -Skip $Keep)
    }
    $old += @(Get-ChildItem -LiteralPath $errors -Directory -Filter '*.evidence' | Sort-Object Name -Descending |
            Select-Object -Skip $Keep)
    foreach ($folder in $old) {
        Remove-Item -LiteralPath $folder.FullName -Recurse -Force
        Write-Status "removed old $($folder.Name)"
    }
}

function Watch-Game($Process) {
    $exe = Split-Path -Leaf $Process.Path
    $clientDir = $ClientDir
    if (-not $clientDir) { $clientDir = Split-Path -Parent $Process.Path }
    $started = $Process.StartTime
    $freezes = New-Object System.Collections.ArrayList
    $armed = $false
    Write-Status "watching $exe (pid $($Process.Id))"

    while (-not $Process.HasExited) {
        Start-Sleep -Seconds 1
        $window = Get-GameWindow $Process
        if ($window -eq [IntPtr]::Zero) { continue }
        if (-not $armed) {
            # skip startup, when loading the data files can stall the window for a while
            if (((Get-Date) - $started).TotalSeconds -ge $ArmAfterSeconds -and (Test-Responsive $window 500)) {
                $armed = $true
                Write-Status 'armed'
            }
            continue
        }
        if (Test-Responsive $window ($FreezeSeconds * 1000)) { continue }
        if ($Process.HasExited) { break }
        $freeze = Save-Freeze $Process $window $clientDir $exe
        [void]$freezes.Add($freeze)
        Wait-FreezeOutcome $Process $window $freeze
    }
    Write-Status "$exe exited"

    $events = @($freezes | ForEach-Object { [pscustomobject]@{ Folder = $_.Folder; WhenUtc = $_.WhenUtc } })
    $reports = Get-ChildItem -LiteralPath (Join-Path $clientDir 'Errors') -Filter '*.txt' -ErrorAction SilentlyContinue
    $events += @($reports | Where-Object { $_.LastWriteTime -ge $started } |
            ForEach-Object {
                [pscustomobject]@{
                    Folder  = Join-Path $_.DirectoryName ($_.BaseName + '.evidence')
                    WhenUtc = $_.LastWriteTimeUtc
                }
            })
    foreach ($event in $events) {
        Write-Status "saving server evidence into $($event.Folder)"
        Save-Evidence $event.Folder $event.WhenUtc
    }
    Remove-OldEvidence $clientDir
}

function New-LaunchShortcut([string]$ExePath) {
    $clientDir = Split-Path -Parent $ExePath
    $link = Join-Path $clientDir 'WoW + freeze catcher.lnk'
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
    $shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
    $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Launch "{1}"' -f $PSCommandPath, $ExePath
    $shortcut.WorkingDirectory = $clientDir
    $shortcut.WindowStyle = 7
    $shortcut.IconLocation = "$ExePath,0"
    $shortcut.Description = 'Start WoW with the freeze catcher'
    $shortcut.Save()
    Write-Status "shortcut written: $link"
}

if (-not $Traces) { $Traces = Join-Path $PSScriptRoot '..\..\env\dist\logs\botobs' }
$script:TraceDir = [IO.Path]::GetFullPath($Traces)

if ($Launch) {
    $exePath = (Resolve-Path -LiteralPath $Launch -ErrorAction Stop).Path
    if ($CreateShortcut) {
        New-LaunchShortcut $exePath
        return
    }
}
$script:ProcDumpExe = Resolve-ProcDump $ProcDump
$Host.UI.RawUI.WindowTitle = 'WoW freeze catcher'

if ($Launch) {
    $game = Start-Process -FilePath $exePath -WorkingDirectory (Split-Path -Parent $exePath) -PassThru
    Watch-Game $game
    return
}
if ($ProcessId) {
    Watch-Game (Get-Process -Id $ProcessId -ErrorAction Stop)
    return
}

Write-Status "waiting for a 3.3.5 client matching '$ProcessName'"
while ($true) {
    $game = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object { Test-Client $_ } |
        Select-Object -First 1
    if ($game) {
        Watch-Game $game
        Write-Status "waiting for the next client"
    } else {
        Start-Sleep -Seconds 2
    }
}
