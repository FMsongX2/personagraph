@echo off
rem Windows launcher equivalent of ./am. PERSONAGRAPH_PYTHON overrides the interpreter.
setlocal
set "PYTHON=%PERSONAGRAPH_PYTHON%"
if not defined PYTHON (where py >nul 2>nul && set "PYTHON=py -3")
if not defined PYTHON set "PYTHON=python"
%PYTHON% "%~dp0scripts\alignment-memory.py" %*
exit /b %ERRORLEVEL%
