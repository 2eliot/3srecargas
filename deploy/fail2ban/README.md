# Fail2ban para el login del admin

La app **ya bloquea por sí sola** la IP después de 10 fallos (24 h) y aplica
la regla de 3 intentos + 1 cada 5 minutos. Este jail es una capa extra.

## Importante: Cloudflare

La web está detrás de Cloudflare. Un `iptables`/`nftables` normal en el VPS
**no corta al cliente**: el tráfico llega desde las IPs de Cloudflare. Para
bloquear de verdad hay que usar la acción `cloudflare-token` (token de API con
permiso de Firewall/WAF) o crear la regla equivalente en Cloudflare.

El archivo `jail.d/3srecargas-admin.local` viene con `enabled = false` para no
romper nada. Sin zona y token, la app sigue protegiendo el login por sí sola.

## Instalación (requiere root)

La forma recomendada evita dejar el token en el historial del shell:

```bash
printf '%s' 'tu_token' > /root/cf_token
chmod 600 /root/cf_token
sudo CF_ZONE="id_de_zona" CF_TOKEN_FILE=/root/cf_token bash install.sh
```

También se puede hacer a mano:

```bash
cp filter.d/3srecargas-admin.conf /etc/fail2ban/filter.d/
cp jail.d/3srecargas-admin.local /etc/fail2ban/jail.d/
# Editar el archivo instalado: cfzone (ID de zona) y cftoken (token de API)
nano /etc/fail2ban/jail.d/3srecargas-admin.local
# Cambiar enabled = false a enabled = true
fail2ban-client -t          # valida la configuración ANTES de reiniciar
systemctl restart fail2ban
fail2ban-client status 3srecargas-admin
```

El instalador copia los archivos, quita CRLF, comprueba que exista la acción
`cloudflare-token`, valida con `fail2ban-client -t` y solo entonces reinicia.
Un error de configuración no tumba los demás jails (SSH incluido).

El token debe tener permiso de **Firewall/WAF** en esa zona. El ID de zona
está en el resumen del dominio en el panel de Cloudflare.

La app escribe estas líneas en el journal de systemd:

```
ADMIN_LOGIN_FAIL ip=<IP> identifier_matches=0 ua=...
ADMIN_2FA_FAIL ip=<IP> user=... ua=...
ADMIN_LOGIN_BLOCK ip=<IP> ...
```

El filtro incluido las reconoce y captura IPv4 e IPv6 (la IP de IPv6 puede
venir agrupada por /64).

## Si no tienes token de Cloudflare

Deja el jail desactivado. El bloqueo por app (`admin_login_attempts`) sigue
funcionando y se puede liberar desde **Admin → Seguridad**.
