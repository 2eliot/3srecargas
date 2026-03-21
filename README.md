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
- **Notificaciones por correo**: emails automáticos al crear, aprobar, completar o rechazar órdenes (incluye entrega de códigos/PINs por email)
- **Redes sociales configurables**: links del footer editables desde el panel admin

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
# ADMIN_USERNAME=tu-admin
# ADMIN_PASSWORD=tu-clave-segura
# ADMIN_EMAIL=admin@tudominio.com
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
- Credenciales iniciales: las definidas por `ADMIN_USERNAME` y `ADMIN_PASSWORD` en `.env`

### Secciones
| Sección | Descripción |
|---------|-------------|
| Dashboard | Estadísticas, órdenes recientes, alertas de stock bajo |
| Órdenes | Aprobar / rechazar, filtrar por estado |
| Juegos | Agregar/editar juegos con imagen, configurar IDs doble |
| Paquetes | Crear paquetes, marcar como automatizados |
| Stock PINs | Cargar PINs (uno por línea), FIFO automático |
| Afiliados | Crear afiliados, ver comisiones, marcar como pagadas |

## Servicio de Recarga Automática (Bot VPS)

Cuando se aprueba una orden de un paquete **Automatizado ⚡**, la web envía el PIN del stock + el Player ID al bot de scraping que corre en el VPS. El bot ejecuta Playwright + captcha y redime el PIN en la cuenta del jugador.

### Payload enviado al bot (`POST /redeem`)

```json
{
  "pin_key": "ABCD-EFGH-IJKL-MNOP",
  "player_id": "123456789",
  "full_name": "Usuario Recarga",
  "birth_date": "01/01/1995",
  "country": "Venezuela",
  "request_id": "ORD-ABC12345"
}
```

### Respuesta esperada del bot

```json
{
  "success": true,
  "message": "Recarga completada",
  "player_name": "NombreJugador"
}
```

Si `success` es `false`, el PIN **no** se marca como usado y la orden permanece pendiente para reintentar.

### Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `AUTOMATION_SERVICE_URL` | `http://localhost:8000` | URL base del bot de scraping |
| `VPS_REDEEM_URL` | `{AUTOMATION_SERVICE_URL}/redeem` | Endpoint completo de redención |
| `VPS_TIMEOUT` | `120` | Segundos de espera máxima por respuesta |
| `VPS_COUNTRY` | `Venezuela` | País para el formulario de redención |
| `VPS_FULL_NAME` | `Usuario Recarga` | Nombre completo para el formulario |
| `VPS_BIRTH_DATE` | `01/01/1995` | Fecha de nacimiento para el formulario |

### Correo electrónico (SMTP)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `MAIL_SERVER` | `smtp.gmail.com` | Servidor SMTP |
| `MAIL_PORT` | `587` | Puerto SMTP |
| `MAIL_USERNAME` | *(vacío)* | Usuario SMTP (correo) |
| `MAIL_PASSWORD` | *(vacío)* | Contraseña de aplicación SMTP |
| `MAIL_USE_TLS` | `true` | Usar STARTTLS |
| `MAIL_USE_SSL` | `false` | Usar SSL (fallback) |
| `MAIL_DEFAULT_SENDER` | `MAIL_USERNAME` | Remitente por defecto |
| `MAIL_BRAND_NAME` | `3S Recargas` | Nombre de marca en correos |
| `SUPPORT_EMAIL` | `soporte@3srecargas.com` | Correo de soporte |
| `SUPPORT_WHATSAPP` | *(vacío)* | Link WhatsApp soporte |
| `ADMIN_NOTIFY_EMAIL` | *(vacío)* | Correo para alertas de nuevas órdenes |

> **Gmail:** usa una [contraseña de aplicación](https://myaccount.google.com/apppasswords), no tu contraseña normal.

## Notificaciones por Correo

El sistema envía emails automáticos en los siguientes eventos:

| Evento | Destinatario | Contenido |
|--------|-------------|------------|
| Orden creada | Cliente + Admin | Resumen de la orden, estado pendiente |
| Orden aprobada | Cliente | Confirmación de aprobación |
| Orden completada | Cliente | Confirmación + código/PIN si aplica |
| Orden rechazada | Cliente | Notificación con motivo del rechazo |

Los valores de marca, soporte y contacto se configuran tanto en `.env` como en **Admin → Configuración** (los valores del admin tienen prioridad).

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
│   ├── utils/
│   │   ├── email.py         # SMTP send (TLS/SSL, async)
│   │   ├── email_templates.py # HTML email builders
│   │   └── notifications.py # High-level notification dispatcher
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
