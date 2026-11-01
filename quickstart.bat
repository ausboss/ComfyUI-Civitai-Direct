@echo off
rem Civitai Direct quick start for Windows. Double-click it, or run it from a terminal.
rem Uses the Python that ships with portable and Easy-Install ComfyUI when it can find one.
setlocal
set "HERE=%~dp0"
set "PY="
for %%P in ("%HERE%..\..\..\python_embeded\python.exe" "%HERE%..\..\python_embeded\python.exe" "%HERE%..\..\.venv\Scripts\python.exe") do (
    if not defined PY if exist "%%~P" set "PY=%%~P"
)
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python was not found. Install it from https://www.python.org/ or run this from your ComfyUI folder.
    pause
    exit /b 1
)
%PY% "%HERE%quickstart.py" %*
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%
