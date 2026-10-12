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
   gunicorn -w 3 -b 0.0.0.0:8001 --timeout 180 'app:create_app()'
   ```
   > `--timeout 180` es importante: las llamadas a Pabilo/Revendedores/VPS pueden tardar
   > hasta 120s cada una. Con el timeout por defecto de Gunicorn (30s), el worker podía
   > ser matado a mitad de una recarga — dejando la orden en un estado inconsistente sin
   > que la app llegara a registrar si la recarga se completó o no.

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
| Soporte | Bandeja en vivo: lista de chats a la izquierda y el chat abierto a la derecha |
| Asistente IA | Botones y saludo del chat del cliente + asistente de IA con sus instrucciones |

### Bandeja de soporte en vivo

`/admin/soporte` es una pantalla dividida: a la izquierda la bandeja (buscador, filtros por etiqueta, pestañas **En atención / En espera / Resueltos** con su contador) y a la derecha el chat abierto, que se carga dentro de un `iframe` sin recargar la página. En pantallas angostas se ve una cosa a la vez, con botón para volver.

- La bandeja se actualiza sola consultando `GET /admin/soporte/bandeja.json` cada 4 s (cada 20 s si la pestaña está en segundo plano). Devuelve un `version` (hash); si no cambió responde `{"unchanged": true}` y no se repinta nada. También actualiza el contador del menú y el título de la pestaña.
- El chat abierto ya tenía su propio refresco de mensajes; dentro de la bandeja se abre con `?embed=1` (o el encabezado `Sec-Fetch-Dest: iframe`), que oculta el menú del admin.
- Es *polling*, no websockets: detrás de nginx + gunicorn funciona sin configuración extra. Si nginx manda `X-Frame-Options: DENY`, el panel derecho quedaría en blanco; debe ser `SAMEORIGIN` (o no enviarse).

### Chat de soporte del cliente: menú de botones y asistente de IA

`/soporte` es un chat normal: el hilo se crea en el servidor con el primer mensaje (o el primer adjunto), así que abrirlo sin escribir nada no deja chats vacíos. Se configura en **Admin → Asistente IA** (`/admin/soporte/asistente`).

**Menú de botones** (no usa IA; `app/utils/support_bot.py`, `static/js/support-bot.js`): *Estado de mi recarga* y *Comprar recarga* (los precios se ven al elegir el juego). Cada uno se puede apagar, y el nombre del asistente y el saludo son editables.
- Los precios salen de la misma función que la tienda (`/api/packages/<id>`), así que nunca difieren de los de la página; lo agotado o cerrado por horario sale apagado.
- **Comprar** lleva la compra COMPLETA por el chat (`static/js/support-buy.js`): ID (con apodo donde la tienda puede verificarlo; correo en wallet y tarjetas) → método de pago (pregunta solo si hay 2 o más) → código de descuento (opcional) → datos para pagar con botón *Copiar* → foto del comprobante (se lee la referencia y el cliente la confirma) → orden → seguimiento en vivo. No hay lógica de compra propia: usa las MISMAS rutas de la tienda (`POST /checkout/<id>` para iniciar y confirmar, `GET ?format=json` para cotizar, `/order/<n>/auto-verify` y `/complete-payment`), así que la orden es una orden normal: sale en el admin, da puntos a ese ID, avisa por la campanita/correo/push, respeta descuentos, pagos incompletos, métodos que piden cédula y teléfono, etc. Para eso el "confirmar" del checkout acepta `ajax=1` y responde en JSON (`ok`, `order_number`, `message`, `code`); sin `ajax` la tienda se comporta exactamente como siempre. Si algo falla, el chat ofrece *Terminar en la tienda* con el enlace `/?juego=<slug>&paquete=<id>&jugador=<id>&zona=<z>` (la tienda valida ese enlace).
- **Estado** busca por número de orden (12 dígitos) o referencia del pago (últimos 6+ dígitos), o lee la **foto del comprobante** con el lector de comprobantes de la tienda (Gemini, `GEMINI_API_KEY`; la foto se lee en memoria y no se guarda). Nunca sirve el ID de jugador (es público) y la tarjeta no muestra ID, correo, teléfono ni referencia. Antes de pedir datos invita a revisar las notificaciones (la campanita), que ya traen el estado del último pedido.
- Límites por IP: consultas del menú 60/min; estado 8/min y 40/h; fotos 5/min y 20/h. Las consultas no crean chats.

**Asistente de IA** (`app/utils/support_ai.py`): solo resuelve preguntas, no vende ni toma pedidos. Por defecto usa **Gemini con la misma `GEMINI_API_KEY` que lee los comprobantes** (no se escribe ninguna llave en el panel; si Google retira un modelo, prueba el siguiente de la lista). También puede usar DeepSeek o una IA propia compatible (Ollama), cada una con su propia llave guardada aparte: la llave de un proveedor nunca se envía a otro. Sabe en vivo juegos, paquetes y precios, métodos de pago y el estado de un pedido, y sigue las *Instrucciones para la IA* que escribe el admin. Si no puede resolver, pasa el chat a una persona con la etiqueta elegida.

**Comprobantes en el chat** (`app/utils/receipt_check.py`): cuando el cliente adjunta la foto de un comprobante, el servidor (no el modelo) la lee con Gemini (`extract_receipt_details_from_image_*` en `reference_extraction.py`: referencia, monto y a quién se pagó) y comprueba dos cosas: (1) si esa referencia ya está en una orden, con la misma regla de la tienda para "es la misma operación" (`same_bank_reference`: iguales, o una es el final de la otra) y sin límite de fecha; (2) si el pago fue a **nuestros datos**: teléfono, cédula/RIF y correo se comparan con los métodos de pago activos y con lo que el admin escribe en *Instrucciones para la IA* (cualquier teléfono, cédula, correo o cuenta que ponga ahí cuenta como nuestro). Veredicto `ours` / `not_ours` / `unknown` (datos que se contradicen, destinatario ilegible o cuenta no anotada → lo ve una persona; solo teléfono, cédula y correo pueden acusar). Si el pago NO está en el sistema y SÍ es a nuestros datos, el servidor adjunta al mensaje de la IA el botón **Crear mi orden con este comprobante** (`action_url` = `#comprar:<id del mensaje con la foto>`), y la compra sigue en el chat con ese comprobante, sin mostrar datos para pagar ni pedir la foto de nuevo (`SupportBuy.startWithReceipt`); la orden pasa por el checkout normal, que vuelve a verificar el pago en el banco. Si ya está en el sistema, la IA da el estado; si es a otros datos o no se pudo comprobar, no ofrece nada y lo pasa a una persona. Al modelo solo le llegan los veredictos y números ya validados, nunca texto libre de la imagen. El resultado se guarda en `SupportMessage.ai_note` (JSON versionado); el botón solo se ofrece con el menú de botones activo y por 90 minutos desde que se envió la foto, una sola vez por comprobante.

## Campana de notificaciones

En el header de la tienda, junto al menú, hay una campana con el número de avisos nuevos. Junta tres cosas, siempre con los más nuevos primero:

- **Novedades del admin**: se escriben en **Admin → Notificaciones** (título, mensaje y link opcional). Las ve todo el que entre a la web; se pueden ocultar o eliminar, y opcionalmente salir también como push.
- **Estado de los pedidos de ese navegador**: se calcula en el momento, uno por orden (verificando el pago, en proceso, pago incompleto con lo que falta, lista o rechazada). El navegador solo sigue las órdenes que se hicieron en él: la página de la orden las registra en `localStorage` únicamente si la compra se hizo en ese navegador, así que alguien que conozca el correo o el ID de otro no ve sus avisos.
- **Mensajes a una orden**: en el detalle de cada orden en el admin ("Notificación para el cliente"). Salen como "Sobre tu orden #…" en la campana de quien hizo esa compra (y por push si el cliente lo activó).

La campana consulta `POST /api/notificaciones` (solo lectura) al cargar la página y cada minuto. Todo el texto se pinta como texto, nunca como HTML, y los links solo pueden ser rutas de la tienda o `https://`.

## Servicio de Recarga Automática (Bot VPS)

Cuando se aprueba una orden de un paquete **Automatizado ⚡**, la web envía el PIN del stock + el Player ID al bot de scraping que corre en el VPS. El bot ejecuta Playwright + captcha y redime el PIN en la cuenta del jugador.

### Qué vía usa cada paquete

El interruptor **"Automatizado ⚡ (usa stock de PINs)"** de cada paquete es el que decide, y tiene prioridad sobre el mapeo de Revendedores:

| Paquete | Stock de PINs | Mapeo Revendedores | Vía usada |
|---|---|---|---|
| Automatizado ⚡ | Sí | Sí o no | **Bot VPS** (stock propio) |
| Automatizado ⚡ | No | Sí | Revendedores (respaldo) |
| Automatizado ⚡ | No | No | Error: "Sin stock de códigos" |
| Normal | — | Sí | Revendedores |
| Normal, categoría `tarjetas` | Sí | — | Entrega directa del PIN, sin bot |

Es decir: marcar el paquete como automatizado y cargarle PINs es suficiente para que las recargas pasen a hacerse con tu propio stock. Si el stock se agota y el paquete tiene mapeo, las órdenes siguen saliendo por Revendedores en vez de quedarse trabadas.

### Proveedores de recarga (Revendedores y Central One)

Un paquete con mapeo se recarga por API con **uno de dos proveedores**. En **Admin → Juegos → Mapeo de proveedores** se elige, por paquete:

- **Proveedor principal**: quien entrega ese paquete (con su propio catálogo de juegos y paquetes y la cantidad de recargas).
- **Proveedor de respaldo** (opcional): el otro proveedor. Si el principal **confirma que no entregó nada** (sin saldo, sin stock, producto dado de baja, llave inválida o pedido fallido), la orden pasa sola al respaldo y queda anotado en las notas (`[Respaldo]`) y en el detalle de la orden.

Reglas que evitan recargar dos veces (cada recarga repetida es dinero perdido):

- Nunca se pasa al respaldo si hay duda de que el principal ya recargó (timeout, error de red, "procesando" sin confirmar).
- Nunca se pasa al respaldo si el principal ya hizo alguna recarga de un mapeo de varios pasos.
- Un ID de jugador inválido tampoco pasa al respaldo: lo rechazaría igual.
- Una vez que el respaldo toma la orden, **no vuelve** al proveedor anterior.
- Central One recibe una llave de idempotencia por intento: si la conexión se cae, el reintento usa la misma llave y devuelve el pedido original en vez de crear otro.

Configuración de Central One en el `.env` del servidor (la llave nunca va al navegador): `CENTRALONE_API_KEY` y, si hace falta, `CENTRALONE_BASE_URL`. Permisos que debe tener la llave: `catalog:read`, `balance:read`, `orders:read` y `orders:write`, y **`codes:read`** (es el único que no viene marcado al crear la llave) para los productos que entregan un código. Luego, en el mapeo, **Sincronizar Central One** trae su catálogo. Central One no tiene modo de prueba: cada pedido mueve saldo real.

**Productos de código (gift cards / PINs).** Central One entrega el código aparte (`GET /orders/{id}/codes`). La tienda lo trae y lo guarda en la orden (`pin_delivered`, uno por línea si son varios), así que en la **sección de códigos** (categoría `tarjetas`) el cliente lo ve en su orden y le llega por correo, igual que con tus PINs propios. La orden no se completa hasta tener el código. Si la llave no tiene `codes:read`, la orden espera y avisa (no se pasa al respaldo: Central One sí entregó). Si el paquete NO es de la sección de códigos, el código queda en el detalle de la orden para el admin y la orden queda pendiente hasta que la apruebe. Para una orden que Central One ya entregó sin que llegara el código, el detalle de la orden tiene el botón **Traer código de Central One**.

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

El bot debe responder **JSON con `success` explícito**. Una respuesta HTTP 200 cuyo cuerpo no sea JSON válido se trata como fallo, no como éxito: si `VPS_REDEEM_URL` apunta por error a otro servicio (o a un proxy que devuelve HTML), la orden queda pendiente en vez de darse por entregada sin haber recargado nada.

### Manejo del stock de PINs ante fallos

El PIN se **reserva** (`pins.order_id`) antes de llamar al bot, de modo que dos órdenes del mismo paquete nunca reciben el mismo código. Qué pasa después depende de la respuesta:

| Respuesta del bot | Qué se hace con el PIN |
|---|---|
| `success: true` | Se consume y la orden se completa |
| `success: false` (rechazo limpio) | Vuelve al stock |
| `success: false` + `manual_review: true` | **Ambiguo** — se consulta `/redeem/verify` |
| Timeout | **Ambiguo** — se consulta `/redeem/verify` |
| No se pudo conectar | Vuelve al stock (nunca llegó al bot) |

`manual_review: true` significa que la validación ya se envió al proveedor y el PIN **pudo consumirse igual**. En ese caso se consulta `POST /redeem/verify`, que responde si el PIN ya fue canjeado *sin* canjearlo:

- `status: 'used'` → la recarga sí ocurrió: se consume el PIN y la orden se completa.
- `status: 'available'` → el PIN sigue intacto: vuelve al stock.
- `status: 'unknown'` (o el bot no responde) → el PIN queda **reservado** a esa orden, nunca de vuelta al stock, para que no se le entregue un código quemado a otro cliente.

> Comprueba el bot con `curl -s http://127.0.0.1:8000/health` → `{"status":"ok","browser_ready":true,...}`.
> Un `GET` a `/redeem` devuelve 404 aunque el bot esté sano: esas rutas solo aceptan `POST`.

### Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `AUTOMATION_SERVICE_URL` | `http://localhost:8000` | URL base del bot de scraping |
| `VPS_REDEEM_URL` | `{AUTOMATION_SERVICE_URL}/redeem` | Endpoint completo de redención |
| `VPS_TIMEOUT` | `120` | Segundos de espera máxima por respuesta |
| `VPS_COUNTRY` | `Venezuela` | País para el formulario de redención |
| `VPS_FULL_NAME` | `Usuario Recarga` | Nombre completo para el formulario |
| `VPS_BIRTH_DATE` | `01/01/1995` | Fecha de nacimiento para el formulario |
| `GEMINI_API_KEY` | *(vacío)* | API key para extraer referencias desde comprobantes con Gemini |
| `GEMINI_REFERENCE_TIMEOUT` | `25` | Segundos máximos para la extracción IA |

### Binance Pay (verificación automática)

El cliente recibe un código de 6 dígitos y lo escribe en la Nota del envío. **Binance no siempre propaga esa nota al historial del receptor**: en producción una parte de las transferencias entrantes reales llega con `note` vacío aunque el cliente sí lo escribió. Por eso el emparejamiento tiene dos vías:

1. **Código en el memo** — vía normal.
2. **Monto + ventana de tiempo** — respaldo para memos vacíos. Solo se acredita si no hay ambigüedad: si otra orden pendiente espera el mismo monto, o si hay más de un pago candidato, la orden se deja con una nota para revisión manual en vez de arriesgar acreditarla mal.

Cada pago acreditado queda en `orders.binance_tx_id` (índice único), así que una misma transferencia nunca puede saldar dos órdenes.

La verificación corre en un hilo por orden **y** en el scheduler de recuperación, de modo que sobrevive a un reinicio de worker o a un despliegue.

| Variable | Default | Descripción |
|----------|---------|-------------|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | *(vacío)* | Credenciales con permiso de historial de Pay |
| `BINANCE_PROXY` | *(vacío)* | Proxy de salida para las llamadas a Binance |
| `BINANCE_REQUEST_TIMEOUT_SECONDS` | `15` | Timeout por endpoint |
| `BINANCE_LOOKBACK_MINUTES` | `90` | Minutos hacia atrás que se buscan pagos (el cliente suele pagar **antes** de confirmar la orden) |
| `BINANCE_VERIFY_WINDOW_MINUTES` | `180` | Cuánto se sigue buscando el pago de una orden pendiente |
| `BINANCE_AMOUNT_TOLERANCE` | `0.02` | Margen bajo el monto esperado; el sobrepago siempre se acepta |
| `BINANCE_MATCH_BY_AMOUNT` | `true` | Activa el respaldo por monto para memos vacíos |

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

Todo lo que escribe un cliente (ID, apodo, referencia, nombre y mensajes del chat...) se **escapa como HTML** antes de entrar a un correo (`_detail_row` en `email_templates.py` y los correos de soporte en `notifications.py`): sin eso un cliente podía meter links o etiquetas en el correo que la tienda manda al admin o a la dirección de otra persona. Si agregas una plantilla de correo nueva, escapa con `html.escape` todo dato que no sea tuyo. Un pedido con varios códigos (Central One) los muestra uno por línea.

## Seguridad: qué está cubierto

- **Texto del usuario en pantalla:** el chat, la campana y las tarjetas del menú pintan con `textContent` (nunca `innerHTML`); las plantillas usan el autoescape de Jinja y los datos que van a `<script>` pasan por `|tojson`. Los avisos de la campana solo aceptan links `/ruta` o `https://` (nunca `javascript:`), y el navegador lo vuelve a comprobar. Los adjuntos del chat solo admiten imágenes y video por extensión (no `.svg` ni `.html`) y el nombre se limpia con `secure_filename`.
- **Una orden por pago:** el confirmar del checkout toma un candado por referencia (`checkout_ref:<referencia>`, tabla `process_locks`) mientras revisa "referencia repetida" y guarda la orden. Sin él, tres envíos del mismo pago a la vez creaban tres órdenes. La verificación automática sigue bloqueando cualquier orden que tenga otra con su misma referencia.
- **Topes anti-abuso** (los cuentan `check_rate_limit` y `process_locks`, por IP): campanita 30/min si manda órdenes, fotos del chat 20/h, lectura de comprobantes del menú 5/min y 20/h, estado de recarga 8/min y 40/h, chats nuevos 3/h, 10 mensajes/min por chat y 30 respuestas de IA por chat y hora (pasado el tope atiende una persona). Todos usan `X-Forwarded-For`: **nginx debe sobrescribirlo** (`proxy_set_header X-Forwarded-For $remote_addr;`), si no un visitante puede inventar su IP y saltarse los topes.
- **Panel admin:** todas las rutas `/admin/...` rechazan a un visitante y a un cliente con sesión de cliente (hay un guardia por blueprint; `admin_support_bp` se apoya además en `@login_required` en cada ruta).
- Las páginas y respuestas JSON llevan `X-Content-Type-Options: nosniff`.

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
│   │   ├── notifications.py # High-level notification dispatcher
│   │   ├── locks.py         # Lock distribuido en BD (reemplaza threading.Lock entre workers)
│   │   └── order_scheduler.py # Hilo de recuperación en 2do plano (pagos/recargas atascadas)
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
