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
   gunicorn -w 3 --worker-class gthread --threads 8 -b 127.0.0.1:8001 --timeout 180 'app:create_app()'
   ```
   > **Auditoría 2026-10-14.** `--worker-class gthread --threads 8`: cada worker atiende 8 peticiones a la vez. Con el modo normal (`sync`),
   > 3 personas esperando una llamada larga (verificar un ID nuevo tarda hasta 45 s, Pabilo 30 s, el bot hasta 120 s) ocupaban los 3 workers
   > y la web dejaba de responder a todos. `-b 127.0.0.1:8001` (en vez de `0.0.0.0`): gunicorn solo debe hablar con Nginx; si queda abierto al
   > mundo, cualquiera lo puede llamar directo. (Aunque quede abierto, la app ya ignora `X-Forwarded-For` de conexiones que no vengan de la
   > misma máquina o de una red privada; si tu Nginx está en OTRA máquina con IP pública, pon su IP en `TRUSTED_PROXY_IPS`.)
   > Antes de cambiar el comando en producción, prueba en el VPS que todo siga andando.
   >
   > **Nginx (recomendado, no se pudo comprobar desde aquí):** la app ya comprime con gzip lo suyo (`APP_GZIP=0` lo apaga) y manda caché larga de
   > los estáticos con `?v=` fuera del modo desarrollo; si Nginx ya lo hace, no pasa nada (no recomprime). Conviene además `http2` en el `listen`,
   > `client_max_body_size 20m;` (por defecto es 1 MB y rompe subir el comprobante o las fotos de perfil) y que `proxy_set_header X-Forwarded-For
   > $remote_addr;` SOBRESCRIBA la cabecera (no `$proxy_add_x_forwarded_for`) y `TRUSTED_PROXY_COUNT=1` (2 si hay Cloudflare delante).
   > Si el administrador queda bloqueado por claves equivocadas de otra persona, el navegador que ya entró antes se reconoce solo; para liberar
   > a otro: `DELETE FROM api_rate_limit_hits WHERE bucket_key = 'admin_password_failures';`.
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
| Stock PINs | Cargar PINs (uno por línea), FIFO automático. El inventario sale **de 25 en 25** con paginador (`PINS_PAGE_SIZE` en `admin.py`; el orden es estable: disponibles primero, por hora de carga y por id, así ningún PIN se repite ni se salta entre páginas) y borrar un PIN vuelve a la misma página |
| Afiliados | Crear afiliados, ver comisiones, marcar como pagadas |
| Cuentas | Las personas que crearon una cuenta: buscar, corregir sus datos, **restablecer la contraseña** si la olvidan, desactivar la cuenta, ver o quitar sus IDs enlazados y **moderar su perfil público** (ocultarlo o restablecerlo) (ver *Cuentas y perfiles* y *Perfil público*) |
| Soporte | Bandeja en vivo: lista de chats a la izquierda y el chat abierto a la derecha |
| Asistente IA | Botones y saludo del chat del cliente + asistente de IA con sus instrucciones |

### Bandeja de soporte en vivo

`/admin/soporte` es una pantalla dividida: a la izquierda la bandeja (buscador, filtros por etiqueta, pestañas **En atención / En espera / Resueltos** con su contador) y a la derecha el chat abierto, que se carga dentro de un `iframe` sin recargar la página. En pantallas angostas se ve una cosa a la vez, con botón para volver.

- La bandeja se actualiza sola consultando `GET /admin/soporte/bandeja.json` cada 4 s (cada 20 s si la pestaña está en segundo plano). Devuelve un `version` (hash); si no cambió responde `{"unchanged": true}` y no se repinta nada. También actualiza el contador del menú y el título de la pestaña.
- El chat abierto ya tenía su propio refresco de mensajes; dentro de la bandeja se abre con `?embed=1` (o el encabezado `Sec-Fetch-Dest: iframe`), que oculta el menú del admin.
- Es *polling*, no websockets: detrás de nginx + gunicorn funciona sin configuración extra. Si nginx manda `X-Frame-Options: DENY`, el panel derecho quedaría en blanco; debe ser `SAMEORIGIN` (o no enviarse).

**Diseño del chat del admin** (`admin/support_detail.html` y `admin/support.html`): contenedores de **cristal** (fondo translúcido, `backdrop-filter`, borde luminoso arriba/izquierda y un resplandor violeta/rojo detrás, igual que los paneles de la tienda); las burbujas son **cápsulas con la foto metida en una punta** (claras las del cliente con su inicial, oscuras las del equipo con la foto de soporte de Configuración o el logo, y el robot en las de la IA), cada una con nombre y hora arriba. Las **respuestas rápidas** son una fila de botones pegada arriba de la caja de escribir (un clic las manda); el botón *Editar* abre una ventana para crear, editar o borrar (los datos van en atributos `data-*`, no en un `onclick`). Junto al texto hay un **selector de emojis** propio (sin librerías: pestañas, "recientes" guardados en el navegador y se inserta donde está el cursor). Los mensajes dejan pasar el unidor de emojis U+200D (👩‍💻 ya no se parte en dos).

### Chat de soporte del cliente: menú de botones y asistente de IA

`/soporte` es un chat normal: el hilo se crea en el servidor con el primer mensaje (o el primer adjunto), así que abrirlo sin escribir nada no deja chats vacíos. Se configura en **Admin → Asistente IA** (`/admin/soporte/asistente`).

**El cliente solo ve su chat 24 horas** (`CLIENT_VISIBLE_HOURS` en `app/utils/support.py`) contadas desde el último mensaje del cliente, del equipo o de la IA (los avisos del sistema no cuentan). Pasado ese tiempo el servidor deja de reconocer el chat para ese navegador: la página sale con la bienvenida, `/soporte/hilo`, `/mensaje`, `/adjunto` y `/cerrar` responden **410** y borran la cookie `support_token`, y el JS borra su respaldo del token. Si el cliente escribe de nuevo (incluso desde una pestaña que se quedó abierta, también con una foto) se abre un chat NUEVO con ese mensaje. El chat viejo no se toca: el equipo lo ve completo en el panel, con un aviso de que el cliente quizá ya no lo ve. Si una respuesta del equipo llega después de las 24 h, solo le llega por correo o push si dejó datos. No hay aviso de "chat cerrado, escribe y lo reabrimos".

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

El panel es una hoja de cristal con una fila por aviso. **El color depende del proceso de la orden**: *en proceso* (verificando el pago, en proceso o pago incompleto) en **amarillo**, *aprobado* (lista) en **verde** y *rechazada* en **rojo**; el título va encerrado en un contenedor de ese color y el ícono (dibujado, ya no emojis) y el botón "Ver mi orden ›" también. Las novedades y mensajes del equipo van en neutro (negro / blanco). Todo el texto va en blanco puro. Los colores salen de la clase `st-wait` / `st-ok` / `st-bad` / `st-neutral` que pone `static/js/notifications.js` según `level` y `kind` del aviso.

La campana consulta `POST /api/notificaciones` (solo lectura) al cargar la página y cada minuto. Todo el texto se pinta como texto, nunca como HTML, y los links solo pueden ser rutas de la tienda o `https://`.

## Cristal en el menú, los botones del encabezado, las notificaciones y las ventanas

Todo usa el mismo cristal de las tarjetas como Player ID (`.glass-panel` en `neon.css`: fondo casi transparente, desenfoque, borde de luz arriba/izquierda y sombras). Regla importante: **un `backdrop-filter` dentro de otro no ve la página** (el de adentro solo ve lo que hay dentro del de afuera), por eso `.site-header` y `.categories-bar` ya no llevan desenfoque propio (main.css se lo ponía y apagaba el cristal de lo que tenían dentro) y las ventanas oscurecen y desenfocan con su propio fondo (`.overlay-modal`).

- **Menú lateral**: hoja de cristal flotante a la derecha con filas de cristal y las filas apareciendo una tras otra. Los cuadritos de los íconos van en **rojo, blanco y negro** (clases `drawer-icon-*`), más **dorado** (solo Ranking) y **azul** (solo "Soy mini influencer" y su panel, `href^="/minis"`), a pedido del dueño.
- **"Soporte" del menú** (`.drawer-item[href^="/soporte"]` al final de la sección del menú en `neon.css`): lleva **borde rojo**, un resplandor y un **anillo que se expande y late** (`::before`, animación `nx-support-ping`) más un pequeño tambaleo del ícono, para que los clientes lo vean; con "reducir movimiento" queda el borde rojo fijo.
- **Sorteo Diario** (`templates/promos/sorteo.html`, `/promos/sorteo`): toda la página pasó al mismo cristal que "Verificar ID" de la portada (`.glass-card` = `.glass-panel`: fondo casi transparente, desenfoque 4 px —8 px en pantallas anchas—, borde de luz y mismas sombras). Las tarjetas de adentro **no** llevan desenfoque propio (uno dentro de otro no ve la página; `.glass-card .glass-card`), las piezas pequeñas (botón de volver, etiqueta de estado, "Consultar", cuadros de ganadores/historial) usan `.glass-chip` / `.glass-inset`, y las ventanitas del ganador y de la repetición usan `.glass-overlay` + `.glass-window` como las ventanas de la tienda. El contenedor `.mobile-shell` ya no es negro sólido, así que la imagen de fondo que se ponga en Admin → Configuración → Fondos (`drawer_bg_sorteo`) se ve a través del cristal; sin imagen se ve el mismo fondo de la portada (negro con brillo rojo arriba y violeta abajo). **Adivina el Número** (`templates/promos/adivina_numero.html`, `/promos/adivina-el-numero`) recibió la misma conversión (cristal, contenedor transparente para que se vea el fondo `drawer_bg_adivina`, ventanita del premio en cristal); **Recarga Acumulada** conserva su estilo anterior.
- **Hordas de Diamantes** (`templates/promos/hordas.html`, `/promos/hordas`): misma conversión a cristal que el Sorteo (tarjetas, caja del ID, filas del ranking, ganadores anteriores, ventanita "Te faltan puntos" y las ventanas del juego: pausa, mejora y fin de partida) y **contenedores más compactos** (menos relleno y letras un poco más chicas, imágenes de personajes al 80 %) para ahorrar espacio. **Fondo**: antes esta página no tenía dónde ponerlo; ahora usa, en este orden, el fondo propio de **Admin → Configuración → Fondos de los accesos → "Hordas de Diamantes"** (clave `drawer_bg_hordas`, nueva) y, si no hay, el **fondo general de la web** (`SITE_BACKGROUND_IMAGE`), con el mismo velo oscuro (.72) de las otras páginas. La pantalla del juego en sí (canvas, joystick) no cambia.
- **Torneos** (2026-10-13, pedido del dueño): torneos **gratis, por orden de llegada, con salas**. El dueño publica el link en su canal de WhatsApp y, a la hora, abre las inscripciones desde **Admin → Dinámicas → Torneos** (`/admin/torneos`). **Cliente** (`/promos/torneos/<id>`, `templates/promos/torneos.html`; `/promos/torneos` muestra el torneo si hay uno solo, la lista si hay varios o "¡Muy pronto!" si no hay): escribe su ID, lo **verifica** (si el juego tiene verificador; el servidor lo vuelve a verificar) y toca "Unirme"; la web lo mete en la **primera sala con lugar** y solo entonces le muestra el **nombre, ID y clave de la sala** (con botones de copiar), su lugar y el aviso de que quien esté dentro y no figure en la lista será expulsado. Si ya estaba inscrito (mismo ID) vuelve a ver su sala, y el celular se acuerda de su ID. **Salas en cadena**: cada sala tiene un máximo; cuando se llena se **abre la siguiente** (las demás salen "Bloqueada"); con todo lleno dice "Cupos agotados". Los cupos se actualizan solos cada 10 s. **Admin** (`routes/admin_tournaments.py`, `templates/admin/tournaments.html` y `tournament_detail.html`): crear el torneo (nombre, juego, texto, inscripciones por conexión), **salas** (nombre, ID de la sala, clave, máximo de jugadores; se pueden reordenar, editar y borrar si están vacías), **premios** por puesto (1.º, 2.º... = un paquete del juego; valen para cada sala), estados **Preparando → Abierto → Cerrado → Terminado** con el botón "Copiar link para WhatsApp" y "Ver como cliente" (vista previa aun en Preparando), la **lista de inscritos de cada sala** (lugar, ID y nombre del juego) con "Copiar lista (ID - Nombre)" para compararla con quienes están dentro de la sala del juego y "Sacar" (esa persona ya no puede volver a inscribirse y su lugar queda libre; **la web no puede expulsar a nadie dentro del juego, eso se hace a mano**), y los **ganadores**: por cada puesto de cada sala se elige al jugador y "Entregar premio" (pide confirmar) crea el premio por el mismo camino que Sorteo y Hordas (`deliver_prize_to_player`, orden interna de $0 con prefijo `TORNEO`); una vez entregado no se repite ni se cambia. **Historial de ganadores** (fecha, sala, puesto, ID, nombre, premio y orden) por torneo y general. Seguridad: los lugares de cada sala son fijos del 1 al máximo con restricción única en la base (aunque entren 50 a la vez nadie se pasa del máximo ni repite lugar); la clave de la sala solo sale con el ID de un inscrito; tope de inscripciones por conexión (editable, 0 = sin límite) y de intentos por minuto; los apodos del juego se escriben siempre como texto (`jsattr` en el aviso de "Sacar"). Tablas nuevas: `tournaments`, `tournament_rooms`, `tournament_entries`, `tournament_prizes`, `tournament_winners` (se crean solas). Fondo propio en Configuración → Fondos de pantalla (`drawer_bg_torneos`). Entrada en el menú lateral dentro de "Dinámicas Gratis" y en la ventana "Juegos" de la barra de abajo (cada botón con su color; Hordas se queda en negro con la calavera en movimiento, Torneos va en amarillo con negro y blanco: las dos espadas pelean y el cuadrito tiembla como en una turbulencia). Pruebas: `test_tournaments.py` (98, incluye 14 inscripciones simultáneas), `test_torneos.py` (17), `ui_torneos_test.js` (29, Chrome real).
- **Torneos — salas elegibles, un ID por sala y torneos programados** (2026-10-13/14, pedido del dueño): (1) **Elegir sala** (`promos/torneos.html`): las salas son botones y salen en **cuadrícula de 2 por fila** (con una sola sala se ve la tarjeta ancha de siempre); al tocar una se abre un panel debajo. La **sala activa** (la primera con lugar) muestra los **premios** y el formulario (ID + verificar + "Unirme a Sala X"); una **bloqueada** muestra un aviso amarillo que se sacude ("No puedes unirte a Sala 2 porque Sala 1 todavía no está llena. ¡Apúrate a entrar a Sala 1, que es la sala activa!") con un botón "Ir a Sala 1"; una **llena** dice que está llena y deja entrar solo a quien ya estaba inscrito; una **terminada** lo avisa. Con una sola sala queda elegida sola. La tarjeta de estado suma el **resumen de salas** (N salas · N disponibles · N llenas · N terminadas, `public_state()['rooms_summary']`). "Tu lugar" sale en grande (`#7 de 30`) y el aviso de expulsión va en una caja amarilla que rebota (`warnBounce`, respeta `prefers-reduced-motion`). El servidor valida la sala elegida (`join_tournament(..., room_position)`): bloqueada/llena/terminada/inexistente -> error claro, y si la sala activa cambió mientras tanto lo dice. (2) **Volver a entrar**: tocar su sala + escribir el ID (`GET /promos/api/torneos/<id>/estado?player_id=..&room=N`); si está inscrito en ESA sala ve ID, clave y lugar; si no, `not_in_room` y la página dice "No estás inscrito en Sala N" (no se filtra nada de otra sala). (3) **Un ID solo puede estar en UNA sala a la vez** (en este torneo o en otro del mismo juego) hasta que esa sala **termine**: columnas nuevas `tournament_entries.game_id`, `released_at` y `tournament_rooms.finished_at`, índice único PARCIAL `uq_tournament_entry_live_player (game_id, player_id) WHERE status='active' AND released_at IS NULL` (dos toques a la vez tampoco lo duplican; ya no hay restricción única por torneo). Una sala termina con **"Terminar sala"** en el panel (reversible con "Reabrir sala" mientras ninguno de sus jugadores haya entrado a otra), sola cuando se entregan **todos los premios** de la sala, o cuando se **termina el torneo**; al terminar, su inscripción queda en el historial, sus jugadores quedan libres y la sala ya no frena a la siguiente (estado `finished`). Un ID sacado por el admin sigue sin poder volver a ese torneo. `_ensure_tournament_entry_rules()` (`app/__init__.py`) reconstruye la tabla vieja de las bases de prueba (SQLite). (4) **Verificación de ID obligatoria en todo torneo** (`verify_player_strict`): sin verificación activa en el juego no se puede abrir el torneo ni inscribirse; si el juego no devuelve el nombre de la cuenta tampoco se deja pasar (el ID + nombre es lo que identifica a la persona). En local `.env` trae `SCRAPE_ENABLED=false` y no hay `active_login_game_id`, así que NO deja inscribir hasta activarlo en Configuración → Verificación de ID. (5) **Torneos programados**: campo "Se abre el" (fecha y hora de Venezuela, selector propio oscuro y en español `static/js/datetime-picker.js`, `window.ModernDateTime`, cargado en todo el admin); publicado con hora futura los clientes ven una **cuenta regresiva** (el servidor manda los segundos que faltan) y a la hora se abre **solo** sin recargar; los datos de las salas se cargan antes. Botones "Publicar con cuenta regresiva" / "Abrir ya". Pruebas: `test_tournaments.py` (reglas L–R: sala elegida, un ID/una sala, volver a entrar, terminar/reabrir, premios que terminan la sala, dos torneos a la vez, programados), `ui_torneos_test.js` (elegir sala, volver a entrar, un ID/una sala, torneo programado que se abre solo, panel).
- **Ranking — columna de premios rediseñada** (2026-10-14): tarjetas por puesto con color de medalla (oro/plata/bronce y rojo para el resto), insignia redonda con el número, premio en grande con una gema de diamante dibujada (`rankingPrizeValueHtml` en `main.js`; si el premio no es una cantidad de diamantes sale el nombre del paquete como texto) y un brillo suave en el 1.º. Van en UNA fila arriba del ranking (`--n` = cantidad de premios) en celular y en PC. **La web es para celular**: en PC la ventana del ranking también se ve del tamaño de un celular (`min(100%, 460px)`, centrada) y su lista baja con la rueda del mouse (antes en PC quedaba cortada y no bajaba: la tarjeta es `flex` en columna y `.ranking-board` hace el scroll). Los totales del ranking dicen **"recargados"** (`Diamantes recargados`, `Oro recargado`, `units_label` en `RANKING_DEFS` de `routes/main.py`) para que nadie crea que ese número es el premio. Estilos en `neon.css` (sección "Ranking: recuadro de PREMIOS"); prueba `ui_rank_pc_test.js`.
- **Letra de la web (2026-10-13, pedido del dueño):** la letra base del texto pasó de *Space Grotesk* a **Archivo Black** en TODO lo que usaba la anterior: variable `--nx-font-sans` de `neon.css` (portada, perfiles y su editor, soporte, ranking, minis, iniciar sesión, Mi cuenta...), los usos sueltos de `"Space Grotesk"` en `neon.css` y la opción «Letra del nombre» del perfil (la clave interna `space` se conservó para no romper lo ya guardado; ahora dibuja Archivo Black). Se carga desde Google Fonts en `base.html`, `profiles/*`, `auth/login.html` y `auth/profile.html`. Lo que usaba **Outfit** (títulos, números, botones de la barra de abajo y las páginas de juegos Hordas, Sorteo, Adivina y Recarga Acumulada) NO se tocó. Como Archivo Black solo tiene un grosor, `font-synthesis-weight: none` en `body` evita que el navegador la «engorde» de nuevo. Para volver atrás basta cambiar `--nx-font-sans` en `neon.css`.
- **Logo al centro:** en las cabeceras donde el logo va solo (perfil público, editor del perfil, Mi cuenta, iniciar sesión) se centra con `.header-inner > .logo-link:only-child { grid-column: 2 }` (`neon.css`). En la tienda y soporte ya iba al centro (hueco | logo | campana, instalar y menú); queda a unos 4 px del centro exacto porque a la derecha hay tres botones y no cabe más centrado sin pisarlos. Las páginas de juegos (Hordas, Sorteo...) no tienen cabecera con logo.
- **Editor del perfil más compacto:** las ventanas de configuración (campos, textos, botones, barras, tarjetas de imagen y de marcos) se achicaron al final de `profile.css` (bloque «Ventanas de configuración MÁS COMPACTAS»); la vista previa de arriba no cambió. Los 5 botones de configuración están en cuadrícula de 2 columnas (el 5.º ocupa todo el ancho).
- **Campana y botón del menú** (`.nav-toggle`): botones de cristal con exactamente el cristal de `.glass-panel`.
- **Ranking de la tienda**: podio con los 3 primeros y lista del 4.º al 10.º. **Tocar la foto de cualquiera** abre un cuadro de cristal (`.rk-pop`, en el `body`; fondo negro .5 con desenfoque de 10 px y tarjeta sin desenfoque propio): con el botón **"Ver perfil"** (lleva a `/u/<usuario>`) si esa persona tiene cuenta con ese ID **verificado**, perfil **público** (no oculto por el admin) y la casilla "Aparecer en el Top de perfiles y en el Ranking de la tienda" encendida en su editor; si no, un **aviso** ("todavía no tiene un perfil público en la tienda") con "Entendido". Para el visitante, sin cuenta / privado / oculto / casilla apagada / ID sin verificar se ven **igual** (el servidor solo manda el enlace cuando se cumple todo: `profile_social.ranking_profiles`, campo `profile` de `/api/rankings`). La foto sale solo si además tiene foto de perfil. Es una **decisión del dueño (2026-10-13) que cambia la regla anterior** de que el ranking no debía enlazar a perfiles: el perfil público sigue sin mostrar IDs, compras ni juegos, pero ahora quien mira el ranking puede ver quién es el puesto N (ID tachado y número de diamantes) si esa persona lo permitió. Escape, tocar fuera o la X cierran el cuadro (Escape cierra solo el cuadro, no el ranking); las fotos también se usan con teclado (Tab, Enter, Espacio). Pruebas: `ui_ranking_test.js`.
- **Ventanas** (Ranking, Canjear puntos, Soporte...: `.overlay-modal-card`): el mismo cristal de Player ID, esquinas de 20 px, sin el borde rojo grueso ni el brillo pulsante de antes.
- Las pruebas del navegador comparan con píxeles reales (franjas de colores de fondo) que el fondo se vea igual a través de estos cristales y a través de Player ID.

## Cuentas y perfiles

Hay **dos formas de entrar** y no hacen lo mismo:

| | Con el ID | Con una cuenta |
|---|---|---|
| Cómo | Pestaña **Con mi ID** (juego + ID/correo con el que compró) | Pestañas **Mi cuenta** / **Registrarse** (usuario o correo + contraseña, o Google) |
| Perfil | **Solo lectura** (`/profile`): ve sus órdenes y un aviso "crea una cuenta para editar" | Dos páginas: su **perfil público** (`/u/<usuario>`, estilo Discord, lo ven los demás) y **Mi cuenta** (`/account`: correo, contraseña, IDs enlazados y órdenes; es privado) |
| Órdenes que ve | Las de ese ID | Las de **todos los IDs que enlazó** (si entró con Google, también las hechas con su correo) |

- **Registrarse** pide *nombre de usuario, correo, contraseña y repetir contraseña* (más un campo escondido que solo llena un programa). Usuario: 3-20 caracteres (letras, números, `.`, `-`, `_`), único sin importar mayúsculas y sin nombres reservados (admin, soporte...). Correo: válido y único. Contraseña: mínimo 8, no puede ser igual al usuario/correo ni de las típicas (12345678...). Al registrarse queda con la sesión abierta en su perfil básico. El diseño del perfil se afinará después.
- **Enlazar un ID** (Mi cuenta → Mis IDs, hasta 10; reglas decididas por el dueño el 2026-10-13, para que la gente se haga el perfil **antes** de comprar y arme comunidad): (1) **cualquiera puede poner su ID, tenga compras o no**, pero el ID **tiene que existir** en el juego (misma comprobación de la compra: `routes/verify.require_verified_player`; si el juego no tiene verificador, pasa). (2) Un ID **vive en UNA sola cuenta**: si ya está en otra, la web dice "ese ID ya está usado en otra cuenta" (sin decir de quién) y muestra el formulario para **verificarlo**. (3) **Quien lo verifica se lo lleva**: con el número de una orden de ese ID (aprobada o completada) y los últimos 4+ dígitos de su referencia de pago, el ID pasa a su cuenta, queda verificado y se **borra de la cuenta que nunca lo verificó**, que recibe un aviso (`AccountNotice`, sale una sola vez en Mi cuenta, sin decir quién fue). (4) Un ID **ya verificado en otra cuenta NO se mueve solo** (cualquiera puede pagar una recarga a un ID ajeno y así conseguir los datos de la prueba): solo lo mueve el administrador (Admin → Cuentas → detalle → enlazar ID: queda verificado, se le quita a la otra cuenta y se le avisa). (5) Un ID sin verificar **no caduca** (el dueño lo decidió: nunca queda bloqueado porque el dueño verdadero siempre puede verificarlo con una compra). Estados en Mi cuenta: *Sin compras todavía* (ID sin órdenes), *Verificación pendiente* (ya tiene compras y falta verificarlo) y *✓ Verificado*; un ID sin verificar no muestra sus órdenes, no cuenta para el nivel/ranking/insignias y no da el signo. Quitarlo solo lo oculta del perfil (no borra nada). Una cuenta **sin compras** igual puede dar me gusta y seguir. Código: `utils/auth_accounts.link_id_to_account` / `claim_verified_link` / `verify_existing_link` (con un candado por ID `id_claim:<juego>:<id>` para que dos cuentas no se lo lleven a la vez).
- **Comprar con una cuenta**: la sesión de la cuenta **no cambia** al comprar. La orden se guarda a nombre del cliente de ese ID (igual que si comprara sin cuenta) y la cuenta la ve cuando tiene ese ID enlazado. Comprar sin cuenta sigue iniciando la sesión del cliente de ese ID, como siempre.
- **Datos**: tabla `users` (cuenta con contraseña: `account_kind='registered'`, `account_scope='account'`; Google: `google_email`/`google`; cliente por ID: `player_id`... y `account_scope='game:<id>'`) y tabla nueva `user_linked_ids` (cuenta, juego, ID; la crea sola `db.create_all()`, sin migración). Código: `routes/auth.py` (registro, `/login/cuenta`, `/profile` y sus POST), `utils/auth_accounts.py` (reglas y órdenes del perfil), `templates/auth/login.html` y `profile.html`.
- **Seguridad**: contraseñas con hash de werkzeug; mismo mensaje "Usuario o contraseña incorrectos" exista o no la cuenta (y se gasta el mismo tiempo); tope de 20 intentos por IP y 12 por cuenta cada 10 min, 10 registros por IP por hora, 8 intentos de cambio de correo/contraseña por cuenta cada 10 min; la cuenta de Google y el cliente por ID **no** entran por el formulario de contraseña; una cuenta desactivada se queda fuera al siguiente clic; los textos del usuario se muestran escapados.
- **Admin → Cuentas** (`/admin/cuentas`): contadores, búsqueda (usuario, correo, número; el `%` y `_` son literales), filtros por tipo y estado, 25 por página. En el detalle: cambiar usuario/correo/teléfono (el correo de una cuenta de Google no se cambia), **restablecer la contraseña** (escribir una o "Generar contraseña temporal": se muestra **una sola vez** en pantalla, sin letras que se confunden, y no queda guardada en ningún mensaje), activar/desactivar la cuenta (desactivar no borra nada) y enlazar/quitar IDs. Los clientes que solo compran con su ID no salen ahí (no tienen cuenta). No hay forma de "ver" una contraseña: se guardan cifradas.
- **Recuperar la contraseña**: no hay correo automático (el servidor no tiene SMTP); la persona escribe a Soporte y el admin le restablece la contraseña desde Cuentas.
- Pruebas (fuera del repo): `test_accounts.py` (~200 comprobaciones: validaciones, límites, perfil de solo lectura vs cuenta, IDs enlazados, compra, admin) y `ui_accounts_test.js` (navegador real).
- **Mi cuenta (`/account`), una sola tarjeta:** arriba va UNA tarjeta con la identidad (foto, usuario, correo, miembro desde), los botones **Mis datos** y **Cerrar sesión**, y debajo, dentro de la misma tarjeta, "Tu perfil público" (Personalizar mi perfil / Ver mi perfil). **Mis datos** (nombre de usuario, correo, contraseña) ya no es una tarjeta: es un **popup** (`#accountDataModal`, fuera de las tarjetas porque el cristal de una tarjeta rompe el `position:fixed`) que se abre con el botón, se cierra con la X, tocando fuera o con Esc; al guardar o fallar un cambio, la página vuelve con `#datos`, reabre el popup y muestra el mensaje adentro. Le siguen "Obtén el verificado y su insignia", "Mis IDs" y "Mis órdenes".
- **Rutas**: `/profile` lleva a quien tiene cuenta a su perfil público (`/u/<usuario>`; si es de Google y aún no eligió usuario, a `/account`) y a quien entró solo con su ID le muestra el perfil de solo lectura. `/account` es la página de seguridad (solo cuentas). El registro y el inicio de sesión terminan en `/profile`.

## Perfil público (estilo Discord)

Cada cuenta (con usuario elegido) tiene una página pública en **`/u/<usuario>`** que su dueño personaliza en **`/profile/edit`** (botón "Personalizar mi perfil" en Mi cuenta o "Editar perfil" en el propio perfil). El editor muestra la **vista previa en vivo** (la misma tarjeta de la página pública) y **cinco botones**: *Mi presentación*, *Foto de perfil*, *Portada (banner)*, *Fondo de la página* y *Colores y letra*. Cada botón abre una **ventana (popup)** con el **perfil pequeño arriba** (es el mismo elemento de la vista previa, que se mueve a la ventana y vuelve al cerrarla), las opciones al medio y los botones "Listo" y "Guardar cambios" fijos abajo; se cierra con la X, tocando fuera o con Escape. Debajo de los botones hay un contador "Imágenes propias: N de 4".

**Qué se puede personalizar**
- **Presentación**: nombre para mostrar, "Acerca de mí" (300 caracteres), público / privado, y mostrar u ocultar la fecha de registro.
- **Foto de perfil**: imagen (PNG, JPG, WEBP o GIF animado; se recorta cuadrada), forma (círculo o cuadrado redondeado), casilla del **aro de su nivel del mes** (lo único que queda de los aros de color: Bronce, Plata, Oro, Diamante o Leyenda, se pone solo según el nivel) y el **marco** (ver *Marcos* abajo).
- **Banner**: imagen o 2 colores en degradado (6 listos, dentro de un desplegable) y el **marco** de la portada (ver *Marcos*).
- **Fondo de la página**: degradado, color liso, imagen propia o el banner difuminado, con oscurecer y desenfoque; colores, imagen y oscurecer/desenfoque van en tres desplegables. Una imagen de fondo solo se ve con el tipo "Mi imagen": al subirla (o elegirla) el tipo cambia solo, y si hay imagen guardada pero el tipo no la usa, el editor avisa con un botón "Usar mi imagen como fondo".

**Marcos (imágenes, con o sin movimiento).** Los aros y marcos **de color** (neón, doble línea, esquinas...) **ya no existen**: lo que el dueño pidió son marcos de imagen. Un marco es una imagen con el centro transparente (PNG, WEBP o GIF animado, peso máximo **3 MB** el marco de la foto y **5 MB** el de la portada) que se pone encima de la foto o de la portada. Hay tres opciones por sitio: **sin marco**, uno de la **tienda de marcos** o **"Mi marco"** (uno que la persona sube y hace ella misma; en la foto se puede ajustar tamaño y posición). La tienda se maneja en **Admin → Cuentas → "Tienda de marcos"** (`/admin/cuentas/marcos`): se sube la imagen, se elige si es para foto o portada y se le pone un **precio en puntos** (0 = gratis para todos). Un marco con precio se **compra una sola vez** desde el editor, pagando con los **puntos de un ID verificado** de la cuenta (se descuentan con una operación atómica, sin pasarse del saldo ni cobrar dos veces), y desde entonces es suyo para siempre. Si un marco que ya compraron se "borra", solo se desactiva en la tienda (no se le quita a quien lo tiene). En la base, `avatar_frame_id` / `banner_frame_id` valen: el id del marco de la tienda, `NULL` = su marco propio, `-1` = sin marco. Los valores viejos de aro/marco de color que ya estaban guardados se ignoran al dibujar (no se borran).

**Medidas ideales** (se muestran en el editor, en un recuadro ámbar bajo cada marco, y en Admin → Cuentas → Tienda de marcos): marco de la **foto 600 × 600 px** (cuadrada, con el hueco transparente del centro de unos 430 px = 72 % del ancho, porque el marco se dibuja al 140 % de la foto) y marco de la **portada 1500 × 600 px** (proporción 5:2, la de la portada, se estira justo encima). Un GIF con movimiento solo conserva la animación si no pasa de 1024 px (foto) o 1600 px (portada) de lado; más grande se aplana a una sola imagen. Pesos máximos: `MEDIA_KINDS` en `utils/profiles.py` (marcos propios) y `MAX_FRAME_BYTES_BY_KIND` en `utils/profile_frames.py` (marcos de la tienda).

**Marco de la portada que SOBRESALE** (probado el 2026-10-13, pedido del dueño para dibujos con manos u otras partes que se salen de la portada): en **Portada → Mi marco para la portada** hay una barra nueva, **«Cuánto sobresale»** (0 a 50 %, en 0 por defecto: así ningún perfil existente cambia). El marco es una imagen que normalmente se estira justo sobre la portada (proporción 5:2); con la barra en N % se dibuja **más alta** (alto de la portada × (1 + 2N/100)), igual por arriba y por abajo, de modo que la franja del centro cae justo sobre la portada y lo que se dibujó fuera de ella se sale. Medida pensada: **1500 × 1200 px** con fondo transparente y la franja de la portada (1500 × 600) en el centro (50 % = 300 px por arriba y por abajo). Al elegir la imagen, la barra se **ajusta sola** al tamaño real del archivo (`suggestBannerExtra` en `profile-editor.js`: (alto/ancho/0.4 − 1)/2, entre 0 y 50 %) y avisa por qué; se puede cambiar a mano. Solo cuenta si hay un marco puesto (propio o de la tienda): con «Sin marco» el efecto se apaga. La portada se baja N % de su alto para dejar libre el espacio de arriba (así las manos no tapan los botones de arriba), y la foto de perfil y los textos quedan **por encima** del dibujo. Se guarda como `banner.ov_extra` en el tema (`THEME_SPEC`, rango 0–50, recortado en el servidor), se dibuja con `--bn-ov-extra` y `data-bn-pop="on|off"` (`theme_vars` / `theme_attrs`, y su espejo en `profile-editor.js`; CSS al principio de `profile.css`). Cuenta como una sola imagen del tope de 4. Pruebas: `test_frames_editor.py` (sección L) y `ui_controls_test.js` (sección E). En `Desktop\3srecargas_demo` hay dos imágenes para probar: `ejemplo_portada_manos.png` (se sube tal cual) y `plantilla_portada_guias.png` (guías para dibujar encima; no se sube).

**Controles del editor (todos del mismo estilo de cristal):** el **selector de color** moderno (`static/js/color-picker.js` + `css/color-picker.css`; reemplaza al cuadro viejo del navegador en los campos `<input type="color" data-modern-color>` del editor y de las etiquetas de soporte del admin: botón con el color y su código, panel con cuadro de color, tono, código `#RRGGBB`, gotero donde el navegador lo permite y 16 colores rápidos; el `<input>` original sigue escondido guardando el valor, así que la vista previa en vivo y el guardado no cambian); el **selector de imagen** (tarjeta con miniatura, nombre y peso; se toca o se arrastra la imagen encima; "Quitar la imagen actual" es un botoncito); las **barras** (tamaño, mover) finas y compactas con relleno rojo; y las **casillas** como interruptores.

**Tope de 5 imágenes propias por perfil** (2026-10-13, pedido del dueño: antes eran 4 y no se podían llenar los cinco espacios). Cuentan la foto, el marco de la foto, el banner, el marco del banner y el fondo; los marcos de la tienda **no** cuentan. Hay un espacio por cada tipo, así que los cinco se pueden llenar; el tope (`MAX_PROFILE_IMAGES` en `utils/profiles.py`) sigue protegiendo en el servidor (primero procesa lo que se quita y luego lo que se sube) y, si algún día se baja, los espacios vacíos se apagan con un aviso.

**Marcos y fotos con movimiento (arreglado 2026-10-13):** al guardar, una imagen propia animada **conserva su movimiento**. Un **GIF o WEBP animado** que cabe en la medida máxima se guarda **tal cual** (después de comprobar que se lee hasta el último cuadro); un **PNG animado (APNG) o una animación más grande que el máximo** se achica cuadro por cuadro y se guarda como **WEBP animado** (misma velocidad, repetición sin fin, transparencia y recorte cuadrado en la foto). Antes solo se conservaba el GIF chico y lo demás quedaba con un solo cuadro. Límites que protegen al servidor: hasta **300 cuadros**, 160 millones de píxeles en total (cuadros × ancho × alto) al guardar tal cual y 64 millones al volver a procesar, y el peso máximo del tipo (3 o 5 MB; al reprocesar, si no cabe se baja la calidad). Si se pasa, el aviso dice cuántos cuadros tiene el archivo. El fondo sigue siendo una imagen fija. Los marcos de la tienda del admin se guardan tal cual (siempre conservaron el movimiento).

**Admin → Configuración → "Imágenes y fondos del sitio" ordenado (2026-10-13, pedido del dueño: "parece un desastre"):** antes era una cuadrícula de dos columnas con cajas grandes de "toca aquí para subir foto", las miniaturas debajo (filas desparejas) y un párrafo larguísimo. Ahora hay un texto corto, un desplegable "Medidas y peso recomendados" y **3 grupos** con tarjetas parejas (macro `img_tile` en `templates/admin/settings.html`, estilos `.img-*` al final de `admin.css`): **Marca, chat y seguimiento** (logo, foto de soporte, imagen de seguimiento), **Banner promocional** (imagen o video + link) y **Fondos de pantalla** (el de la web y uno por cada página: Recarga Acumulada, Sorteo, Adivina, Hordas, Canjear código, mini influencer, Iniciar sesión). Cada tarjeta lleva **el mismo icono de línea "subir imagen"** (SVG, macro `upload_icon`; antes había emojis distintos 🖼️/📷), la miniatura de lo que está puesto (o "Sin imagen" con ese icono atenuado), su botón de subir/cambiar y la casilla de quitar; una por fila en celular y hasta tres en escritorio. Los nombres de los campos y el guardado no cambiaron. Prueba: `ui_admin_images_test.js` (33), más `ui_avatar_test.js` y `ui_banner_video_test.js`.

**Botones de abajo de cada ventana del editor (2026-10-13, pedido del dueño):** antes eran "Listo" y "Guardar cambios" y parecían hacer lo mismo ("Listo" solo cerraba y dejaba lo cambiado a medias en la vista previa). Ahora son **"Volver"** y **"Guardar cambios"**: **Volver** (y la X, tocar fuera y la tecla Escape) cierra la ventana **sin guardar y quita lo que se cambió dentro de ella** (fotos elegidas, textos, colores, barras); si no se tocó nada cierra sin preguntar, y si hubo cambios pregunta "¿Volver sin guardar?". Lo que se cambió antes de abrir la ventana no se pierde y la marca de "cambios sin guardar" se conserva. **Guardar cambios** guarda todo (lo de todas las ventanas) y no pregunta nada. Código: `takeSnapshot`/`restoreSnapshot`/`requestClose` en `static/js/profile-editor.js`; botones en el macro `pop` de `templates/profiles/edit.html`.
- **Colores y estilo**: color principal, color del nombre y letra del nombre (5). El perfil **no lleva contenedor grande** detrás ni caja alrededor del nombre, y no hay aviso amarillo de "completa tu perfil": el banner, la foto, el nombre y las secciones van directo sobre el fondo.

**Reglas de la tienda (fijas, pedidas por el dueño):** el perfil público **no muestra redes sociales ni enlaces**, **no tiene comentarios ni forma de contactar**, y **nunca muestra IDs, la lista de compras, cantidad de órdenes ni los juegos enlazados**. Motivo: que la competencia no pueda entrar, sacar información y llevarse a los clientes que más compran. Lo único que sale de las compras son **números agregados** (diamantes, nivel del mes, puestos e insignias) y solo de IDs verificados, que el dueño del perfil puede apagar (ver *Perfiles sociales*); además sale la fecha de registro o de primera compra. Cómo se cumple (`utils/contact_filter.py`): en el nombre para mostrar, la frase de estado y "acerca de mí", todo lo que parezca un enlace, dominio, correo, @usuario, nombre de red (Instagram, TikTok, WhatsApp, Telegram, Discord, YouTube...) o número de 6 o más dígitos (ID/teléfono) **se TACHA con asteriscos** (`mask_contact_info`; los espacios se quedan y el resto del texto se guarda normal: "escríbeme al 0414 123 4567" → "escríbeme al **** *** ****"). El editor muestra la vista previa ya tachada al instante y avisa (`maskContact` en `profile-editor.js` hace lo mismo que el servidor: si se cambia una, cambiar la otra); al guardar avisa "Tachamos con asteriscos algo de...". Si de un nombre no queda ni una letra o número (era todo un teléfono o ID) se muestra el usuario. La regla también rige al MOSTRAR (perfil, Top y Mi red): un texto viejo o metido a mano en la base sale tachado. El **nombre de usuario**, que es un identificador, no se puede tachar: ese sí se rechaza (`username_looks_like_contact`). No existe buscador ni directorio de perfiles: se entra con la dirección exacta o desde el **Top de perfiles** (solo cuentas que aceptaron aparecer). Como el filtro no es perfecto (alguien puede escribir "i n s t a"), el admin puede **ocultar** o **restablecer** cualquier perfil (Admin → Cuentas → detalle → Perfil público).

**Privacidad y seguridad**
- Público (cualquiera con la dirección), privado ("solo yo": para los demás da 404) u **oculto por el admin** (404 para todos menos el dueño y el admin; el dueño no puede quitarlo). Los perfiles llevan `noindex` (los buscadores no los indexan).
- Todo lo que llega del formulario se valida en `utils/profiles.py` con listas permitidas y rangos: colores solo `#rrggbb`, números acotados, estilos de una lista; nada del usuario llega al CSS o al HTML sin pasar por ahí, y los textos salen escapados.
- Imágenes (`save_media`): extensión PNG/JPG/WEBP/GIF, se abren y vuelven a guardar con Pillow (WEBP; metadatos fuera; un archivo que no es una imagen de verdad se rechaza), topes de peso (foto 3 MB, banner 4 MB, fondo 4 MB, marco de la foto 3 MB, marco de la portada 5 MB) y de píxeles (16 millones), nombre aleatorio en `uploads/profiles/<id de la cuenta>/`; al reemplazar, quitar o restablecer se borra el archivo viejo del disco. Un GIF animado pequeño se conserva animado (foto, banner y marcos); los fondos siempre quedan fijos. Los marcos de la **tienda** los sube el admin (`utils/profile_frames.py`): se verifican con Pillow (que sean una imagen de verdad PNG/WEBP/GIF) y se guardan tal cual, para no perder la animación, en `uploads/profile_frames/frame_<12 hex>.<ext>`.
- Tope de 20 guardados cada 10 minutos por cuenta.
- Código: `models.UserProfile` (tabla `user_profiles`, la crea sola `db.create_all()`; los marcos de la tienda son `ProfileFrame` y lo que compró cada cuenta `UserFrame`; las dos columnas nuevas `avatar_frame_id`/`banner_frame_id` las agrega sola `_ensure_profile_social_columns`), `routes/profiles.py` (también `buy_profile_frame`), `utils/profiles.py`, `utils/profile_frames.py`, `templates/profiles/` (`_scope.html` es la tarjeta, `public.html`, `edit.html`), `static/css/profile.css` y `static/js/profile-editor.js`. El dibujo es solo variables CSS y atributos `data-*` que calcula el servidor (`theme_vars` / `theme_attrs`) y que el editor recalcula en vivo: **si se agrega una opción hay que tocar `profiles.py`, `profile.css` y `profile-editor.js`**.
- Pruebas (fuera del repo): `test_profiles.py` (~150 comprobaciones: acceso, privacidad, reglas, textos, colores, imágenes, archivos peligrosos, moderación, límites), `test_frames_editor.py` (60: tienda de marcos, compra con puntos, tope de 4 imágenes) y `ui_profile_test.js` (navegador real: botones y ventanas con el perfil pequeño, vista previa en vivo, marco propio, fondo, guardar, página pública, visitante).

**Botones y barra en el perfil público:** el dueño ve arriba tres botones en una sola fila (**Tienda** para volver, **Configuración** y **Copiar enlace**); los visitantes ven Tienda y Top de perfiles. Abajo sale la **misma barra flotante de la portada** (Perfil, Ranking o Puntos, Inicio, Juegos, Soporte), que ahora es una pieza compartida: `templates/_dock.html` (`dock_page = 'home'` en la portada, `'profile'` en el perfil), con `dock.js` y los estilos de `neon.css`. En el perfil el botón rojo descansa en **Perfil** (`data-dock-selected`; tocarlo no recarga, solo sube: `data-dock-here`), **Inicio** y **Soporte** son enlaces, **Juegos** abre la misma ventana, y como las ventanas de **Ranking** y **Puntos** viven en la portada, ahí son enlaces a `/#ranking` y `/#puntos` (`main.js` las abre al llegar y limpia la dirección). La ventana de Configuración queda por encima de la barra.

## Perfiles sociales: nivel, números, insignias, premios, me gusta, seguir y top

Fase 1 del documento "Perfiles sociales en 3S Recargas" (PDF del dueño), **adaptada a sus reglas** (ver arriba: nada de IDs, redes ni comentarios). Está sobre el perfil público `/u/<usuario>`.

**Lo que se ve en el perfil** (todo apagable con "Mostrar mi nivel del mes, mis números, insignias y premios" en Editar perfil):
- **Frase de estado** (máx. 60 letras, con el mismo filtro de contacto) y **título** junto al nombre (el del premio más reciente marcado como título, p. ej. "Campeón de Septiembre").
- **Nivel del mes** (Bronce, Plata, Oro, Diamante, Leyenda) con barra y "Te faltan 350 diamantes para Oro". Se reinicia cada mes. Los umbrales se editan en **Admin → Cuentas → "Ajustes de perfiles"** (por defecto 0 / 500 / 1.500 / 3.000 / 6.000).
- **Nivel por números (como Steam)**, aparte del nivel del mes y permanente: **Nivel 1, 2, 3...** que sube con puntos de experiencia (XP). Se ve como un círculo con el número y su color **junto al nombre** (cambia de color cada 10 niveles: gris, rojo, naranja, amarillo, verde, azul, violeta, rosa, turquesa, oro, leyenda), en un bloque "Nivel de experiencia" con barra y "Le faltan N para el nivel X", en el **Top de perfiles (pestaña "Nivel")** y en "Mi red". Sin premios: solo número y color; lo ve todo el mundo (se apaga con la misma casilla de estadísticas). Código en `app/utils/profile_xp.py`; columnas `xp_total`, `xp_level`, `xp_rank` de `profile_stats`.
  - **El dueño lo configura todo** en **Admin → Cuentas → "Ajustes de perfiles" → "Niveles por números"**: puntos por cada recarga, por cada 100 diamantes, me gusta dados/recibidos, seguidores nuevos/seguidos, partidas de Hordas, registros del Sorteo, intentos de Adivina, perfil completo, insignias, meses de racha y Top 3 (0 = no da puntos); **tope por día** de las acciones de comunidad y juegos (anti cuentas falsas); la **curva** (puntos del nivel 1 al 2, cuánto más cuesta cada nivel, nivel máximo) con tabla de ejemplo; y las **10 misiones** (5 diarias y 5 semanales: meta y puntos, activables). Al guardar se recalcula todo.
  - Los puntos se **calculan desde los hechos** (no se suman a mano): quitar un me gusta o dejar de seguir quita sus puntos; si cambias los números, todos se recalculan. Recargas y juegos solo con **IDs verificados** (partidas marcadas como trampa no cuentan). Las misiones cuentan **solo desde el día en que se activó el sistema** (no regalan puntos por el pasado) y solo las ve su dueño (con "Se reinicia en ..."), junto al desglose "¿De dónde salen mis puntos?". Subir de nivel deja un aviso en Mi cuenta (uno solo: se reemplaza el anterior sin leer).
- **Números**: diamantes totales, recarga del mes, ranking del mes, ranking histórico, reputación y racha (meses seguidos recargando), y el **producto favorito** (el paquete que más compra: solo su nombre, nunca el juego ni cuántas veces; si empatan, el comprado más recientemente). "Cliente desde" = primera compra verificada (si las estadísticas están apagadas, la fecha de registro, para no revelar cuándo empezó a comprar).
- **Insignias** con rareza: Primera recarga, 10 recargas, 50 recargas, Fiel (racha de 3 meses), Veterano (1 año), Popular (50 me gusta), Top 3 del mes y Ballena (umbral editable, 10.000 por defecto en un mes).
- **Vitrina de premios**: el admin los carga a mano (título, descripción, imagen opcional, "mostrar como título") en el detalle de la cuenta; los de "Campeón / Top 2 / Top 3" salen solos al cerrar el mes.
- **Aro automático** (casilla en el editor): el aro de la foto toma los colores del nivel del mes; Leyenda gira.
- **Me gusta, seguidores y siguiendo**: contadores públicos; botones "Me gusta" / "Seguir" para quien visita.

**IDs verificados (la pieza de seguridad):** los números, el ranking, las insignias y el signo de verificado salen **solo de IDs verificados**. Un ID enlazado sin verificar no suma nada: si no, cualquiera enlazaría el ID de otro cliente y vería (y publicaría en su perfil) cuánto compra. Se verifica en **Mi cuenta → "Obtén el verificado y su insignia"** (formulario con producto, ID, número de orden y referencia del pago; sirve para un ID nuevo, para uno ya enlazado sin verificar y para reclamar uno que está en otra cuenta sin verificar) o en la fila del ID (**"Verificar este ID"**), con el **número de una orden de ese ID** (aprobada o completada) y los **últimos 4+ dígitos de su referencia de pago** (misma comprobación que ya usa la tienda para mostrar un PIN: `order_access.reference_tail_matches`); mensaje de error general (no dice cuál falló) y tope de 6 intentos cada 10 minutos. Una cuenta de Google cuenta además las órdenes hechas con su correo verificado. *Limitación conocida:* alguien podría comprar algo pequeño para el ID de otra persona y así tener el número de orden y la referencia; es una barrera de costo, no una prueba perfecta (por eso un ID verificado no se le quita a su dueño con esa prueba). *Pista que se aceptó:* en Mi cuenta, quien enlazó un ID ajeno ve "Verificación pendiente" cuando ese ID compra (el dueño decidió mantener los tres estados).

**Cuáles órdenes cuentan:** aprobadas o completadas, que no sean premios (`payment_method='prize'`), de los IDs verificados; los diamantes se sacan del nombre del paquete (`utils/order_units.extract_order_units`, igual que el Ranking mensual) y los meses van en hora de Venezuela.

**Signo de verificado ✔ (insignia):** las cuentas con **al menos un ID verificado** llevan un signo azul **junto al nombre** en su perfil público, en el Top de perfiles y en Mi red (`templates/profiles/_verified.html`, estilos `.pf-verified` en `neon.css`; `profile_social.verified_user_ids` / `is_verified_account`). Solo dice "Cuenta verificada: compró en 3S Recargas" (sin IDs, juegos ni cantidades) y sale aunque la persona apague "mostrar mis estadísticas". Para animar a verificar, Mi cuenta tiene el apartado **"Obtén el verificado y su insignia"** (3 pasos: recarga con tu ID, anota el número de orden y la referencia del pago, escríbelos ahí). El formulario manda a `/profile/ids` con el campo oculto `from=verify`; si algo falla vuelve al mismo apartado con lo escrito.

**Me gusta y seguir:** **cualquier cuenta** (incluso sin compras: el dueño quiere comunidad desde antes de comprar); el freno contra cuentas falsas son los topes de abajo y los 10 registros por IP por hora; nadie se da me gusta a sí mismo; uno por perfil y se puede quitar; tope por día (30 por defecto, editable) y 20 acciones por minuto; a perfiles privados/ocultos no se puede (404, igual que la página). **No dan diamantes ni descuentos**: solo reputación. Reputación = me gusta ×1 + seguidores ×2 + insignias ×5 + meses en el top 3 ×20 + perfil completo 10.

**Top de perfiles** (`/perfiles/top`, público): pestañas *Este mes* (diamantes del mes), *Histórico* y *Reputación*, de 25 en 25, con avatar, nombre, nivel y número. Solo entran cuentas activas con perfil público, no oculto por el admin y con "Aparecer en el Top de perfiles" encendido. Con "mostrar mis estadísticas" apagado, la fila sale sin nivel ni cifra de diamantes. **Mi red** (`/mi-red`, solo cuentas): seguidores y siguiendo (solo perfiles visibles). No hay directorio ni buscador de perfiles.

**Cálculo (`utils/profile_social.py`):** los números se guardan en `profile_stats` y se recalculan solos cada 15 minutos cuando alguien abre un perfil o el top (`ensure_fresh`; un solo proceso a la vez con un candado), o con **"Recalcular ahora"** en los ajustes del admin. **Cierre de mes**: la primera vez solo fija el punto de partida (no regala premios de meses viejos); desde ahí, al empezar un mes se cierra el anterior (y los que falten, hasta 12): los 3 primeros reciben la insignia *Top 3 del mes* y un premio en la vitrina (el primero, "Campeón de <mes>" como título). Quien tenga "aparecer en el top" apagado no puede ganarlo.

**Tablas nuevas** (las crea sola `db.create_all()`; las columnas nuevas de tablas que ya existían las agrega `_ensure_profile_social_columns` en `app/__init__.py`): `profile_stats`, `profile_likes`, `profile_follows`, `user_badges`, `profile_awards`, `account_notices` (avisos de una sola vez para una cuenta); columnas `user_linked_ids.verified_at` y `user_profiles.status_text / show_stats / in_rankings`. Código: `utils/profile_social.py` (todo el cálculo y las reglas), `routes/profiles.py` (like/follow/top/mi red), `templates/profiles/` (`_scope.html` ya trae todo, `top.html`, `network.html`), admin en `routes/admin.py` (`account_award_*`, `account_social_settings`) y `templates/admin/profile_settings.html`.

**Qué NO se hizo de la Fase 1 del PDF (por las reglas del dueño):** mostrar el **apodo e ID de Free Fire** y las **firmas/comentarios** (Fase 2). De la Fase 2 solo se hizo el **producto favorito** (el dueño decidió no hacer el gráfico de 6 meses, que mostraría hábitos de compra); siguen sin hacer comparar perfiles, rareza en %, código de referido y más insignias, y la Fase 3 (puntos canjeables, tienda de cosméticos, liga mensual, misiones, clanes). **Decisión del dueño (2026-10-13):** el acceso "Con mi ID" se queda como está (sin pedir número de orden): la competencia no puede adivinar quién es el que más le compra.

**Perfiles de prueba (NO son parte del repo):** en la carpeta del dueño `Desktop\3srecargas_demo` hay `sembrar_demo.py` (crea ~100 cuentas `@demo.test` con contraseña `Demo-12345`, recargas de un juego oculto "[DEMO] Juego de prueba", niveles, insignias, me gusta, seguidores, premios, fotos y banners) y `quitar_demo.py` (los borra todos). Solo funcionan contra la base SQLite local y **se niegan a tocar PostgreSQL**. Sus recargas aparecen en Admin → Órdenes/Estadísticas: borrarlas antes de mirar ganancias reales o de copiar la base a otro sitio.

Pruebas (fuera del repo): `test_social.py` (~120 comprobaciones: verificación, cálculo, niveles, insignias, me gusta/seguir con todas sus reglas, top, cierre de mes, premios, ajustes del admin, privacidad) y `ui_social_test.js` (navegador real).

## Página de Iniciar sesión (historial de compras)

- **Fondo configurable**: en **Admin → Configuración → Fondos de los accesos** hay un campo "Fondo de *Iniciar sesión*" (clave `drawer_bg_login`). Si hay imagen se pone fija detrás de todo con un velo oscuro; sin imagen se ve como siempre.
- **Selector de producto**: el campo dice "Selecciona el producto que compraste". Con JavaScript el `<select>` se esconde (sigue siendo el que viaja en el formulario) y al tocar el campo sale una **burbuja de cristal** con los productos (imagen o inicial), con flechita hacia el campo, que se abre abajo o arriba según haya sitio, se cierra con X / tocando fuera / Escape y se maneja con las flechas del teclado (`static/js/bubble-picker.js`, estilos `nx-bubble-*` al final de `neon.css`). Si se envía sin elegir producto, la burbuja se abre sola.
- **Pestañas** *Con mi ID / Mi cuenta / Registrarse* (enlaces `/login`, `/login?tab=cuenta`, `/register`; con JavaScript cambian el panel sin recargar y un cuadro de cristal se desliza a la pestaña elegida; los campos de contraseña llevan "ojito"). Detalles en *Cuentas y perfiles*.
- **Botón Volver** (arriba de la tarjeta): regresa a la página anterior del sitio; si se entró directo, o la anterior es la misma página de login (después de un intento fallido), lleva a la tienda.
- **Entrar con Google** (aparte del ID):
  1. En Google Cloud: *APIs y servicios → Credenciales → Crear credenciales → ID de cliente de OAuth → Aplicación web*, y en *URI de redireccionamiento autorizados* pegar la dirección que muestra **Admin → Configuración → Entrar con Google (clientes)** (termina en `/login/google/callback`).
  2. Pegar el *Client ID* y el *Client secret* en esa sección y guardar (o ponerlos en el `.env` como `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`). El secreto nunca se vuelve a mostrar. Mientras falte una llave (o esté desmarcado "Mostrar el botón") el botón **no sale**.
  3. El cliente toca "Continuar con Google" (sale en las pestañas **Mi cuenta** y **Registrarse**), elige su cuenta y entra con su **correo verificado**; con Google la cuenta se crea sola la primera vez. Su perfil muestra las órdenes de los IDs que enlace **y** las hechas con ese correo (`Order.email`, de cualquier juego; no se reasigna ni se quita nada a nadie). Se guarda como cliente con `account_scope='google'` y `account_kind='google_email'`.
  - Código: `app/utils/google_login.py` (flujo de código OAuth/OpenID) y rutas `/login/google` y `/login/google/callback` en `routes/auth.py`. Seguridad: `state` aleatorio de un solo uso con vencimiento de 10 min, `nonce`, comprobación de `iss`, `aud`, `exp` y de que Google marque el correo como verificado, tope de 30 intentos por IP cada 10 min, destino `next` solo interno, y **nunca** entra como administrador aunque el correo sea el del admin. Pruebas: `test_login_google.py` (Google simulado, 70+ comprobaciones).

## Banner promocional con video

En **Admin → Configuración → Banner promocional** se puede subir una foto **o un video MP4 liviano**. El video se reproduce solo, **sin sonido y en bucle** (sin controles; en iPhone no pasa a pantalla completa) en el mismo recuadro de cristal 5:2 de la foto, y el link del banner sigue funcionando. Reglas (`save_promo_banner_video` en `routes/admin.py`): solo **MP4**, máximo **8 MB** (`PROMO_BANNER_VIDEO_MAX_MB`) y se comprueba que de verdad sea un MP4 (la caja `ftyp` al inicio), no solo que termine en `.mp4`; un video pesado se avisa al elegirlo en el navegador y el servidor lo vuelve a revisar. Se guarda en `uploads/branding/`; al cambiarlo por otra foto o video, o al marcar "Quitar banner promocional", se borra el archivo anterior. La tienda decide si pone `<video>` o `<img>` por la extensión (`.mp4`). Consejo: un video de 3-6 segundos a ~1000x220, sin audio y comprimido (H.264) pesa menos de 1 MB. Pruebas (fuera del repo): `test_banner_video.py` y `ui_banner_video_test.js`.

## App instalable (botón "Instalar app")

La tienda se puede instalar en la pantalla de inicio del celular (PWA). Piezas: `/manifest.webmanifest` (ruta en `main.py`: nombre, colores, pantalla completa, íconos), íconos en `static/pwa/` (192, 512, 512 *maskable* y el de iPhone de 180, generados del logo `favicon.png` sobre fondo negro), las etiquetas del `<head>` de `base.html`, el service worker `sw.js` (con un manejador `fetch` vacío que no cachea nada) y `static/js/pwa-install.js`. Hay dos botones con `data-pwa-install`: uno **en medio de la campanita y el menú** del header y otro dentro del **menú desplegable** ("Instalar app"). Salen ocultos y solo aparecen donde se puede instalar: en Android/Chrome/Edge cuando el navegador manda `beforeinstallprompt` (un toque abre el cuadro de instalación del propio navegador) y en iPhone/iPad siempre (Apple no deja instalar con un botón: se muestra una ventanita con los pasos *Compartir → Añadir a pantalla de inicio*). Si la web ya está instalada (abierta como app) o el navegador no puede instalarla, no se muestra nada. Para instalar hace falta HTTPS (en local solo funciona en `localhost`). Si cambias el logo, vuelve a generar los íconos de `static/pwa/`.

## Barra flotante de abajo (portada)

Íconos: estilo iPhone (como la barra de pestañas de iOS): **de línea** (1.6 px, extremos redondeados) en la barra y **rellenos** dentro del botón de cristal cuando el apartado está elegido. Cada `<svg>` de `index.html` lleva las dos versiones (`.ico-line` y `.ico-fill`) y el CSS muestra una u otra: casa, trofeo (o regalo si no hay ranking), audífonos con micrófono para Soporte, control de videojuego para Juegos y persona en círculo para Perfil. `dock.js` copia el ícono del apartado elegido al botón.

La portada (`index.html`) lleva una barra de cristal flotante abajo con cinco apartados iguales, en este orden: **Perfil, Ranking (o Canjear puntos si no hay ranking activo), Inicio (el del medio, donde empieza el botón), Juegos y Soporte**. El ícono de **Soporte parpadea en rojo siempre, una vez cada 2 segundos** (clase `nx-dock-blink`; con "reducir movimiento" queda rojo fijo). **Juegos** abre una ventana de cristal (`#nxGamesPopup` al final de `index.html`, estilos al final de `neon.css`) que sube desde la barra y lista, en este orden: *Mini Juegos* → Hordas de Diamantes; *Dinámicas* → Canjear Puntos, Recarga Acumulada, Sorteo Diario, Adivina el Número. Cada botón lleva a su página (`promos_bp.hordas_page`, `recarga_acumulada_page`, `sorteo_page`, `adivina_page`) y solo salen las que están activas (mismas condiciones `PROMO_AVAILABILITY` del menú). Canjear Puntos no tiene página propia: cierra la ventana y abre el mismo cuadro de puntos del menú (`#openPointsModalBtn`). Se cierra con la X, tocando fuera, con Escape o al elegir otro apartado de la barra. Perfil es un enlace a `/profile` (quien no ha iniciado sesión pasa por Iniciar sesión). En la portada se ocultan los botones flotantes de Soporte y de Tutorial (en las demás páginas siguen como siempre); la barra se esconde sola mientras se escribe en un campo (teclado abierto).

Un botón redondo de cristal marca el apartado elegido y **se queda ahí** (empieza en Inicio, que es el del medio). Es un disco transparente (deja ver el fondo, sin color) con un **aro de luz negro, blanco y rojo que gira solo** (6 s por vuelta; el aro y su destello son `::before`/`::after` con `animation: nx-dock-ring-spin`, se apaga con "reducir movimiento"). El hueco de la barra es un círculo con el mismo centro que el botón y 6 px más de radio (separación igual por todos lados) con los hombros suavizados. Al cambiar de apartado hace el efecto de gota: el botón se hunde en la barra donde estaba (sale) y, en el apartado nuevo, la barra se hincha como una gota, de la punta nace el botón y la gota se hunde en el hueco debajo (entra); no se desliza por la barra. Toda la lógica está en `static/js/dock.js`: la forma de la barra es una curva (`clip-path: path()` para el cristal y dos SVG para el borde brillante y la sombra) que se recalcula en cada cuadro; el CSS (sección "Barra flotante de abajo" al final de `neon.css`) solo pone colores y tamaños. Ranking y Puntos abren los mismos cuadros del menú (`openRankingModalBtn` / `openPointsModalBtn`) y **con esa ventana abierta la barra se queda a la vista**: `dock.js` vigila su `aria-hidden` y pone `is-over-modal` (z-index por encima del fondo oscuro) y `dock-over-modal` en el body (las ventanas dejan 104 px libres abajo); tocar otro apartado de la barra, o el mismo, cierra la ventana. Para ver un cuadro suelto de la animación en las pruebas: `nxDock.nxPreview(indiceDelApartado, milisegundos)`.

## Pestañas de categorías (portada)

Las pestañas Juegos / Tarjetas / Wallet (`.categories-inner` en `index.html`) van dentro de una barra con **el mismo cristal y la misma forma que las tarjetas como Player ID** (`.glass-panel`, esquinas de 16 px): solo texto, sin cuadros ni íconos por dentro. La categoría elegida va en blanco y más gruesa, con un cuadro de cristal por detrás que usa **exactamente** los valores de `.glass-panel` (fondo, desenfoque, borde de luz, sombras; esquinas de 11 px). Ese cuadro es UNO solo (`.cat-slider`) y **se desliza con transición** de una pestaña a la otra: `static/js/cat-slider.js` vigila el cambio de clase `active` que hace `main.js` y mueve el cuadro (la animación es CSS, ~0.4 s con un pequeño rebote). Sin ese script, la pestaña elegida lleva su propio cuadro. Si se cambia `.glass-panel`, hay que cambiar también la regla del cuadro en `neon.css`; `ui_dock_test.js` (sección K) compara los dos con el de Player ID.

## Aviso de pago: "Ya sé, no mostrar más" (48 horas)

El aviso "¡ATENCIÓN: LEE ESTO ANTES DE PAGAR!" (portada y página de pago) tiene debajo de "OK, ENTIENDO" el botón **YA SÉ, NO MOSTRAR MÁS** ("Volverá a aparecer en 48 horas"). Guarda en el navegador del cliente (`localStorage`, llave `nx_payment_warning_snooze_until`, la misma en las dos páginas) la hora actual + 48 h; mientras no pase ese momento el aviso no sale (se sigue directo con los pasos del pago, y con el video si lo hay) y al vencer vuelve a salir solo. "OK, ENTIENDO" no guarda nada. El aviso de Wallet (`#walletNoticeModal`) no cambió.

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
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | *(vacío)* | Llaves de "Entrar con Google" (alternativa a ponerlas en Configuración; las del panel mandan) |
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
- **Topes anti-abuso** (los cuentan `check_rate_limit` y `process_locks`, por IP): campanita 30/min si manda órdenes, fotos del chat 20/h, lectura de comprobantes del menú 5/min y 20/h, estado de recarga (el menú que acepta los 6 últimos dígitos de la referencia) 4/min y 20/h por IP y 20/min y 150/h entre todas las IP juntas, chats nuevos 3/h, 10 mensajes/min por chat y 30 respuestas de IA por chat y hora (pasado el tope atiende una persona), órdenes nuevas 10/min y 60/h por IP, completar un pago 12/h por orden y 40/h por IP, y la clave del admin se frena sola (60 claves equivocadas en 15 min sumando todas las IP). La IP sale de `client_ip()` (`app/utils/locks.py`): toma el ÚLTIMO valor de `X-Forwarded-For` (el que agrega nginx), no el primero, que lo escribe el visitante. Con `proxy_set_header X-Forwarded-For $remote_addr;` (recomendado) o con `$proxy_add_x_forwarded_for` funciona igual; si además hay Cloudflare delante, pon `TRUSTED_PROXY_COUNT=2` en el `.env`.
- **Panel admin:** todas las rutas `/admin/...` rechazan a un visitante y a un cliente con sesión de cliente (hay un guardia por blueprint; `admin_support_bp` se apoya además en `@login_required` en cada ruta).
- **Cuentas de clientes:** contraseñas con hash, mensajes de error que no revelan quién tiene cuenta, topes por IP y por cuenta (login, registro, cambios de contraseña/correo), editar solo con cuenta (el perfil por ID es de solo lectura) y rutas `/admin/cuentas...` cubiertas por el mismo guardia del panel. Detalle en *Cuentas y perfiles*.
- Las páginas y respuestas JSON llevan `X-Content-Type-Options: nosniff`. Las páginas HTML llevan además `X-Frame-Options: SAMEORIGIN` y `Content-Security-Policy: base-uri 'self'; object-src 'none'; frame-ancestors 'self'` (nadie mete la tienda en otra página ni cambia la base de los enlaces) y `Referrer-Policy: strict-origin-when-cross-origin`. *No* hay una CSP estricta de scripts (la tienda usa muchos `<script>` y `onclick` en línea); la defensa es escapar todo y probarlo (abajo).
- **Textos dentro de `onclick="..."` (auditoría 2026-10-13):** `'{{ nombre }}'` dentro de un atributo **no es seguro** (el navegador vuelve a convertir `&#39;` en `'` antes de ejecutar el JS: un nombre con comilla rompe la cadena y ejecuta código; pasaba en `admin/minis.html` con el nombre que escribe cualquiera al registrarse como mini). Regla: dentro de `onclick`/`onsubmit` los textos van con el filtro **`|jsattr`** (`onclick="abrir({{ nombre|jsattr }})"`, sin comillas alrededor; para frases: `confirm({{ ('¿Eliminar ' ~ nombre ~ '?')|jsattr }})`). Los enlaces que escribe una persona o un ajuste van con **`|safe_href`** (solo `http(s)`, `mailto`, `tel` o ruta del sitio; un `javascript:` se vuelve `#`). Están aplicados en todo el admin, la barra de redes, el banner y los links de minis.
- **Filtro de IDs en la puerta** (`_reject_hostile_ids` en `app/__init__.py`): en TODAS las rutas públicas, un `player_id` / `identifier` / `zone_id` / `lookup_identifier` / `link_identifier` con comillas, `<`, `>`, `` ` ``, barra invertida, caracteres de control/invisibles o de más de 150 letras se rechaza (JSON 400 en las llamadas internas; en los formularios de cuenta, vuelta a la misma página con aviso). Un ID de verdad nunca los lleva. El checkout además limpia teléfono (solo dígitos y `+-().`) y correo, y recorta la zona.
- **Registro de minis:** el nombre del canal ya no admite `<` `>` ni caracteres de control y el link se guarda siempre como `http(s)` (un `javascript:x` queda `https://javascript:x`).
- **Subidas de clientes** (`utils/upload_guard.py`): un "comprobante.png" o adjunto del chat que por dentro empieza como HTML/SVG/XML (`<`) se rechaza; las imágenes de verdad se guardan tal cual.
- **Entradas raras no rompen nada:** `create_app` usa una clase de petición (`_JsonObjectRequest`) cuyo `get_json(silent=True)` devuelve `None` si el cuerpo JSON no es un objeto `{...}` (un texto suelto, un número o una lista rompían ~40 rutas con error 500) y hay un manejador de `OverflowError` (un ID de 25 cifras no cabe en la base: responde 400 en vez de 500). `fuzz_posts.py` (3.000 peticiones basura a todos los POST como visitante, cuenta y admin, con la red externa bloqueada) y `fuzz_gets.py` (valores absurdos en las rutas) terminan con 0 errores.
- **Pruebas de esta auditoría (fuera del repo):** `test_taint.py` (envenena TODAS las columnas de texto de una copia de la base y abre ~120 páginas como visitante, cuenta y admin, también con la carga en la barra de direcciones), `ui_taint_test.js` (lo mismo en Chrome real con ~750 clics y un control positivo), `test_ingress.py` (cargas por los formularios reales, permisos del admin —336 peticiones sin ser admin—, IDOR, redirecciones, subidas, cabeceras) y `scan_templates.py` / `scan_js_sinks.py` (barrido estático de plantillas y JS).

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
