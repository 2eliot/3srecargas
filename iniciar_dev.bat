@echo off
echo ===================================================
echo INICIO DEL SERVIDOR DE DESARROLLO (SQLite)
echo ===================================================
echo.

cd /d "%~dp0"

echo [1/3] Instalando dependencias (sin PostgreSQL)...
python -m pip install --upgrade pip
python -m pip install -r requirements_dev.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: No se pudieron instalar las dependencias
    pause
    exit /b 1
)
echo.
echo Dependencias instaladas correctamente!
echo.

echo [2/3] Base de datos configurada: SQLite (dev.db)
echo.

echo [3/3] Iniciando servidor Flask...
echo.
echo ============================================
echo El servidor se iniciara en:
echo   http://localhost:5000
echo   http://127.0.0.1:5000
echo.
echo Base de datos: SQLite (dev.db)
echo Admin inicial: definido por ADMIN_USERNAME y ADMIN_PASSWORD en .env
echo.
echo Presiona Ctrl+C para detener el servidor
echo ============================================
echo.
python run.py
