@echo off
echo Building Deadlite Worker...
call .\venv\Scripts\python.exe -m PyInstaller --onefile --noconfirm worker\client.py -n deadlite_worker

echo.
echo Building Deadlite Manager...
call .\venv\Scripts\python.exe -m PyInstaller --onefile --noconfirm --add-data "manager/templates;templates" manager\app.py -n deadlite_manager

echo.
echo Build complete! Executables are located in the "dist" folder.
pause
