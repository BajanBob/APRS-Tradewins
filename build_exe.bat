@echo off
REM Build Tradewind.exe on your own Windows PC (needs Python 3.10+ from python.org)
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller || goto :fail
python tests\test_modem.py || goto :fail
python -m PyInstaller --noconfirm --onefile --name Tradewind --icon icon.ico --add-data "faceplate.html;." --collect-all sounddevice app.py || goto :fail
echo.
echo Done: dist\Tradewind.exe
pause
exit /b 0
:fail
echo.
echo Build failed - see messages above.
pause
exit /b 1
