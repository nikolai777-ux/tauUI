@echo off
rem Start tauUI with the project's own Python environment (.venv).
rem Pass a graph file to open it directly:  run.bat graphs\my_graph.tau
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py %*
