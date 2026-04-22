import base64
import json
import mimetypes
import os
import re

import requests
from flask import current_app


GEMINI_MODEL_CANDIDATES = (
    'gemini-2.5-flash',
)


def _normalize_reference_value(value):
    raw = str(value or '').strip().upper()
    if not raw:
        return ''

    compact = re.sub(r'\s+', '', raw)
    compact = compact.strip(':-#.')
    compact = re.sub(r'[^A-Z0-9-]', '', compact)
    if len(compact) < 4 or len(compact) > 30:
        return ''
    return compact


def _extract_reference_from_text(text):
    raw = str(text or '').strip()
    if not raw:
        return ''

    labeled_patterns = [
        r'(?:referencia|ref(?:erencia)?|n(?:ro|um|u?m)?\.?\s*(?:de\s*)?(?:operacion|operaci[oó]n|transaccion|transacci[oó]n))\D{0,20}([A-Z0-9-]{4,30})',
        r'(?:codigo\s*(?:de\s*)?(?:referencia|operacion|operaci[oó]n))\D{0,20}([A-Z0-9-]{4,30})',
    ]
    upper_raw = raw.upper()
    for pattern in labeled_patterns:
        match = re.search(pattern, upper_raw, re.IGNORECASE)
        if match:
            candidate = _normalize_reference_value(match.group(1))
            if candidate:
                return candidate

    numeric_candidates = re.findall(r'\b\d{4,30}\b', raw)
    if numeric_candidates:
        numeric_candidates.sort(key=len, reverse=True)
        for candidate in numeric_candidates:
            normalized = _normalize_reference_value(candidate)
            if normalized:
                return normalized

    generic_candidates = re.findall(r'\b[A-Z0-9-]{4,30}\b', upper_raw)
    generic_candidates.sort(key=len, reverse=True)
    for candidate in generic_candidates:
        normalized = _normalize_reference_value(candidate)
        if normalized:
            return normalized

    return ''


def _extract_json_block(text):
    raw = str(text or '').strip()
    if not raw:
        return None

    try:
        return json.loads(raw)
    except Exception:
        pass

    start = raw.find('{')
    end = raw.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        return json.loads(raw[start:end + 1])
    except Exception:
        return None


def _extract_text_from_gemini_response(data):
    candidates = data.get('candidates') or []
    for candidate in candidates:
        content = candidate.get('content') or {}
        for part in content.get('parts') or []:
            text = str(part.get('text') or '').strip()
            if text:
                return text
    return ''


def _get_gemini_api_key():
    return str(current_app.config.get('GEMINI_API_KEY') or '').strip()


def _guess_mime_type(filename, fallback='image/jpeg'):
    guessed, _ = mimetypes.guess_type(filename or '')
    if guessed and guessed.startswith('image/'):
        return guessed
    return fallback


def _build_gemini_error_message(status_code, remote_message=''):
    remote_message = str(remote_message or '').strip()
    lowered = remote_message.lower()

    if 'not available to new users' in lowered or 'deprecated' in lowered or 'decommissioned' in lowered:
        return 'La lectura automática de referencias no está disponible en este momento. Ingresa la referencia manualmente.'

    if 'quota exceeded' in lowered or 'rate limit' in lowered or 'too many requests' in lowered:
        return 'La lectura automática de referencias no está disponible en este momento. Ingresa la referencia manualmente.'

    if status_code in (401, 403):
        return 'La lectura automática de referencias no está disponible en este momento. Ingresa la referencia manualmente.'

    if status_code >= 500:
        return 'La lectura automática de referencias no está disponible en este momento. Ingresa la referencia manualmente.'

    return remote_message or f'Gemini devolvió HTTP {status_code}.'


def _should_retry_with_fallback(status_code, remote_message=''):
    if status_code < 400:
        return False

    lowered = str(remote_message or '').strip().lower()
    retry_markers = (
        'not available to new users',
        'deprecated',
        'decommissioned',
        'not found',
        'is not found',
        'unsupported',
    )
    return any(marker in lowered for marker in retry_markers)


def _extract_gemini_remote_error(data):
    if not isinstance(data, dict):
        return ''
    error_data = data.get('error') or {}
    if isinstance(error_data, dict):
        return str(error_data.get('message') or '').strip()
    return ''


def _build_gemini_model_candidates():
    candidates = []
    for model_name in GEMINI_MODEL_CANDIDATES:
        normalized = str(model_name or '').strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _post_gemini_generate_content(model, api_key, payload, timeout):
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    return requests.post(url, json=payload, timeout=timeout)


def extract_reference_from_image_bytes(image_bytes, mime_type='image/jpeg', filename='capture.jpg'):
    if not image_bytes:
        return {'ok': False, 'reference': '', 'message': 'No se recibieron bytes del comprobante.'}

    api_key = _get_gemini_api_key()
    if not api_key:
        return {'ok': False, 'reference': '', 'message': 'La API key de Gemini no está configurada.'}

    prompt = (
        'Analiza este comprobante de pago y extrae unicamente la referencia bancaria o numero de operacion del pago. '
        'Ignora montos, telefonos, cedulas, numeros de cuenta, fecha, hora y nombres del beneficiario. '
        'Si no puedes identificar una referencia con claridad, devuelve referencia vacia. '
        'Responde solo JSON con esta forma exacta: '
        '{"found":true,"reference":"123456","reason":"texto corto"}.'
    )

    payload = {
        'contents': [
            {
                'parts': [
                    {'text': prompt},
                    {
                        'inline_data': {
                            'mime_type': mime_type,
                            'data': base64.b64encode(image_bytes).decode('ascii'),
                        }
                    },
                ]
            }
        ],
        'generationConfig': {
            'temperature': 0,
            'response_mime_type': 'application/json',
        },
    }

    timeout = current_app.config.get('GEMINI_REFERENCE_TIMEOUT', 25)
    response = None
    data = {}
    model = GEMINI_MODEL_CANDIDATES[0]
    remote_message = ''

    for candidate_model in _build_gemini_model_candidates():
        model = candidate_model
        try:
            response = _post_gemini_generate_content(candidate_model, api_key, payload, timeout)
        except requests.exceptions.Timeout:
            return {'ok': False, 'reference': '', 'message': 'Gemini no respondió a tiempo.'}
        except requests.exceptions.ConnectionError:
            return {'ok': False, 'reference': '', 'message': 'No se pudo conectar con Gemini.'}
        except Exception as exc:
            return {'ok': False, 'reference': '', 'message': f'Error consultando Gemini: {exc}'}

        try:
            data = response.json()
        except Exception:
            data = {}

        if response.status_code < 400:
            break

        remote_message = _extract_gemini_remote_error(data)
        if _should_retry_with_fallback(response.status_code, remote_message) and candidate_model != GEMINI_MODEL_CANDIDATES[-1]:
            continue

        message = _build_gemini_error_message(response.status_code, remote_message)
        return {'ok': False, 'reference': '', 'message': message}

    if not response or response.status_code >= 400:
        message = _build_gemini_error_message(getattr(response, 'status_code', 500), remote_message)
        return {'ok': False, 'reference': '', 'message': message}

    raw_text = _extract_text_from_gemini_response(data)
    parsed = _extract_json_block(raw_text) or {}

    reference = _normalize_reference_value(parsed.get('reference'))
    if not reference:
        reference = _extract_reference_from_text(raw_text)

    message = str(parsed.get('reason') or '').strip()
    if not message and not reference:
        message = 'No se pudo identificar una referencia clara en el comprobante.'

    return {
        'ok': True,
        'reference': reference,
        'message': message,
        'raw_text': raw_text,
        'model': model,
        'filename': os.path.basename(filename or ''),
    }


def extract_reference_from_image_path(file_path):
    if not file_path or not os.path.exists(file_path):
        return {'ok': False, 'reference': '', 'message': 'El archivo del comprobante no existe.'}

    with open(file_path, 'rb') as capture_file:
        return extract_reference_from_image_bytes(
            capture_file.read(),
            mime_type=_guess_mime_type(file_path),
            filename=file_path,
        )