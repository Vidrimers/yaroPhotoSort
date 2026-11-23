# yarophoto:// handler - called by Windows when the page links to yarophoto://...
# URL: yarophoto://start | yarophoto://stop | yarophoto://open
# Fixed actions only, whitelist below. No arbitrary commands are executed.
param(
    [Parameter(Position = 0)]
    [string]$Url
)

Add-Type -AssemblyName System.Windows.Forms

function Fail([string]$Msg) {
    [System.Windows.Forms.MessageBox]::Show($Msg, 'yarophoto', 'OK', 'Error') | Out-Null
    exit 1
}

if (-not $Url) { Fail 'Empty URL.' }
try { $u = [uri]$Url } catch { Fail "Cannot parse URL: $Url" }
if ($u.Scheme -ne 'yarophoto') { Fail "Unknown scheme: $($u.Scheme)" }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 8460
$panel = "http://127.0.0.1:$port"

function Test-ServerAlive {
    try {
        $r = Invoke-WebRequest -Uri "$panel/api/ping" -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

switch ($u.Host) {
    'start' {
        if (Test-ServerAlive) {
            Start-Process $panel
            exit 0
        }
        $bat = Join-Path $root 'start.bat'
        if (-not (Test-Path -LiteralPath $bat)) { Fail "start.bat not found:`n$bat" }
        Start-Process -FilePath 'cmd.exe' -ArgumentList @('/k', "`"$bat`"") -WorkingDirectory $root
        exit 0
    }
    'stop' {
        if (-not (Test-ServerAlive)) {
            [System.Windows.Forms.MessageBox]::Show('Server is not running.', 'yarophoto', 'OK', 'Information') | Out-Null
            exit 0
        }
        try {
            Invoke-WebRequest -Uri "$panel/api/shutdown" -UseBasicParsing -Method Post -TimeoutSec 5 | Out-Null
            [System.Windows.Forms.MessageBox]::Show('Server stopped.', 'yarophoto', 'OK', 'Information') | Out-Null
        } catch {
            Fail "Could not stop the server:`n$_"
        }
        exit 0
    }
    'open' {
        Start-Process $panel
        exit 0
    }
    default {
        Fail "Unknown action: $($u.Host)`nAllowed: start, stop, open"
    }
}
