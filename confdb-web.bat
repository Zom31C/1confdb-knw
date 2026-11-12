@echo off
rem Запуск веб-интерфейса confdb (браузер) через venv проекта
"%~dp0.venv\Scripts\python.exe" -m confdb.webui %*
