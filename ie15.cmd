@echo off
rem Эмулятор терминала «Электроника 15ИЭ-00-013» — интерактивный сеанс.
rem Конфигурация: ie15.conf рядом с этим файлом; аргументы — как у ./ie15.
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
rem без явной --line подключение берётся из ie15.conf (по умолчанию
rem ssh://test@besm6.cs.msu.ru, ключ ~/.ssh/besm_test, линии 4202-4223 —
rem вторая копия терминала сама возьмёт следующую свободную линию)
%PY% -m ie15emu --seconds 0 %*
if errorlevel 1 pause
