# Установка эмулятора терминала «Электроника 15ИЭ-00-013» (Windows):
# создаёт ярлык на рабочем столе, открывающий диалог с БЭСМ-6
# в окне консоли. Конфигурация подключения — ie15.conf.
#
# Запуск:  powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cmd = Join-Path $dir "ie15.cmd"
if (-not (Test-Path $cmd)) {
    Write-Error "Не найден $cmd — распакуйте проект целиком."
}

# Python обязателен (3.10+; для ssh:// нужен ещё paramiko)
$py = $null
foreach ($c in @("py", "python")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) {
    Write-Warning "Python не найден — установите Python 3.10+, затем запустите скрипт снова."
} else {
    & $py -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
    if ($LASTEXITCODE -ne 0) { Write-Warning "Нужен Python 3.10 или новее." }
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "Электроника 15ИЭ (терминал).lnk"
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $cmd
$lnk.WorkingDirectory = $dir
$lnk.IconLocation = "$env:SystemRoot\System32\cmd.exe,0"
$lnk.Description = "Диалог с БЭСМ-6 по SSH или telnet-линии (ie15.conf)"
$lnk.Save()
Write-Output "Ярлык создан: $lnkPath"
