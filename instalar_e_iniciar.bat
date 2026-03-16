@echo off
echo ===================================================
echo INSTALACION DE DEPENDENCIAS E INICIO DEL SERVIDOR
echo ===================================================
echo.

cd /d "%~dp0"

echo [1/3] Instalando dependencias...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: No se pudieron instalar las dependencias
    echo Verifica que tengas conexion a internet y que Python este correctamente instalado
    pause
    exit /b 1
)
echo.
echo Dependencias instaladas correctamente!
echo.

echo [2/3] Verificando archivo .env...
if not exist .env (
    echo Creando .env desde .env.example...
    copy .env.example .env
)
echo OK
echo.

echo [3/3] Iniciando servidor Flask...
echo.
echo ============================================
echo El servidor se iniciara en:
echo   http://localhost:5000
echo   http://127.0.0.1:5000
echo.
echo Presiona Ctrl+C para detener el servidor
echo ============================================
echo.
python run.py
pause
