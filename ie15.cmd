@echo off
rem Эмулятор терминала «Электроника 15ИЭ-00-013» — интерактивный сеанс.
rem Конфигурация: ie15.conf рядом с этим файлом; аргументы — как у ./ie15.
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
rem по умолчанию — перебор ДКС-линий (4202..4223): вторая копия
rem терминала сама возьмёт следующую свободную линию
set "EXTRA="
echo "%*" | findstr "--line --port --script --help" >nul || set "EXTRA=--line tcp://127.0.0.1:4202-4223"
%PY% -m ie15emu --seconds 0 %EXTRA% %*
if errorlevel 1 pause
