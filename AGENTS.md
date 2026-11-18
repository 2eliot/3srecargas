# AGENTS.md — Contexto operativo de 3S Recargas

> **Origen de este archivo:** lo creó OpenCode el **17-nov-2026** después de leer los dos historiales locales de Claude Code de este proyecto (`~/.claude/projects/c--Users-reals-Desktop-3srecargas/`): 1.333 mensajes del dueño (849 únicos) y 4.865 respuestas, entre el **22-sep-2026** y el **16-nov-2026**, más las **71 notas de memoria** de `~/.claude/projects/c--Users-reals-Desktop-3srecargas/memory/`.
>
> Si hay conflicto entre este archivo y el estado real del repo, **manda el repo**. Verifica con `git status`, `git log` y el código antes de actuar.
>
> Los historiales completos y las notas están fuera del repo. No se copiaron aquí para no meter datos operativos en Git. En una sesión nueva, lee primero `memory/MEMORY.md` y la nota que corresponda al tema.

---

## 0. Resumen en 10 líneas

- **3S Recargas** es una tienda Flask de recargas de juegos (Free Fire, Blood Strike, Mobile Legends, Telegram, tarjetas, Wallet…) con panel admin, cuentas/perfiles, puntos, chat de soporte, programa de creadores y minijuegos.
- El dueño es **Sebastián** (`realsebastian28@gmail.com`), habla solo español, no es programador y trabaja con **capturas de pantalla** y pedidos cortos.
- Producción: **https://3srecargas.com** y **https://3srecargas.net**, en un VPS Hostinger; PostgreSQL en otro servidor por túnel SSH.
- Repo: **github.com/2eliot/3srecargas**. La rama de trabajo real es **`fix/recarga-acumulada-seguridad`**; en el último estado está igual a `origin/main`.
- Último commit desplegado: **`5ea3a90`** (18-nov-2026): acciones masivas de órdenes + arreglo de Central One + contexto para agentes.
- **Todo está commiteado y desplegado.** `central_one.py` quedó incluido en `5ea3a90`; no hay cambios de código pendientes.
- Desarrollo local en Windows: **puerto 5001**, SQLite `data/app.db`. El puerto 5000 es OTRO proyecto.
- La web es **mobile-first**: en PC debe verse como un celular, salvo el panel admin.
- Regla de oro: **no tocar otros proyectos** del dueño (3sticket, 3srevendedores, etc.) ni mezclar sus reglas.
- Cuando dice **“terminamos”** = commit local; **“sube todo”** = commit + push + desplegar en el VPS; **“link para Eliot”** = repo + commit en dos líneas.

---

## 1. Cómo trabajar con el dueño

- **Idioma:** siempre español. Tono claro, directo y no técnico. Él escribe con errores y sin ñ porque usa teclado US; no se los corrijas en la respuesta ni los copies.
- **Capturas:** su forma principal de especificar es un pantallazo del admin o de la tienda. Mira **qué parte de la pantalla está señalando** antes de asumir otra cosa.
- **Cambios pequeños y rápidos:** pide “más grande”, “centrado”, “más abajo”, “que no se vea pegado”. Haz exactamente eso, sin rediseñar de más.
- **Si dice “no lo veo”:** no asumas caché. Mira su captura y comprueba que el control esté visible **sin hacer scroll** (scrollY 0). Los controles importantes van arriba, junto al estado o título de lo que controlan.
- **“Esto es solo pregunta” / “no hagas nada” / “avísame antes”:** investiga en modo lectura y responde; **no edites código** hasta que lo pida.
- **No lances agentes, auditorías ni revisiones completas** mientras él sigue pidiendo cambios. Solo pruebas normales. Los agentes se activan cuando dice “terminamos” o lo pide expresamente.
- **No le pidas contraseñas ni llaves por chat.** No guardes contraseñas en memoria ni en archivos del repo.
- **No mezcles proyectos.** Si un pantallazo o un texto no aparece en este código, pregunta “¿esto es de 3S Recargas o de otro proyecto?” antes de investigar.
- **Precios y datos vivos:** él edita precios de paquetes a mano en el admin. No los sobrescribas en un despliegue ni en un script.
- **Cuando dudes entre dos interpretaciones:** elige la más conservadora o pregunta. Ya ha pasado que una coma mal leída puso “Juegos” donde iba “Ranking”.

---

## 2. Frases del dueño y qué significan

| Él dice | Qué hacer |
|---|---|
| “abre / levanta / activa el server” | Arrancar el servidor local en **5001** (ver §5). Verificar `curl http://127.0.0.1:5001/api/rankings` → 200. |
| “terminamos, haz lo de siempre” | `git status` / `git diff`, commit **solo** de los archivos tocados en la sesión, mensaje en español estilo `feat: ...` o `fix: ...`. **No push**. |
| “sube todo”, “sube a la web”, “sube al vps” | Commit + push a `main` y a la rama + desplegar al VPS + verificar. Si ya está todo commiteado, solo push/deploy. |
| “sube todo y dame el link para Eliot” | Todo lo anterior y responder exactamente: `Repo: https://github.com/2eliot/3srecargas` y `Commit actual: https://github.com/2eliot/3srecargas/commit/<hash completo de 40>` (dos líneas, hash completo). |
| “¿subiste?” | Verificar de verdad (git y web); no responder solo porque se hizo el commit. |
| “esto es solo pregunta” | Responder sin tocar nada. |
| “avísame antes” | Revisar solo lectura, explicar y esperar su decisión. |
| “no me gustó, borra todo lo que hiciste” | Revertir **solo** lo de esa petición y verificar que quedó byte a byte como antes. |
| “ya lo hice / yo lo hago manual” | No repetir la acción; dejar registro y seguir. |

---

## 3. Datos del proyecto

### Stack

- **Flask 3** + **SQLAlchemy 2** + **Flask-Login** + Jinja2.
- Producción: **PostgreSQL**; local: **SQLite** (`data/app.db`, ignorado por git).
- Frontend: HTML/Jinja + CSS propio (`neon.css`, `main.css`, `profile.css`, `admin.css`) + JS vanilla. Sin build step.
- PWA instalable (`/manifest.webmanifest`, `sw.js`), notificaciones push y campana.
- IA: Gemini para leer comprobantes; DeepSeek/Gemini/otra para el asistente de soporte (la IA de soporte está **APAGADA** en producción según la última memoria).

### URLs y servidor

| Qué | Valor |
|---|---|
| Dominios | `3srecargas.com`, `3srecargas.net` (Cloudflare) |
| VPS | `177.7.62.94` (Hostinger KVM 2, Boston, Ubuntu 26.04) |
| Acceso real | `ssh -i ~/.ssh/id_ed25519_3s_webs -o IdentitiesOnly=yes tresrecargas@177.7.62.94` |
| App | `/home/apps/web-c-3srecargas` (no es repo git; el código se copia) |
| Servicio | `web-c-3srecargas.service` (systemd) → gunicorn `127.0.0.1:5002` |
| Base de datos | PostgreSQL en `177.7.62.54`, vía túnel SSH local `127.0.0.1:6432`, base `tresrecargas` |
| Túnel | Servicio `tunel-db` del VPS. **No tocarlo ni cambiar el puerto.** |
| Uploads | `app/static/uploads/` (varios GB de comprobantes reales). **Nunca borrar ni rsync --delete.** |

> **Ojo:** `CLAUDE.md` (creado por Eliot) está desactualizado en el acceso: dice `root` y una llave en `C:\Users\user\.ssh\id_ed25519`. El acceso que funciona es el de la tabla: usuario `tresrecargas` y llave `id_ed25519_3s_webs`. No cambies la llave ni pidas la contraseña.

### Requisitos en el servidor

- Python **3.14** en producción. `requirements.txt` del repo no compila tal cual.
- En el servidor existe **`requirements.migracion.txt`**:
  - `Pillow>=11.0`
  - `psycopg2-binary>=2.9.10`
  - `SQLAlchemy==2.0.54` **fijado** (2.1 cambia a psycopg v3 y rompe con `ModuleNotFoundError: psycopg`).
- Si agregas dependencias, actualiza también ese archivo en el servidor.

---

## 4. Estado actual (18-nov-2026)

- **Rama actual:** `fix/recarga-acumulada-seguridad`; el código desplegado es `5ea3a90` (este archivo puede ir un commit de documentación después).
- `origin/main` y `origin/fix/...` deben estar iguales; el último deploy de **código** es `5ea3a90`. Este archivo se actualiza con un commit de documentación aparte. El puntero local `main` suele estar atrás: no lo uses como referencia sin actualizarlo.
- **Último despliegue:** 18-nov-2026 00:05 UTC, commit `5ea3a90`, acciones masivas + Central One. Respaldos `backups-deploy/db-2026-11-18-0003.dump` y `code-2026-11-18-0003.tgz`.
- **Central One (incluido en `5ea3a90`):** distingue una recarga directa (`requires_target`) de un código de tarjeta, así no deja 23 órdenes esperando al admin. Prueba local `test_co_direct_topup.py` 11/11. **Ojo:** no volver a mezclarlo sin querer en commits de otros temas; ya está desplegado.
- **Verificación de ID de Free Fire:** `ff_verify_enabled = 1` (ENCENDIDA) al 15-nov. No la apagues ni la enciendas sin que él lo pida.
- **Binance:** método activo; verificación por referencia con ID completo y ventana de 48 h antes de la orden. En el `.env` del servidor se comentó `BINANCE_PROXY` el 15-nov porque el proxy estaba muerto; Binance va directo. En el `.env` local la variable todavía aparece: si pruebas Binance local y falla por proxy, coméntala también.
- **Asistente de IA de soporte:** construido y con frenos de gasto, pero **APAGADO** en producción (sin proveedor/instrucciones activas). No lo enciendas sin que él lo pida.
- **Clave del admin:** cambiada el 15-nov (16 caracteres generados en el servidor, entregados en el chat). No está guardada en ningún archivo; no la inventes ni la repitas.
- **Meta Pixel:** dos IDs activos (`1893616271596771` y `1480049273980368`) en páginas públicas. Solo PageView; el evento Purchase **no** está implementado (se le ofreció y no respondió).
- **17/18-nov-2026 (DEPLOYED `5ea3a90`):** acciones masivas en Admin → Órdenes: seleccionar varias pendientes y aprobar (máx. 10 por lote) o rechazar (hasta 50) con un motivo común. La selección no se pierde con el polling y pausa el auto-refresco de 45 s. Ver §7 y §9.

---

## 5. Servidor local (Windows)

Comando real del dueño (ya autorizado en `.claude/settings.local.json`):

```powershell
cd C:\Users\reals\Desktop\3srecargas
.\venv\Scripts\python.exe -c "from app import create_app; create_app().run(debug=True, host='0.0.0.0', port=5001)"
```

- **Puerto 5001.** El 5000 es **3sRevendedores**, otro proyecto: nunca lo mates ni lo reutilices.
- `run.py`, `start_server.bat` e `iniciar_dev.bat` dicen 5000: están viejos; el dueño usa 5001.
- El dueño también entra desde el celular por la IP LAN de la PC en `:5001`.
- Cambios de Python: el reloader reinicia solo. Cambios de plantilla/CSS: hard refresh (`Ctrl+Shift+R`) o cerrar/reabrir la pestaña; el `static_v` cachea por proceso y a veces requiere reiniciar.
- La base local es la real de desarrollo (`data/app.db`) y los uploads son reales. No dejes datos de prueba.

---

## 6. Despliegue al VPS

**Nunca editar código directamente en el servidor.** Flujo: editar local → probar → commit → push → copiar → migrar si hace falta → reiniciar → verificar.

### Receta que funcionó (reconstruir si el scratchpad ya no está)

1. **Comprobar que el servidor está igual al commit anterior** (hashes por archivo, ignorando CRLF). Si hay diferencias reales, investigar antes de copiar.
2. **Respaldar** en `/home/apps/web-c-3srecargas/backups-deploy/`:
   - Base: `pg_dump "$DB" -Fc` → `db-AAAA-MM-DD-HHMM.dump`
   - Código: tar.gz sin `venv`, sin `uploads`, sin `.gunicorn` y sin `.env.bak-emergencia` → `code-AAAA-MM-DD-HHMM.tgz`
3. **Copiar solo los archivos cambiados** desde el commit (por `git show` o `scp`), a una carpeta staging, y verificar `sha256sum -c`. No usar `git archive` a través del tar de Windows: alteró archivos en el pasado.
4. **Migrar** si hay tablas/columnas nuevas: ejecutar **una sola vez** `create_app()` en el servidor **antes** de reiniciar (evita carreras entre los 3 workers de gunicorn). Las columnas nuevas suelen crearse con las funciones `_ensure_*`; las tablas nuevas con `db.create_all()`.
5. **Aplicar** con `rsync -a staging/app/ app/` (**sin `--delete`**) y ajustar dueño.
6. **Reiniciar:** `sudo -n systemctl restart web-c-3srecargas.service`.
7. **Verificar:**
   - `systemctl is-active` → active.
   - Páginas públicas y admin → 200.
   - Rutas nuevas sin sesión → 302 al login.
   - `sudo -n journalctl -u web-c-3srecargas.service` → 0 tracebacks. Los errores `[BS] API error: USER ID não existe` son IDs inválidos de clientes, no fallos del deploy.
   - Comparar hashes de los archivos desplegados con el commit.
8. **Rollback:** descomprimir el `code-*.tgz` en la app y reiniciar. Los cambios de base son aditivos.

### Notas de despliegue

- El servidor es **compartido con otros proyectos**. Toca solo `/home/apps/web-c-3srecargas`.
- El usuario de la app solo puede `sudo -n systemctl restart|start|stop web-c-3srecargas.service` y `sudo -n journalctl -u ...` (no `status --no-pager`).
- nginx ya sobrescribe `X-Forwarded-For $remote_addr` y restaura la IP real de Cloudflare; los topes por IP funcionan.
- `uploads/` es sagrado: nunca `rsync --delete`.
- CRLF: el repo usa LF y el servidor puede tener CRLF. Un hash distinto por saltos de línea **no** es un cambio real; compara con `tr -d '\r'`.
- Git: Eliot a veces commitea/pushea directo a `origin/main` y copia a mano al servidor. Antes de pushear: `git fetch`, `git rev-list --left-right --count origin/main...HEAD`, y si hay commits de Eliot, fusionar primero.
- Si el dueño dice “sube todo”, se sube **git + web**, aunque él solo mencione una. Es la rutina confirmada.

---

## 7. Mapa funcional del código

### Rutas (blueprints en `app/routes/`)

| Archivo | Área |
|---|---|
| `main.py` | Tienda, `/api/games`, `/api/packages`, rankings, notificaciones, validación de cupones |
| `checkout.py` | Compra en 2 pasos, verificación de pago, completar pago, correos, puntos |
| `admin.py` | Panel admin: juegos, paquetes, órdenes, pagos, usuarios, settings, soporte, minis, estadísticas |
| `auth.py` | Login por ID (sin contraseña), cuentas, registro, Google, admin |
| `profiles.py` | Perfiles públicos `/u/<usuario>`, editor, Top, Mi red |
| `points_shop.py` | Tienda de puntos de cuentas `/tiendapuntos` |
| `admin_points_shop.py` | Admin de la tienda de puntos |
| `minis.py` | Panel de creadores (“Código de creador”) y retiros |
| `support.py` | Chat del cliente |
| `admin_support.py` | Bandeja y chat del admin, respuestas rápidas, asistente IA |
| `promos.py` | Sorteo, Adivina, Hordas, Recarga acumulada, Torneos, API de promo |
| `admin_hordas.py` | Admin de Hordas por cuenta |
| `admin_tournaments.py` | Admin de torneos |
| `redeem.py` | Canje de códigos/PINs |
| `verify.py` | Verificación de ID de jugador (Revendedores, Blood Strike, Free Fire) |
| `affiliates.py` | Cupones y afiliados |
| `revendedores_webhook.py` | Webhook del proveedor |

### Utilidades clave (`app/utils/`)

- **Pagos:** `payment_verification.py` (Pabilo/banco), `binance_reference.py`, `binance_pay.py`, `payment_flags.py`, `order_states.py`, `receipt_check.py`, `reference_extraction.py`.
- **Órdenes/proveedores:** `order_processing.py`, `central_one.py`, `order_pause.py`, `order_refunds.py`, `package_costs.py`, `order_scheduler.py`, `availability.py`.
- **Cuentas/perfiles:** `auth_accounts.py`, `profiles.py`, `profile_social.py`, `profile_xp.py`, `profile_frames.py`, `profile_shop.py`, `ranking_boards.py`, `account_points.py`, `points.py`, `points_shop.py`, `official_profile.py`, `google_login.py`, `google_link.py`.
- **Soporte:** `support.py`, `support_bot.py`, `support_ai.py`, `site_notifications.py`, `notifications.py`, `push_notifications.py`.
- **Juegos:** `hordas.py`, `promos.py`, `minigames.py`, `tournaments.py`.
- **Contenido/UI:** `package_sections.py`, `player_field.py`, `zone_rules.py`, `page_bg.py`, `banner_lite.py`, `image_optimize.py`, `web_perf.py`.
- **Seguridad:** `upload_guard.py`, `contact_filter.py`, `locks.py`, `order_access.py`, `totp.py` (Google Authenticator), `admin_login_guard.py` (3 intentos + 1/5 min), `order_blocks.py` (strikes por IP), `email_domains.py` (correos permitidos).
- **Creadores:** `mini_influencers.py`.

### Modelos principales (`app/models.py`)

`Game`, `Service`, `Package`, `Order`, `PaymentMethod`, `Setting`, `User`, `UserProfile`, `PlayerPoints`, `AccountPoints`, `RankingBoard`, `RankingArchive`, `SupportChat`, `SupportMessage`, `SupportTag`, `SupportQuickReply`, `Affiliate`, `AffiliateWithdrawal`, `MiniRank`, `MiniNotification`, `Tournament*`, `PromoHorde*`, `HordeAccount*`, `PointsShopItem`, `PointsShopOrder`, `UserShopItem`, `OrderRefund`, `UserGoogleLink`.

### Funcionalidades vivas (mapa rápido)

- **Tienda:** juegos, paquetes por secciones (Moneda/Pases/Tarjetas + apartados propios), precios USD/Bs, tasa, cupones, afiliados, aviso de pago, tutorial, banner, dock, modo liviano, PWA, Meta Pixel.
- **Checkout:** pago primero, orden después; comprobante con IA; referencia manual; pago incompleto → completar restante; pago ya usado/repetido; pausa; rechazo con motivo; devolución; forzar aprobación; cambio de paquete antes de aprobar.
- **Proveedores:** Revendedores (principal) + Central One (respaldo por paquete). Respaldo automático solo si el principal confirma que no entregó nada. Central One entrega recargas directas o códigos según `requires_target`.
- **Cuentas:** cuentas independientes de los IDs; el ID conserva su historial; la cuenta acumula sus compras (`Order.account_user_id`). Entrada por ID sigue sin contraseña y es solo lectura.
- **Perfiles:** públicos estilo Discord, 5 imágenes propias máximo, marcos/fondos/fotos, niveles numéricos, reputación, insignias, Top, Mi red, privacidad estricta (sin IDs, compras, juegos, redes ni comentarios).
- **Puntos:** puntos por ID (ruleta/canje) y bolsa de cuenta (tienda `/tiendapuntos`; recargas, marcos, fondos, fotos, ruleta, nivel/reputación). Puntos de compra del Top = `round(USD × 50)`.
- **Soporte:** chat en vivo, bandeja admin, compra dentro del chat, comprobante leído por Gemini, etiquetas, respuestas rápidas, campana, IA con límites (apagada).
- **Creadores:** antes “minis”; ahora solo código de creador y escalera de rangos con bonos automáticos al saldo; retiros con datos de cobro y comprobante.
- **Juegos:** Hordas de Diamantes por cuenta (recompensas en puntos, aprobación manual), Sorteo Diario, Adivina el Número, Recarga Acumulada, Ruleta, Torneos con salas y premios automáticos.
- **Admin:** dashboard, órdenes (con selección múltiple y acciones masivas: aprobar máx. 10 / rechazar hasta 50, solo pendientes), ediciones, dinámicas, ranking editable, creadores, soporte, estadísticas, configuración, mantenimiento.

---

## 8. Reglas fijas del dueño (no renegociar sin que él lo pida)

1. **No tocar 3sticket ni ningún otro proyecto.** Si un pantallazo no coincide con este código, preguntar de qué proyecto es. Nunca buscar ni modificar los otros proyectos desde aquí.
2. **Los ceros de una referencia cuentan.** Se toman los últimos 6 caracteres; mínimo 6; solo se rechaza una referencia totalmente en ceros. No importar reglas de 3sticket.
3. **Al cliente rechazado se le muestra SOLO `orders.reject_reason`.** Nunca `orders.notes` (contiene Pabilo, proveedores y costos internos).
4. **Perfil público:** prohibido mostrar redes, links, comentarios, IDs de jugador, compras, cantidad de órdenes o juegos. Enmascarar correos, teléfonos, @handles y números de 6+ dígitos.
5. **La entrada por ID sin contraseña se queda.** Es solo lectura. No proponer contraseñas/códigos para ver el historial por ID.
6. **Web mobile-first.** En PC, las páginas de cliente se ven como un celular (columna angosta); el admin es la excepción.
7. **Controles importantes arriba**, junto al estado/título, visibles sin scroll. Si dice “no lo veo”, revisar su captura antes de culpar a la caché.
8. **No rehacer la fila de juegos “5+ / Ver más”.** La rechazó expresamente. Si vuelve a pedirla, pedir su dibujo y probar 390/800/1280 px.
9. **No encender/apagar la verificación de ID de Free Fire** sin que lo pida. Él la controla desde Admin → Configuración.
10. **No restaurar el respaldo de Central One** que él quitó a propósito (31-oct, mantenimiento). Si Central One vuelve a fallar, preguntar antes de tocar mapeos.
11. **No sobrescribir precios ni costos** que él editó en el admin. Los scripts de imágenes solo deben tocar la columna `image`.
12. **No mostrar datos de pago ni instrucciones internas al cliente** en la página de la orden.
13. **No meter `central_one.py` en commits de otros temas.** Es un cambio de otra sesión.
14. **No guardar contraseñas** en memoria, AGENTS.md ni git. La del admin vive en el `.env` del servidor.
15. **No correr auditorías/agentes** hasta que diga “terminamos” o lo pida.
16. **No mezclar “prueba” con producción:** los tests no deben escribir en `data/app.db` real ni en la base de producción.
17. **Acciones masivas de órdenes:** solo aplican a órdenes `pending`; una completada/devuelta/rechazada se omite. Aprobar va en lotes de máximo 10 (cada aprobación puede consultar al proveedor); rechazar hasta 50. No quitar esas validaciones del servidor aunque el JS se vea bien.
18. **Crear órdenes:** máximo **3 por minuto y 30 por hora por IP** (`ORDERS_PER_IP_PER_MINUTE` / `ORDERS_PER_IP_PER_HOUR` en `routes/checkout.py`). No subirlos sin que él lo pida. El tope se cuenta al confirmar la orden, después de validar correo/zona/ID.
19. **Admin con Google Authenticator:** si `totp_enabled` está activo, **siempre** se pide el código; `ADMIN_TOTP_REQUIRED=0` solo evita la inscripción obligatoria. No borrar `totp_secret`, `totp_recovery_hashes` ni el sello `admin_2fa_at` de la sesión. Teléfono perdido: `scripts/reset_admin_totp.py` en el servidor. El panel (incluidos soporte, torneos, hordas y puntos) queda cerrado hasta configurarlo.
20. **Correos de clientes:** solo proveedores reconocidos (Gmail, Outlook/Hotmail, Yahoo, iCloud, etc.) y los dominios que el admin agregue en **Admin → Seguridad**. `example.com`, los de prueba y los temporales **nunca** se aceptan, ni siquiera agregándolos. Aplica a compras y canje de códigos.
21. **Strikes por IP:** **5 órdenes rechazadas en 24 h** bloquean esa IP **24 h solo para crear órdenes**; navegar, ver órdenes y soporte siguen abiertos. Se cuenta por `orders.rejected_at` (no por `updated_at`) y solo órdenes con `client_ip`. Se libera o se apaga en **Admin → Seguridad**. No bloquear el canje de códigos de regalo.
22. **IP y user-agent:** se guardan en cada orden nueva (`orders.client_ip`, `orders.user_agent`) y solo los ve el admin en el detalle. No mostrarlos al cliente ni meterlos en correos.
23. **fail2ban:** el jail viene **desactivado** y es una capa extra; la app ya bloquea sola. Activarlo requiere root, el ID de zona y un token de Cloudflare con permiso de Firewall/WAF. Validar con `fail2ban-client -t` antes de reiniciar; no tumbar los jails de SSH.

---

## 9. Pruebas: trampas conocidas

- **`config.py` hace `load_dotenv(override=True)`**: el `.env` del repo pisa cualquier `DATABASE_URL` que pases por entorno. Muchos tests con `create_app()` escriben en la base local real. Para regresiones, usa una **copia limpia sin `.env`** y `REPO_OVERRIDE` (el harness vivía en el scratchpad de la sesión, no en el repo).
- **SQLite en WAL:** una copia con `shutil.copy` puede perder lo reciente. Usa `sqlite3` con `backup()`.
- **Rate limits pegados:** `api_rate_limit_hits` guarda contadores (`complete_payment_%`, `hordas_%`, etc.). Si un test falla con “Demasiados intentos”, borra esas filas de la copia.
- **Puertos de Chrome/Node:** 5060 y 5061 están bloqueados; usa 509x.
- **`/api/packages` exige `X-Page-Day`**: sin header o con header vacío puede devolver “página vencida”. En curl, un header vacío se manda con `-H "X-Page-Day;"`.
- **CRLF:** comparaciones de hash dan falso negativo. Compara ignorando `\r`.
- **Tests de UI** abren Chrome real con CDP y capturas; no están en el repo. Si no existen, créelos en una carpeta temporal, no los commitees.
- **No dejes datos de prueba** en la base local: revisa y limpia después de un test que use `create_app()` real.
- **XSS:** en plantillas usa `|jsattr` dentro de `onclick/onsubmit`, `|safe_href` para links de usuarios/settings, y `textContent`/escape antes de `innerHTML`. `|tojson` solo en atributos entre comillas simples o `<script>`.
- **IDs hostiles:** `_reject_hostile_ids` bloquea comillas, `<>`, backticks, barras invertidas y controles en IDs. No lo desactives.
- **CSRF:** no hay tokens; la app se apoya en `SameSite=Lax`. No agregues formularios cross-site.
- **2FA del admin en pruebas:** el secreto TOTP vive en `admin_users.totp_secret` (no en la cookie). Genera el código con `app.utils.totp.generate_code(secret)` y usa `session_stamp_valid`/`stamp_session` para el sello `admin_2fa_at`. `qrcode[pil]==8.2` debe estar instalado; sin él el QR sale vacío pero la clave manual funciona.
- **Scripts de mantenimiento:** `scripts/reset_admin_totp.py` pone `DISABLE_SCHEDULER=1` para no quedarse con el liderazgo del scheduler. Si corres `create_app()` a mano en el servidor para migrar, usa también `DISABLE_SCHEDULER=1`.
- **Columnas/tablas nuevas de esta tanda:** `orders.rejected_at`, `orders.client_ip`, `orders.user_agent`, columnas TOTP en `admin_users`, tablas `ip_order_blocks` y `admin_login_attempts`, índice `ix_orders_client_ip_status (client_ip, status, rejected_at)`. La migración corre en `create_app()`; en producción hay que ejecutarla **una vez antes** de reiniciar.

---

## 10. Pendientes y propuestas NO construidas (al 17-nov-2026)

> Casi todas esperan una decisión o un “sí” del dueño. No las construyas por iniciativa propia.

1. **Central One:** el arreglo ya está desplegado en `5ea3a90`; no queda pendiente.
2. **Free Fire “No encontramos ese ID” al comprar:** diagnóstico hecho; propuesta de recordar la verificación en la sesión y tratar “200 sin ok” como reintento. **No construida.**
3. **Pedidos manuales → chat de soporte:** falta que él responda 4 preguntas (verificación previa, quién escribe el mensaje, texto/datos, si aplica a compras dentro del chat). **No construido.**
4. **Aviso de margen bajo en costos automáticos:** el costo ya se actualiza solo en Free Fire/Mobile Legends/Blood Strike; falta el aviso y el botón de aplicar precio. **No construido** (no eligió monto mínimo).
5. **“Paquete N no encontrado o inactivo”:** el proveedor se quedó sin saldo 2 veces; se destrabaron 21 y 37 órdenes a mano. Falta el cambio de código para tratarlo como temporal y que “Aprobar” reintente. **No construido** (espera su sí).
6. **Intrusos en salas de torneo:** la inscripción es segura; el hueco es que ID+clave salen a quien escriba un ID inscrito. Propuestas (1) registrar consultas por conexión y marcar, (2) llave por teléfono. **No construidas.**
7. **Rechazar + reutilizar pago = doble entrega:** 2 casos reales en 45 días. Propuestas de consultar al proveedor antes de rechazar / no liberar el pago. **No construidas.**
8. **“Pago ya usado” con los mismos últimos 6 dígitos:** Pabilo devuelve un solo pago por el final. Propuestas de mostrar la referencia completa del banco y el número de orden con enlace. **No construidas.**
9. **Código de descuento que desaparece antes de confirmar:** diagnóstico con 2 casos; propuesta de guardar el motivo del descarte/avisar si el total cambió. **No construida.**
10. **Avatar vestible:** idea de avatar por capas con ropa comprada con puntos; Hordas es vista desde arriba y necesita arte propio. **Nada construido**; espera decisiones y artista.
11. **Enmascarar mejor el ID en rankings públicos:** hoy muestra 4 caracteres + `****`; se propuso 2 letras. **Sin respuesta.**
12. **Evento Purchase del Meta Pixel:** solo hay PageView. **No implementado** (se le ofreció).
13. **Probar en su celular real** el arreglo de la fila de juegos (`touch-action`) y el modo liviano. **Pendiente de su prueba.**
14. **Código viejo de Hordas por ID:** dormido; se puede borrar cuando él confirme. Los 3 ganadores viejos de Blood Strike se pagaron a mano.
15. **Renombrar/borrar/reordenar apartados propios de paquetes:** no hay UI; para quitar uno hay que reasignar sus paquetes.
16. **Botón “Cambiar clave” en el admin:** no construido; hoy se cambia en el `.env` del servidor con script y reinicio.
17. **Optimizaciones de imágenes/Cloudflare:** ideas ofrecidas (redimensionar imágenes de paquetes, cachear solo uploads públicos). **No hechas.**
18. **Activar el jail de fail2ban:** los archivos están en `deploy/fail2ban/`, pero el jail viene **desactivado**. Para activarlo hace falta root, el ID de zona y un token de Cloudflare con permiso de Firewall/WAF. La app ya bloquea por sí sola; no es urgente.
19. **Revisar los correos permitidos después de unos días:** la lista incluye Gmail, Outlook/Hotmail, Yahoo, iCloud, Cantv y otros ISP; si un cliente legítimo con correo de empresa queda fuera, el dueño debe agregar el dominio en **Admin → Seguridad**. No abrir `example.com` ni temporales.
20. **PII pendiente de otra sesión:** `/redimir/historial` y `/order/pending-completion` muestran datos que conviene revisar, y quedaron huecos de validación de subidas. **No forman parte de esta tanda**; no mezclarlos sin pedido.
21. **Comprobar `orders.client_ip` tras el despliegue:** el nginx de 3srecargas restaura la IP real desde Cloudflare (`CF-Connecting-IP`) y manda `X-Forwarded-For $remote_addr`, así que debería guardar la IP del cliente y no la de Cloudflare. Si aparecen rangos `172.6x/172.7x`, revisar nginx antes de confiar en los strikes.

---

## 11. Historia condensada (para no repetir errores)

### Sep 2026 — base y primer gran bloque
- Reorganización del admin (Ediciones, Dinámicas, Afiliados), mapeo de proveedor con cantidades, canje directo de puntos, completar pago restante, notas en órdenes, forzar aprobación.
- Chat de soporte: bandeja en vivo, compra dentro del chat, comprobante con Gemini, 24 h, PWA, campana, rediseño cristal.
- Programa de minis: registro, rangos, retiros, notificaciones, reglas.
- Creación de **Hordas de Diamantes** (canvas, repeticiones, ranking semanal, antichéat).
- Auditoría de seguridad inicial: XSS en atributos, `safe_href`, límites, uploads.

### Oct 2026 — cuentas, perfiles, torneos, auditorías y despliegues
- 12-oct: primer despliegue propio al VPS (`505c78c`); se abandona el “link para Eliot” como única vía.
- Perfiles públicos, cuentas independientes de IDs, niveles numéricos, rankings editables, Top, marcos, fondos.
- Torneos con salas, cuenta regresiva, verificación obligatoria y premios automáticos.
- Auditorías de seguridad y rendimiento; modo liviano; mobile-first; arreglos de velocidad.
- 15-18 oct: perfil oficial `3srecargas`, secciones de paquetes, tipos de campo, Binance por referencia, pausa de órdenes, zona obligatoria, etiquetas rojas de pago, interruptor de verificación FF.
- 19-oct: Hordas por cuenta; 21-oct: se quitan los videos pagos de minis y pasan a “Código de creador”.

### Nov 2026 — costos, rangos, Google, devoluciones, incidentes
- 2-nov: costos automáticos de paquetes (FF/ML/BS) y rangos de creadores automáticos (`3995809`, `ac948c4`).
- 6-nov: botón de devolución, anuncio de paquete en descuento, Google vincular cuentas, Meta Pixel nuevo (`ec71edb`, `a80b0fd`).
- 12-nov: Zone ID de Mobile Legends solo 4 dígitos (`d504ce1`).
- 15-nov: cambio de clave del admin que cierra todas las sesiones (`4efff28`), rechazo muestra solo el motivo (`944d275`), Central One directo diagnosticado, Binance sin proxy.
- 16-nov: retiros de creadores como tarjetas con comprobante y copiar correo (`ac6c525`).
- 17/18-nov: acciones masivas de aprobar/rechazar órdenes (`e84f4cb`), arreglo de recargas directas de Central One (`0e25f04`) y contexto operativo (`5ea3a90`), desplegados juntos.

---

## 12. Dónde está el contexto completo

- **Historiales crudos:** `C:\Users\reals\.claude\projects\c--Users-reals-Desktop-3srecargas\*.jsonl` (dos sesiones: `406e2eed...` sep-nov, `8dc40084...` nov).
- **Memoria curada (leer primero):** `C:\Users\reals\.claude\projects\c--Users-reals-Desktop-3srecargas\memory\`
  - `MEMORY.md`: índice de las 71 notas.
  - `user_profile.md`: cómo es el dueño.
  - `feedback_end_of_session.md`: rutina de commit/deploy.
  - `reference_local_server.md`: servidor 5001.
  - `reference_eliot_link.md`: formato del link.
  - `project_*.md`: una nota por función/incidente.
- **Docs del repo:** `README.md` (muy largo, funcional), `CLAUDE.md` (guía de Eliot; ojo con los datos viejos de SSH).
- **Este archivo** es el resumen operativo para OpenCode. Si una nota de memoria y este archivo difieren, gana la nota más reciente **y** el código.

---

## 13. Primeros pasos en una sesión nueva

1. `git status`, `git log --oneline -5`, `git branch -vv`; confirma rama y si hay cambios sin commitear.
2. Lee `AGENTS.md` (este archivo) y, según el tema, la nota de memoria correspondiente.
3. Si el pedido es visual, pide/mira la captura; no rediseñes de más.
4. Si hay que probar, levanta el server en 5001 y usa una copia de la base para tests.
5. Si hay que desplegar, sigue §6 y verifica de verdad.
6. Si hay algo ambiguo o de otro proyecto, **pregunta antes de tocar**.
