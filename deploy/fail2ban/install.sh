#!/usr/bin/env bash
# Instala el jail de fail2ban para el login del admin. SOLO ROOT.
#
# Uso recomendado (el token no queda en el historial):
#   printf '%s' 'tu_token' > /root/cf_token && chmod 600 /root/cf_token
#   sudo CF_ZONE="id_de_zona" CF_TOKEN_FILE=/root/cf_token bash install.sh
#
# También acepta CF_TOKEN="..." (queda en el historial del shell; bórralo
# después). Si falta la zona o el token, el jail queda desactivado: la app ya
# bloquea por sí sola.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Ejecuta esto como root (sudo)." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
ACTION_FILE=/etc/fail2ban/action.d/cloudflare-token.conf
if [ ! -f "$ACTION_FILE" ]; then
  echo "Falta $ACTION_FILE: este fail2ban no tiene la acción de Cloudflare." >&2
  echo "El jail no se puede activar. La app sigue bloqueando por sí sola." >&2
  exit 1
fi

cp "$HERE/filter.d/3srecargas-admin.conf" /etc/fail2ban/filter.d/
cp "$HERE/jail.d/3srecargas-admin.local" /etc/fail2ban/jail.d/
# El repo puede venir de Windows con CRLF: bash y fail2ban esperan LF.
sed -i 's/\r$//' /etc/fail2ban/filter.d/3srecargas-admin.conf
sed -i 's/\r$//' /etc/fail2ban/jail.d/3srecargas-admin.local

CF_TOKEN="${CF_TOKEN:-}"
if [ -n "${CF_TOKEN_FILE:-}" ] && [ -f "${CF_TOKEN_FILE}" ]; then
  CF_TOKEN="$(tr -d '\r\n' < "${CF_TOKEN_FILE}")"
fi

if [ -n "${CF_ZONE:-}" ] && [ -n "$CF_TOKEN" ]; then
  sed -i "s|PON_TU_ZONE_ID_DE_CLOUDFLARE|${CF_ZONE}|g" /etc/fail2ban/jail.d/3srecargas-admin.local
  sed -i "s|PON_TU_TOKEN_DE_CLOUDFLARE|${CF_TOKEN}|g" /etc/fail2ban/jail.d/3srecargas-admin.local
  sed -i 's/^enabled = false/enabled = true/' /etc/fail2ban/jail.d/3srecargas-admin.local
  chmod 640 /etc/fail2ban/jail.d/3srecargas-admin.local
  echo "Jail activado con la zona y el token de Cloudflare."
else
  chmod 644 /etc/fail2ban/jail.d/3srecargas-admin.local
  echo "Sin CF_ZONE/CF_TOKEN/CF_TOKEN_FILE: el jail queda desactivado. La app sigue bloqueando por sí sola."
fi

# Validar ANTES de reiniciar: si la config está mal, fail2ban no debe caerse
# (se llevarían por delante también los jails de SSH y otros proyectos).
if ! fail2ban-client -t; then
  echo "La configuración de fail2ban no es válida. NO se reinició nada." >&2
  exit 1
fi

if grep -q '^enabled = true' /etc/fail2ban/jail.d/3srecargas-admin.local; then
  systemctl restart fail2ban
  fail2ban-client status 3srecargas-admin || true
else
  echo "Jail desactivado: no se reinició fail2ban."
fi
