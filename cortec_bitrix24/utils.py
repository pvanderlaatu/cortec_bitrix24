# Copyright (C) 2026 Corporación de Tecnología CORTEC S.R.L.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of CORTEC Bitrix24.
#
# CORTEC Bitrix24 is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CORTEC Bitrix24 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with CORTEC Bitrix24. If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

"""
cortec_bitrix24.utils
======================
Conversiones entre las convenciones de Bitrix24 y las de Frappe.

Todo lo que aquí vive es puro (sin acceso a base de datos) salvo
parse_datetime(), que consulta la zona horaria del sitio. Eso hace que
sea la parte de la migración más fácil de cubrir con tests.
"""

import re
from datetime import datetime, timezone

import frappe
from frappe.utils import get_datetime, now_datetime

# Prefijo internacional por defecto para números sin código de país.
# Costa Rica: +506, ocho dígitos.
DEFAULT_COUNTRY_CODE = "506"
DEFAULT_NATIONAL_LENGTH = 8

# Longitudes que se consideran "un número nacional sin prefijo". El
# rango cubre Costa Rica (8) sin cerrarle la puerta a los países cuyos
# números nacionales tienen 9 o 10 dígitos, por si el código de país se
# cambia en Settings.
NATIONAL_LENGTH_RANGE = (7, 10)

_BBCODE_TAG = re.compile(r"\[/?[A-Za-z][^\]]*\]")
_BBCODE_BR = re.compile(r"\[br\]", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# Teléfonos
# ---------------------------------------------------------------------------


def normalize_phone(raw: str | None, country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """
    Normaliza un teléfono a E.164 (`+50688887777`).

    Bitrix24 guarda los teléfonos tal como los tecleó el agente:
    `8888-7777`, `(506) 8888 7777`, `+506 8888-7777`, `00506...`. Sin
    normalizar, el mismo cliente aparece como tres contactos distintos y
    el historial de WhatsApp no cuadra con el Lead.

    Un número que no se pueda interpretar se devuelve solo con la
    basura visual quitada: perder el dato sería peor que guardarlo mal.
    """
    if not raw:
        return ""

    digits = re.sub(r"[^\d+]", "", str(raw).strip())
    if not digits:
        return ""

    if digits.startswith("+"):
        return "+" + re.sub(r"\D", "", digits[1:])

    digits = re.sub(r"\D", "", digits)

    # 00 como prefijo internacional (marcación europea).
    if digits.startswith("00"):
        return "+" + digits[2:]

    # Ya trae el código de país sin el "+".
    if digits.startswith(country_code) and len(digits) > NATIONAL_LENGTH_RANGE[1]:
        return "+" + digits

    # Número nacional sin prefijo internacional.
    if NATIONAL_LENGTH_RANGE[0] <= len(digits) <= NATIONAL_LENGTH_RANGE[1]:
        return f"+{country_code}{digits}"

    return "+" + digits


def phone_matches(a: str | None, b: str | None) -> bool:
    """
    Compara dos teléfonos por sus últimos 8 dígitos.

    Necesario porque el historial de Open Channels trae el número en un
    formato y el contacto del CRM en otro, y para emparejar una
    conversación con su Lead basta la parte nacional.
    """
    da = re.sub(r"\D", "", a or "")[-DEFAULT_NATIONAL_LENGTH:]
    db = re.sub(r"\D", "", b or "")[-DEFAULT_NATIONAL_LENGTH:]
    return bool(da) and da == db


# ---------------------------------------------------------------------------
# Campos multivalor (EMAIL, PHONE, IM, WEB)
# ---------------------------------------------------------------------------


def multifield_values(rows: list | None) -> list[str]:
    """
    Extrae los valores de un campo multivalor de Bitrix24.

    Bitrix devuelve estos campos como
    `[{"ID": "1", "VALUE": "a@b.com", "VALUE_TYPE": "WORK"}, ...]`,
    pero un registro sin valores puede venir como lista vacía, como
    `False` o ausente. Normalizar aquí evita repetir la defensa en cada
    mapeo.
    """
    if not rows or not isinstance(rows, list):
        return []

    out = []
    for row in rows:
        if isinstance(row, dict):
            value = (row.get("VALUE") or "").strip()
        else:
            value = str(row or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def multifield_pairs(rows: list | None) -> list[tuple[str, str]]:
    """
    Como multifield_values(), pero conservando el VALUE_TYPE
    (`WORK`, `MOBILE`, `HOME`…) para poder llenar las tablas hijas
    Contact Email / Contact Phone con algo más que el valor suelto.
    """
    if not rows or not isinstance(rows, list):
        return []

    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = (row.get("VALUE") or "").strip()
        if not value:
            continue
        out.append((value, (row.get("VALUE_TYPE") or "").strip().upper()))
    return out


def first_value(rows: list | None) -> str:
    values = multifield_values(rows)
    return values[0] if values else ""


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------


def parse_datetime(value: str | None) -> datetime | None:
    """
    Convierte una fecha de Bitrix24 a la zona horaria del sitio Frappe.

    Bitrix24 entrega ISO-8601 con desfase explícito
    (`2024-05-13T10:22:31+03:00`), que es el huso del portal, no el del
    sitio. Se pasa a UTC y de ahí al huso del sitio, para que el
    historial migrado quede en la misma línea de tiempo que lo que
    Frappe escriba después.

    Devuelve None si no se puede interpretar: el llamador decide si eso
    significa "dejar el campo vacío" o "usar la fecha de la corrida".
    """
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            # Último recurso: dejar que Frappe lo intente con sus
            # propios formatos ("2024-05-13 10:22:31").
            return get_datetime(text)
        except Exception:
            return None

    if parsed.tzinfo is None:
        # Sin desfase no hay nada que convertir: se asume que ya viene
        # en la hora del sitio.
        return parsed

    from frappe.utils import convert_utc_to_system_timezone

    as_utc = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    converted = convert_utc_to_system_timezone(as_utc)
    return converted.replace(tzinfo=None)


def parse_datetime_str(value: str | None, fallback_now: bool = False) -> str | None:
    """parse_datetime() como cadena `YYYY-MM-DD HH:MM:SS`, lista para la BD."""
    parsed = parse_datetime(value)
    if parsed is None:
        return now_datetime().strftime("%Y-%m-%d %H:%M:%S") if fallback_now else None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def parse_date_str(value: str | None) -> str | None:
    parsed = parse_datetime(value)
    return parsed.strftime("%Y-%m-%d") if parsed else None


def to_bitrix_datetime(value: datetime | str | None) -> str | None:
    """
    Formato que espera Bitrix24 en los filtros `>DATE_MODIFY` de las
    pasadas delta.
    """
    if not value:
        return None
    if isinstance(value, str):
        value = get_datetime(value)
    return value.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------


def bbcode_to_text(value: str | None) -> str:
    """
    Convierte los comentarios del timeline de Bitrix24 (BBCode) a texto
    plano legible. No intenta traducir el marcado a HTML: los
    comentarios migrados son histórico de solo lectura y un `[URL=..]`
    mal traducido ensucia más de lo que aporta.
    """
    if not value:
        return ""

    text = _BBCODE_BR.sub("\n", str(value))
    text = _BBCODE_TAG.sub("", text)
    return text.strip()


def html_to_text(value: str | None) -> str:
    """Texto plano de un cuerpo HTML, para resúmenes y títulos."""
    if not value:
        return ""
    text = _HTML_TAG.sub(" ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def truncate(value: str | None, length: int = 140) -> str:
    text = (value or "").strip()
    if len(text) <= length:
        return text
    return text[: length - 1].rstrip() + "…"


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_truthy(value) -> bool:
    """
    Bitrix24 usa `"Y"`/`"N"` para los booleanos, pero también devuelve
    `1`/`0`, `True`/`False` y cadenas vacías según el método.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().upper() in ("Y", "1", "TRUE", "YES")


def local_cache(key: str) -> dict:
    """
    Diccionario que vive lo que dura el job o la petición, guardado en
    `frappe.local`.

    Con getattr/setattr y no con `frappe.local.__dict__`: en Frappe v16
    `frappe.local` guarda sus atributos en un ContextVar y ya no tiene
    `__dict__`, así que el acceso directo lanza AttributeError.
    """
    cache = getattr(frappe.local, key, None)
    if cache is None:
        cache = {}
        setattr(frappe.local, key, cache)
    return cache


def clear_local_cache(key: str) -> None:
    setattr(frappe.local, key, None)
