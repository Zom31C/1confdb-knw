@echo off
rem Извлечение конфигурации с пропуском сбойных объектов (--skip-errors):
rem упавший объект пропускается с печатью причины, разбор идёт до конца.
rem Использование: extract-skip.bat файл.cf --db out.db [--dump DIR] [--workers N]
"%~dp0.venv\Scripts\python.exe" -m confdb extract --skip-errors %*
