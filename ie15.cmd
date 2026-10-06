@echo off
rem Эмулятор терминала «Электроника 15ИЭ-00-013» — интерактивный сеанс.
rem Конфигурация: ie15.conf рядом с этим файлом; аргументы — как у ./ie15.
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
%PY% -m ie15emu --seconds 0 %*
if errorlevel 1 pause
