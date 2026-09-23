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
Fase 11 — Formularios web.

Esta fase **no migra datos**: los envíos del formulario ya llegaron al
CRM como Leads y viajan en la fase 6. Lo que hace es leer la definición
de cada formulario del portal y dejarla en `Bitrix24 Web Form` junto con
un mapeo campo-del-formulario → campo-de-Frappe.

Ese mapeo es el insumo para el corte: el formulario del sitio deja de
apuntar a Bitrix24 y pasa al Worker `leads-intake` de Cloudflare, que
llama a `cortec_helpdesk.api`. Sin esta fase, reproducir a mano qué
campo del formulario corresponde a cada campo del CRM es trabajo de
arqueología sobre un portal que para entonces ya estará en solo
lectura.

Los métodos de formularios web no están disponibles en todos los planes
de Bitrix24 ni se llaman igual en todas las versiones del portal. Se
prueban varios candidatos y, si ninguno responde, la fase lo anota y
termina sin error: no tener el mapeo automático es un inconveniente, no
un motivo para abortar una migración de horas.
"""

import json

import frappe

from cortec_bitrix24.migration import idmap
from cortec_bitrix24.utils import as_int, is_truthy

# Métodos candidatos, del más moderno al más antiguo.
CANDIDATE_METHODS = ("crm.webform.list", "crm.webform.embed.get", "crm.webform.get")

# Campo de Bitrix24 en el formulario → campo de CRM Lead en Frappe.
# Las claves se comparan en mayúsculas y sin el prefijo de entidad.
FIELD_MAP = {
    "NAME": "first_name",
    "LAST_NAME": "last_name",
    "SECOND_NAME": "middle_name",
    "FULL_NAME": "first_name",
    "EMAIL": "email",
    "PHONE": "mobile_no",
    "WEB": "website",
    "TITLE": "lead_name",
    "COMPANY_TITLE": "organization",
    "POST": "job_title",
    "COMMENTS": "custom_comentarios",
    "SOURCE_DESCRIPTION": "custom_comentarios",
}

# Prefijos de entidad que Bitrix24 antepone en los nombres de campo.
ENTITY_PREFIXES = ("CONTACT_", "LEAD_", "COMPANY_", "DEAL_")


def run(ctx) -> None:
    forms = _fetch_forms(ctx)

    if forms is None:
        ctx.note(
            "formularios",
            "El portal no expone los métodos crm.webform.*. Reproduzca el mapeo "
            "a mano desde el editor de formularios de Bitrix24 antes del corte.",
        )
        ctx.checkpoint()
        return

    ctx.note("formularios_encontrados", len(forms))

    for form in forms:
        bitrix_id = form.get("ID") or form.get("id")
        if not bitrix_id:
            continue
        try:
            _sync_form(ctx, bitrix_id, form)
        except Exception as e:
            ctx.log(
                "error",
                f"No se pudo registrar el formulario {bitrix_id}: {e}",
                bitrix_id=bitrix_id,
            )

    ctx.checkpoint()


def _fetch_forms(ctx) -> list[dict] | None:
    for method in CANDIDATE_METHODS:
        try:
            result = ctx.client.call(method)
        except Exception as e:
            ctx.log("info", f"{method} no disponible ({e}).")
            continue

        forms = _as_forms(result)
        if forms:
            ctx.log("info", f"Formularios leídos con {method}.")
            return forms

    return None


def _sync_form(ctx, bitrix_id, form: dict) -> None:
    nombre = (form.get("NAME") or form.get("name") or f"Formulario {bitrix_id}").strip()
    mapping = _build_mapping(form)

    values = {
        "bitrix_form_id": str(bitrix_id),
        "form_name": nombre,
        "is_active": 1 if is_truthy(form.get("ACTIVE") or form.get("active")) else 0,
        "entity_scheme": _entity_scheme(form),
        "submissions": as_int(form.get("COUNTER") or form.get("counter")),
        "mapping_json": json.dumps(mapping, ensure_ascii=False, indent=1),
        "raw_json": json.dumps(form, ensure_ascii=False, indent=1, default=str),
    }

    if ctx.dry_run:
        ctx.add_to_note_list("formularios", f"{bitrix_id} — {nombre}")
        return

    idmap.upsert(
        ctx, idmap.ENTITY_WEBFORM, bitrix_id, "Bitrix24 Web Form", values
    )


# ---------------------------------------------------------------------------
# Mapeo
# ---------------------------------------------------------------------------


def _build_mapping(form: dict) -> dict:
    """
    {campo del formulario: campo de CRM Lead}.

    Los campos que no se reconocen se dejan con destino `null` en vez de
    omitirse: así el JSON exportado enseña exactamente qué queda por
    decidir a mano, que es más útil que una lista incompleta que parece
    completa.
    """
    mapping = {}

    for field in _as_fields(form):
        code = str(
            field.get("CODE") or field.get("code") or field.get("name") or ""
        ).strip()
        if not code:
            continue

        mapping[code] = {
            "etiqueta": (field.get("CAPTION") or field.get("label") or "").strip()
            or None,
            "obligatorio": is_truthy(field.get("REQUIRED") or field.get("required")),
            "destino_frappe": _target_field(code),
        }

    return mapping


def _target_field(code: str) -> str | None:
    upper = code.upper()

    for prefix in ENTITY_PREFIXES:
        if upper.startswith(prefix):
            upper = upper[len(prefix) :]
            break

    if upper in FIELD_MAP:
        return FIELD_MAP[upper]

    if upper.startswith("UF_CRM_"):
        # Los campos personalizados ya tienen destino decidido en la
        # fase 3; se consulta ahí en vez de inventar otro nombre.
        return frappe.db.get_value(
            "Bitrix24 Field Map", f"Lead::{upper}", "target_fieldname"
        )

    return None


def _entity_scheme(form: dict) -> str | None:
    value = form.get("ENTITY_SCHEME") or form.get("entityScheme") or form.get("SCHEME")
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value) if value else None


# ---------------------------------------------------------------------------
# Normalización de la respuesta
# ---------------------------------------------------------------------------


def _as_forms(result) -> list[dict]:
    if isinstance(result, list):
        return [f for f in result if isinstance(f, dict)]
    if isinstance(result, dict):
        for key in ("forms", "FORMS", "items", "result"):
            value = result.get(key)
            if isinstance(value, list):
                return [f for f in value if isinstance(f, dict)]
        if result.get("ID") or result.get("id"):
            return [result]
    return []


def _as_fields(form: dict) -> list[dict]:
    for key in ("FIELDS", "fields", "INPUTS", "inputs"):
        value = form.get(key)
        if isinstance(value, list):
            return [f for f in value if isinstance(f, dict)]
        if isinstance(value, dict):
            return [f for f in value.values() if isinstance(f, dict)]
    return []
