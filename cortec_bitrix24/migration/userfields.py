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
Fase 3 — Campos personalizados (UF_CRM_*).

Descubre los campos que CORTEC añadió en Bitrix24, los registra en
Bitrix24 Field Map y crea el Custom Field equivalente en el doctype
destino, con módulo "Cortec Bitrix24" para que viaje como fixture.

Los campos descubiertos se marcan con `create_custom_field = 1` por
defecto. La alternativa —no crear nada hasta que alguien lo apruebe
campo por campo— suena más prudente pero en la práctica significa que la
primera corrida real descarta en silencio todo lo que el equipo llevaba
años anotando en Bitrix24. El dry-run lista exactamente qué campos se
van a crear; ahí es donde se revisa, marcando `ignore` en los que
sobran.

Nomenclatura
------------
`UF_CRM_1680000000123` → `custom_b24_1680000000123`. El prefijo `custom_`
es el que Frappe espera en un Custom Field, y `b24_` deja claro de dónde
salió el campo cuando alguien lo encuentre dentro de dos años.
"""

import json

import frappe

from cortec_bitrix24.utils import (
    as_float,
    as_int,
    is_truthy,
    parse_date_str,
    parse_datetime_str,
)

# Entidad de Bitrix24 → (método userfield.list, doctype destino,
# campo tras el que insertar los campos nuevos).
ENTITIES = {
    "Lead": ("crm.lead.userfield.list", "CRM Lead", "lead_name"),
    "Deal": ("crm.deal.userfield.list", "CRM Deal", "next_step"),
    "Contact": ("crm.contact.userfield.list", "Contact", "email_id"),
    "Company": ("crm.company.userfield.list", "CRM Organization", "website"),
}

# USER_TYPE_ID de Bitrix24 → fieldtype de Frappe.
TYPE_MAP = {
    "string": "Data",
    "integer": "Int",
    "double": "Float",
    "money": "Currency",
    "date": "Date",
    "datetime": "Datetime",
    "boolean": "Check",
    "enumeration": "Select",
    "url": "Data",
    "file": "Attach",
    "address": "Small Text",
    "crm": "Data",
    "employee": "Data",
    "iblock_element": "Data",
    "iblock_section": "Data",
    "resourcebooking": "Data",
}

# Un campo multivalor de Bitrix24 no tiene equivalente directo: se
# guarda como texto con un valor por línea, que es legible y no pierde
# nada. Convertirlo en tabla hija exigiría un doctype por campo.
MULTIPLE_FIELDTYPE = "Small Text"

# Límite práctico para un nombre de columna en MariaDB dentro de Frappe.
MAX_FIELDNAME_LENGTH = 60


def run(ctx) -> None:
    resumen = {}

    for entity, (method, target_doctype, insert_after) in ENTITIES.items():
        try:
            fields = ctx.client.call(method) or {}
        except Exception as e:
            ctx.log("warning", f"{method} falló ({e}); se omite {entity}.")
            continue

        # El método devuelve un dict {índice: campo} o una lista según
        # la versión del portal.
        rows = list(fields.values()) if isinstance(fields, dict) else list(fields)

        creados = []
        for row in rows:
            created = _sync_field(ctx, entity, row, target_doctype, insert_after)
            if created:
                creados.append(created)

        resumen[entity] = creados

    ctx.note("campos_personalizados", resumen)
    _clear_cache()
    ctx.checkpoint()


def _sync_field(ctx, entity, row, target_doctype, insert_after) -> str | None:
    uf_code = (row.get("FIELD_NAME") or "").strip()
    if not uf_code:
        return None

    label = _label(row) or uf_code
    uf_type = (row.get("USER_TYPE_ID") or "string").strip()
    multiple = is_truthy(row.get("MULTIPLE"))
    options = _enum_options(row)

    fieldname = _fieldname(uf_code)
    map_name = f"{entity}::{uf_code}"

    if ctx.dry_run:
        if not frappe.db.exists("Bitrix24 Field Map", map_name):
            ctx.add_to_note_list(
                "campos_a_crear", f"{entity}.{uf_code} → {target_doctype}.{fieldname}"
            )
        return None

    row_map = _upsert_field_map(
        ctx,
        map_name=map_name,
        entity=entity,
        uf_code=uf_code,
        label=label,
        uf_type=uf_type,
        target_doctype=target_doctype,
        fieldname=fieldname,
        options=options,
    )

    if row_map.ignore or not row_map.create_custom_field:
        return None

    return _ensure_custom_field(
        ctx,
        doctype=row_map.target_doctype,
        fieldname=row_map.target_fieldname,
        label=label,
        uf_type=uf_type,
        multiple=multiple,
        options=options,
        insert_after=insert_after,
    )


# ---------------------------------------------------------------------------
# Bitrix24 Field Map
# ---------------------------------------------------------------------------


def _upsert_field_map(
    ctx, map_name, entity, uf_code, label, uf_type, target_doctype, fieldname, options
):
    """
    Crea la fila del mapa, o refresca solo lo descriptivo si ya existe.

    El destino (`target_doctype`, `target_fieldname`) y las banderas
    (`ignore`, `create_custom_field`) no se pisan nunca: son
    precisamente las decisiones que se toman a mano tras leer el
    dry-run.
    """
    if frappe.db.exists("Bitrix24 Field Map", map_name):
        frappe.db.set_value(
            "Bitrix24 Field Map",
            map_name,
            {
                "uf_label": label,
                "uf_type": uf_type,
                "options_json": json.dumps(options, ensure_ascii=False)
                if options
                else None,
            },
            update_modified=False,
        )
        return frappe.get_doc("Bitrix24 Field Map", map_name)

    doc = frappe.get_doc(
        {
            "doctype": "Bitrix24 Field Map",
            "bitrix_entity": entity,
            "uf_code": uf_code,
            "uf_label": label,
            "uf_type": uf_type,
            "target_doctype": target_doctype,
            "target_fieldname": fieldname,
            "create_custom_field": 1,
            "ignore": 0,
            "options_json": json.dumps(options, ensure_ascii=False)
            if options
            else None,
        }
    ).insert(ignore_permissions=True)
    ctx.count("created")
    return doc


# ---------------------------------------------------------------------------
# Custom Field
# ---------------------------------------------------------------------------


def _ensure_custom_field(
    ctx, doctype, fieldname, label, uf_type, multiple, options, insert_after
) -> str | None:
    """Crea el Custom Field si no existe. Nunca modifica uno existente."""
    if not doctype or not fieldname:
        return None

    name = f"{doctype}-{fieldname}"
    if frappe.db.exists("Custom Field", name):
        return None

    if frappe.get_meta(doctype).has_field(fieldname):
        # Ya existe como campo estándar del doctype: usarlo tal cual.
        return None

    fieldtype = MULTIPLE_FIELDTYPE if multiple else TYPE_MAP.get(uf_type, "Data")

    field = {
        "doctype": "Custom Field",
        "dt": doctype,
        "module": "Cortec Bitrix24",
        "fieldname": fieldname,
        "fieldtype": fieldtype,
        "label": label,
        "insert_after": insert_after,
        "translatable": 0,
        "description": f"Migrado desde Bitrix24 ({uf_type}"
        + (", multivalor" if multiple else "")
        + ").",
    }

    if fieldtype == "Select" and options:
        # Un Select vacío bloquearía el guardado del documento; se
        # incluye una opción en blanco para los registros sin valor.
        field["options"] = "\n".join([""] + list(options.values()))

    frappe.get_doc(field).insert(ignore_permissions=True)
    ctx.count("created")
    ctx.log(
        "info",
        f"Custom Field creado: {doctype}.{fieldname} ({fieldtype}) — {label}",
        target_doctype="Custom Field",
        target_name=name,
    )
    return f"{doctype}.{fieldname}"


# ---------------------------------------------------------------------------
# Conversión de valores (la usan leads/deals/contacts/companies)
# ---------------------------------------------------------------------------

_CACHE_KEY = "_b24_fieldmap_cache"


def get_field_map(entity: str) -> list[dict]:
    """Filas activas del mapa para una entidad, cacheadas por corrida."""
    cache = frappe.local.__dict__.setdefault(_CACHE_KEY, {})

    if entity not in cache:
        cache[entity] = frappe.get_all(
            "Bitrix24 Field Map",
            filters={"bitrix_entity": entity, "ignore": 0},
            fields=[
                "uf_code",
                "uf_type",
                "target_doctype",
                "target_fieldname",
                "options_json",
            ],
        )

    return cache[entity]


def build_uf_values(ctx, entity: str, record: dict) -> dict:
    """
    Convierte los UF_CRM_* de un registro a valores de campos Frappe.

    Devuelve solo los campos que existen realmente en el doctype
    destino: si alguien marcó `ignore` o borró el Custom Field, el valor
    se descarta en silencio en vez de reventar el guardado del Lead
    entero por un campo accesorio.
    """
    values = {}

    for row in get_field_map(entity):
        if row.uf_code not in record:
            continue

        fieldname = row.target_fieldname
        if not fieldname or not _field_exists(row.target_doctype, fieldname):
            continue

        converted = _convert(record.get(row.uf_code), row.uf_type, row.options_json)
        if converted not in (None, ""):
            values[fieldname] = converted

    return values


def _convert(value, uf_type: str, options_json: str | None):
    if value is None or value == "" or value is False:
        return None

    if isinstance(value, list):
        # Campo multivalor: un valor por línea.
        parts = [
            str(_convert(v, uf_type, options_json) or "").strip() for v in value
        ]
        return "\n".join(p for p in parts if p) or None

    if uf_type == "boolean":
        return 1 if is_truthy(value) else 0
    if uf_type == "date":
        return parse_date_str(value)
    if uf_type == "datetime":
        return parse_datetime_str(value)
    if uf_type in ("double", "money"):
        return as_float(value)
    if uf_type == "integer":
        return as_int(value)
    if uf_type == "enumeration":
        # El registro guarda el ID del elemento, no su etiqueta.
        options = json.loads(options_json) if options_json else {}
        return options.get(str(value), str(value))

    return str(value)


def _field_exists(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.get_meta(doctype).has_field(fieldname))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _label(row: dict) -> str:
    """
    La etiqueta viene como dict por idioma (`{"es": "...", "en": "..."}`)
    o como cadena suelta, según el método y la versión del portal.
    """
    for key in ("EDIT_FORM_LABEL", "LIST_COLUMN_LABEL", "FORM_LABEL"):
        value = row.get(key)
        if isinstance(value, dict):
            for lang in ("es", "en", "la"):
                if value.get(lang):
                    return str(value[lang]).strip()
            for v in value.values():
                if v:
                    return str(v).strip()
        elif value:
            return str(value).strip()
    return ""


def _enum_options(row: dict) -> dict[str, str]:
    """{ID del elemento: etiqueta} de un campo de tipo enumeración."""
    items = row.get("LIST") or []
    if isinstance(items, dict):
        items = list(items.values())

    out = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("ID") or "").strip()
        value = (item.get("VALUE") or "").strip()
        if item_id and value:
            out[item_id] = value
    return out


def _fieldname(uf_code: str) -> str:
    """`UF_CRM_1680000000123` → `custom_b24_1680000000123`."""
    suffix = uf_code.upper()
    for prefix in ("UF_CRM_", "UF_"):
        if suffix.startswith(prefix):
            suffix = suffix[len(prefix) :]
            break

    fieldname = "custom_b24_" + suffix.lower().strip("_")
    return fieldname[:MAX_FIELDNAME_LENGTH]


def _clear_cache() -> None:
    frappe.local.__dict__.pop(_CACHE_KEY, None)
