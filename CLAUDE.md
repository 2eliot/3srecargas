# CLAUDE.md — 3sRecargas

## Qué es

Web de venta de recargas (Flask + gunicorn) que consume la API whitelabel de
Revendedores (`https://inefablerevendedores.co`) y la base PostgreSQL central.
Dominios: **3srecargas.com** y **3srecargas.net** (Cloudflare, cuenta secundaria).

## Servidor de producción (desde 2026-09-29)

Los VPS viejos (66.179.95.95 y 74.208.64.66) fueron **suspendidos por IONOS el
29-sep-2026** y ya no existen. Todo corre en el VPS nuevo de Hostinger:

| Qué | Valor |
|---|---|
| Servidor | **177.7.62.94** (Hostinger KVM 2, Boston, Ubuntu 26.04) |
| Acceso | `ssh root@177.7.62.94` |
| Llave SSH | La llave personal de esta PC: `C:\Users\user\.ssh\id_ed25519` (ya autorizada en el servidor; no hace falta contraseña). **Nunca copiar la llave privada a documentos ni al repo.** |
| Ruta de la app | `/home/apps/web-c-3srecargas` (dueño: usuario `tresrecargas`) |
| Servicio | `web-c-3srecargas` (systemd) → gunicorn en `127.0.0.1:5002` |
| nginx | sitio `3srecargas.conf`; certificados Let's Encrypt en `/etc/letsencrypt` |

## Base de datos

PostgreSQL vive en **otro servidor** (177.7.62.54) y se alcanza por un túnel SSH
local: la app se conecta a `127.0.0.1:6432` (base `tresrecargas`). El túnel es el
servicio `tunel-db` del propio VPS — **no tocarlo ni cambiar el puerto**.

## Bot de Hype (canje de PINs FF)

Desde el 29-sep-2026 el bot corre **local en el mismo VPS** en `127.0.0.1:5000`
(`VPS_REDEEM_URL` / `VPS_VERIFY_URL` del `.env`; hay backup en
`.env.bak-emergencia`). Ya no existe el túnel 6500 al VPS viejo.

## ⚠️ Dependencias — Python 3.14 en el servidor

El servidor usa Python 3.14 y algunos pins del `requirements.txt` no compilan ahí.
En el servidor existe **`requirements.migracion.txt`** con estos ajustes (usar ESE
al reinstalar el venv en producción):

- `Pillow>=11.0` (10.3.0 no compila en 3.14)
- `psycopg2-binary>=2.9.10` (2.9.9 no compila en 3.14)
- `SQLAlchemy==2.0.54` **fijado** — SQLAlchemy 2.1 cambia el driver por defecto a
  psycopg v3 y la app revienta con `ModuleNotFoundError: No module named 'psycopg'`

## Cómo desplegar un cambio

**NUNCA editar código directamente en el VPS.** Flujo:

```
editar local → commit → copiar al VPS → reiniciar servicio
```

```powershell
# 1. Copiar el/los archivos cambiados (ejemplo con un archivo)
scp .\app\routes.py root@177.7.62.94:/home/apps/web-c-3srecargas/app/routes.py

# 2. Ajustar dueño y reiniciar
ssh root@177.7.62.94 "chown -R tresrecargas:tresrecargas /home/apps/web-c-3srecargas/app && systemctl restart web-c-3srecargas && systemctl is-active web-c-3srecargas"

# 3. Verificar
ssh root@177.7.62.94 "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/"
```

Si el cambio agrega dependencias: añadirlas a `requirements.txt` local **y** al
`requirements.migracion.txt` del servidor, e instalar con
`/home/apps/web-c-3srecargas/venv/bin/pip install -r requirements.migracion.txt`.

## ⚠️ No tocar

- `app/static/uploads/` en el servidor: ~1.9 GB de **capturas de pago reales**.
  Nunca borrarla ni sobrescribirla en un despliegue (excluirla de cualquier rsync).
- El túnel `tunel-db` y el `.env` de producción (contiene credenciales).
- Los logs y la carpeta `venv` del servidor se regeneran; el resto es sagrado.
