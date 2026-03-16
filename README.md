# 3S Recargas — E-commerce de Recargas

Plataforma web de recargas móviles y de juegos construida con **Flask + PostgreSQL**.

## Características

- **Mosaico 3×3** responsive con expansión de paquetes al hacer clic
- **3 Categorías**: Juegos, Tarjetas, Wallet
- **Recarga Automática**: integración con microservicio local (puerto 8000)
- **Stock de PINs (FIFO)**: carga masiva por textarea, entrega automática al aprobar
- **Validación de referencia duplicada**: previene doble-pago
- **Sistema de Afiliados**: links únicos, comisiones automáticas, panel de gestión
- **Panel Admin**: gestión completa de juegos, paquetes, órdenes y afiliados
- **Mobile-First**: diseño oscuro, sticky header, CSS Grid

## Instalación

### 1. Clonar y configurar

```bash
git clone <repo>
cd windsurf-project
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. Base de datos PostgreSQL

```sql
CREATE DATABASE 3srecargas_db;
```

### 3. Variables de entorno

```bash
copy .env.example .env
# Editar .env con tus valores:
# DATABASE_URL=postgresql://user:password@localhost/3srecargas_db
# SECRET_KEY=tu-clave-secreta
# ADMIN_USERNAME=admin
# ADMIN_PASSWORD=tu-password
```

### 4. Ejecutar

```bash
python run.py
```

Abre `http://localhost:5000`

## Despliegue rápido (VPS)

1. **Crear carpeta del proyecto**
   ```bash
   mkdir -p /var/www/3srecargas && cd /var/www/3srecargas
   git clone https://github.com/2eliot/3srecargas.git .
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Configurar `.env`** (mismo formato que en local). Para usar SQLite aislado por sitio:
   ```env
   DATA_DIR=/var/www/3srecargas/data
   DATABASE_URL=sqlite:///${DATA_DIR}/app.db
   ```
   > Cada proyecto puede apuntar a su propia carpeta `data/` para evitar conflicto con otras webs.
3. **Migrar datos iniciales**
   ```bash
   mkdir -p $DATA_DIR
   flask shell -c "from app import create_app; app=create_app(); ctx=app.app_context(); ctx.push(); from app.models import db; db.create_all(); ctx.pop()"
   ```
4. **Servicio (ej. Gunicorn + systemd)**
   ```bash
   gunicorn -w 3 -b 0.0.0.0:8001 'app:create_app()'
   ```
   Configura Nginx como proxy → `https://tudominio.com` → Gunicorn.

> Si usas PostgreSQL en producción, sustituye `DATABASE_URL` por la cadena correspondiente y omite `DATA_DIR`.

## Panel Admin

- URL: `http://localhost:5000/admin`
- Credenciales iniciales: `admin` / `admin123` *(cambiar en `.env`)*

### Secciones
| Sección | Descripción |
|---------|-------------|
| Dashboard | Estadísticas, órdenes recientes, alertas de stock bajo |
| Órdenes | Aprobar / rechazar, filtrar por estado |
| Juegos | Agregar/editar juegos con imagen, configurar IDs doble |
| Paquetes | Crear paquetes, marcar como automatizados |
| Stock PINs | Cargar PINs (uno por línea), FIFO automático |
| Afiliados | Crear afiliados, ver comisiones, marcar como pagadas |

## Servicio de Automatización

El paquete marcado como **Automatizado ⚡** llama a `http://localhost:8000` con:

```json
{
  "order_number": "ABC12345",
  "player_id": "123456789",
  "zone_id": "2001",
  "pin": "PIN-CODE-HERE",
  "package": "100 Diamantes",
  "game": "Free Fire"
}
```

Configura la URL en `.env`: `AUTOMATION_SERVICE_URL=http://localhost:8000`

## Links de Afiliado

`https://tudominio.com/r/CÓDIGO` → almacena el código en sesión y redirige a la tienda.

## Estructura del Proyecto

```
windsurf-project/
├── app/
│   ├── __init__.py          # App factory + init DB
│   ├── models.py            # SQLAlchemy models
│   ├── routes/
│   │   ├── main.py          # Tienda + API endpoints
│   │   ├── checkout.py      # Flujo de compra
│   │   ├── admin.py         # Panel administración
│   │   └── affiliates.py    # Redirección de afiliados
│   ├── static/
│   │   ├── css/main.css     # Estilos tienda (dark theme)
│   │   ├── css/admin.css    # Estilos panel admin
│   │   ├── js/main.js       # Mosaico, AJAX, categorías
│   │   └── uploads/         # Imágenes de juegos y paquetes
│   └── templates/
│       ├── base.html / index.html / checkout.html / order_status.html
│       └── admin/           # Todas las vistas del panel
├── config.py
├── run.py
├── requirements.txt
└── .env.example
```
