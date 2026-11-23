# Обработчик yarophoto:// — вызывается Windows при клике на кнопку в index.html.
# Аргумент: yarophoto://start | yarophoto://stop | yarophoto://open
# Только фиксированные действия, белый список ниже. Произвольные команды не выполняются.
param(
    [Parameter(Position = 0)]
    [string]$Url
)

Add-Type -AssemblyName System.Windows.Forms

function Fail([string]$Msg) {
    [System.Windows.Forms.MessageBox]::Show($Msg, 'yarophoto', 'OK', 'Error') | Out-Null
    exit 1
}

if (-not $Url) { Fail 'Пустой URL.' }
try { $u = [uri]$Url } catch { Fail "Не разобрать URL: $Url" }
if ($u.Scheme -ne 'yarophoto') { Fail "Неизвестная схема: $($u.Scheme)" }

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
        if (-not (Test-Path -LiteralPath $bat)) { Fail "start.bat не найден:`n$bat" }
        Start-Process -FilePath 'cmd.exe' -ArgumentList @('/k', "`"$bat`"") -WorkingDirectory $root
        exit 0
    }
    'stop' {
        if (-not (Test-ServerAlive)) {
            [System.Windows.Forms.MessageBox]::Show('Сервер и так не отвечает.', 'yarophoto', 'OK', 'Information') | Out-Null
            exit 0
        }
        try {
            Invoke-WebRequest -Uri "$panel/api/shutdown" -UseBasicParsing -Method Post -TimeoutSec 5 | Out-Null
            [System.Windows.Forms.MessageBox]::Show('Сервер остановлен.', 'yarophoto', 'OK', 'Information') | Out-Null
        } catch {
            Fail "Не удалось остановить сервер:`n$_"
        }
        exit 0
    }
    'open' {
        Start-Process $panel
        exit 0
    }
    default {
        Fail "Неизвестное действие: $($u.Host)`nДопустимые: start, stop, open"
    }
}
