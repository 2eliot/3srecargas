@echo off
echo ===================================
echo DIAGNOSTICO DE LA APLICACION FLASK
echo ===================================
echo.

cd /d "%~dp0"

echo [1/5] Verificando Python...
python --version
if %errorlevel% neq 0 (
    echo ERROR: Python no esta instalado o no esta en el PATH
    pause
    exit /b 1
)
echo OK
echo.

echo [2/5] Verificando Flask...
python -c "import flask; print('Flask version:', flask.__version__)" 2>nul
if %errorlevel% neq 0 (
    echo Flask no esta instalado. Instalando dependencias...
    python -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo ERROR: No se pudieron instalar las dependencias
        pause
        exit /b 1
    )
) else (
    echo OK
)
echo.

echo [3/5] Verificando archivo .env...
if not exist .env (
    echo Creando .env desde .env.example...
    copy .env.example .env
)
echo OK
echo.

echo [4/5] Probando importacion de la app...
python -c "from app import create_app; app = create_app(); print('App creada exitosamente')"
if %errorlevel% neq 0 (
    echo ERROR: No se pudo crear la aplicacion
    echo.
    echo Detalles del error:
    python run.py
    pause
    exit /b 1
)
echo OK
echo.

echo [5/5] Iniciando servidor Flask...
echo El servidor se iniciara en http://localhost:5000
echo Presiona Ctrl+C para detener el servidor
echo.
python run.py
pause
