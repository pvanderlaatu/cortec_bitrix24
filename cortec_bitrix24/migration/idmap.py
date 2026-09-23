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
cortec_bitrix24.migration.idmap
================================
La tabla de correspondencias ID de Bitrix24 → documento de Frappe, y el
upsert() que todas las fases usan para escribir.

De este módulo depende la propiedad central del diseño: **repetir la
migración no duplica nada**. Se apoya en que el nombre del documento
`Bitrix24 ID Map` ES la clave (`lead::1234`), así que la unicidad la
garantiza la clave primaria de MariaDB, no una comprobación previa que
podría perder una carrera.

Tres resultados posibles por registro:

  created  — no existía; se insertó.
  updated  — existía y el hash del payload cambió; se actualizó.
  skipped  — existía y el hash coincide; no se tocó la base de datos.

Ese hash es lo que hace que la segunda pasada de una migración de 40 000
registros sea barata: sin él, "idempotente" significaría reescribir
40 000 documentos cada vez.
"""

import hashlib
import json

import frappe

# Tipos de entidad. Se usan como prefijo del nombre en Bitrix24 ID Map,
# así que cambiarlos rompe la correspondencia con lo ya migrado.
ENTITY_COMPANY = "company"
ENTITY_CONTACT = "contact"
ENTITY_LEAD = "lead"
ENTITY_DEAL = "deal"
ENTITY_ACTIVITY = "activity"
ENTITY_COMMENT = "comment"
ENTITY_CHAT_MESSAGE = "chat_message"
ENTITY_FILE = "file"
ENTITY_WEBFORM = "webform"


def map_name(entity_type: str, bitrix_id) -> str:
    return f"{entity_type}::{bitrix_id}"


def payload_hash(values: dict) -> str:
    """
    Huella de los campos migrados.

    Se serializa con las claves ordenadas y `default=str` para que
    fechas y Decimals no cambien el hash según cómo los devolvió el
    portal esa vez.
    """
    blob = json.dumps(values, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def get_target(entity_type: str, bitrix_id) -> tuple[str, str] | None:
    """
    Documento de Frappe correspondiente a un ID de Bitrix24, o None.

    Comprueba además que el documento siga existiendo: si alguien borró
    a mano el Lead migrado, la fila del ID Map quedó huérfana y hay que
    volver a crearlo, no actualizar un nombre que ya no está.
    """
    row = frappe.db.get_value(
        "Bitrix24 ID Map",
        map_name(entity_type, bitrix_id),
        ["target_doctype", "target_name"],
        as_dict=True,
    )
    if not row or not row.target_doctype or not row.target_name:
        return None

    if not frappe.db.exists(row.target_doctype, row.target_name):
        return None

    return row.target_doctype, row.target_name


def get_target_name(entity_type: str, bitrix_id) -> str | None:
    target = get_target(entity_type, bitrix_id)
    return target[1] if target else None


def bulk_targets(entity_type: str, bitrix_ids: list) -> dict[str, str]:
    """
    Resuelve muchos IDs de una vez.

    Las fases de actividades y de chats necesitan saber a qué Lead/Deal
    pertenece cada mensaje; hacerlo de uno en uno son miles de SELECT.
    No verifica la existencia del destino: quien la necesite usa
    get_target().
    """
    if not bitrix_ids:
        return {}

    names = [map_name(entity_type, b) for b in bitrix_ids]
    rows = frappe.get_all(
        "Bitrix24 ID Map",
        filters={"name": ["in", names]},
        fields=["bitrix_id", "target_name"],
    )
    return {r.bitrix_id: r.target_name for r in rows if r.target_name}


def iter_migrated(entity_type: str, after_id: int, limit: int) -> list[dict]:
    """
    Siguiente bloque de registros ya migrados de un tipo, ordenados por
    ID de Bitrix24.

    La comparación y el orden son **numéricos**. `bitrix_id` es una
    columna de texto, y en texto `"9" > "100"`: un cursor que avanzara
    por comparación de cadenas saltaría registros y repetiría otros, en
    silencio y sin que ninguna fase diera error.

    Lo usan las fases que recorren los Leads y Deals ya migrados para
    pedirles algo al portal (comentarios del timeline, chats de Open
    Channels).
    """
    return frappe.db.sql(
        """
        SELECT bitrix_id, target_name
        FROM `tabBitrix24 ID Map`
        WHERE entity_type = %(entity_type)s
          AND target_name IS NOT NULL
          AND CAST(bitrix_id AS UNSIGNED) > %(after_id)s
        ORDER BY CAST(bitrix_id AS UNSIGNED) ASC
        LIMIT %(limit)s
        """,
        {
            "entity_type": entity_type,
            "after_id": int(after_id or 0),
            "limit": int(limit),
        },
        as_dict=True,
    )


def record(
    ctx,
    entity_type: str,
    bitrix_id,
    target_doctype: str,
    target_name: str,
    hash_value: str = "",
) -> None:
    """
    Escribe (o refresca) la fila del ID Map.

    Usa un INSERT … ON DUPLICATE KEY UPDATE en vez de get_doc/save: es
    una tabla de servicio con millones de filas potenciales y no
    necesita validaciones, versionado ni hooks.
    """
    if ctx.dry_run:
        return

    name = map_name(entity_type, bitrix_id)
    now = frappe.utils.now()

    frappe.db.sql(
        """
        INSERT INTO `tabBitrix24 ID Map`
            (name, creation, modified, modified_by, owner,
             entity_type, bitrix_id, target_doctype, target_name,
             payload_hash, last_synced, run)
        VALUES
            (%(name)s, %(now)s, %(now)s, %(user)s, %(user)s,
             %(entity_type)s, %(bitrix_id)s, %(target_doctype)s, %(target_name)s,
             %(hash)s, %(now)s, %(run)s)
        ON DUPLICATE KEY UPDATE
            modified = %(now)s,
            target_doctype = %(target_doctype)s,
            target_name = %(target_name)s,
            payload_hash = %(hash)s,
            last_synced = %(now)s,
            run = %(run)s
        """,
        {
            "name": name,
            "now": now,
            "user": frappe.session.user,
            "entity_type": entity_type,
            "bitrix_id": str(bitrix_id),
            "target_doctype": target_doctype,
            "target_name": target_name,
            "hash": hash_value,
            "run": ctx.run_name,
        },
    )


def upsert(
    ctx,
    entity_type: str,
    bitrix_id,
    doctype: str,
    values: dict,
    child_tables: dict[str, list[dict]] | None = None,
    created_on: str | None = None,
    modified_on: str | None = None,
    on_insert=None,
) -> str | None:
    """
    Crea o actualiza el documento de Frappe que corresponde a un
    registro de Bitrix24, y deja la correspondencia anotada.

    `child_tables` — {campo: [filas]}. Se reemplazan por completo en una
    actualización: reconciliar fila a fila las tablas hijas de Bitrix24
    (que no exponen un ID estable) daría duplicados en cada pasada.

    `created_on` / `modified_on` — fechas del portal. Se escriben con un
    UPDATE directo después de guardar, porque Frappe pisa `creation` en
    el insert y `modified` en cada save.

    `on_insert` — callback opcional que recibe el doc recién insertado.
    Lo usan las fases que necesitan hacer algo solo la primera vez
    (p. ej. asignar el Lead a un agente).

    Devuelve el nombre del documento, o None en corrida en seco.
    """
    values = {k: v for k, v in values.items() if v is not None}
    hash_value = payload_hash({"f": values, "c": child_tables or {}})

    existing_row = frappe.db.get_value(
        "Bitrix24 ID Map",
        map_name(entity_type, bitrix_id),
        ["target_doctype", "target_name", "payload_hash"],
        as_dict=True,
    )

    target_name = None
    if existing_row and existing_row.target_name:
        if frappe.db.exists(existing_row.target_doctype, existing_row.target_name):
            target_name = existing_row.target_name
        else:
            ctx.log(
                "warning",
                f"{existing_row.target_doctype} {existing_row.target_name} ya no "
                f"existe; se vuelve a crear desde {entity_type} {bitrix_id}.",
                bitrix_id=bitrix_id,
            )

    # Sin cambios desde la última pasada: el caso frecuente en una
    # migración incremental, y el único que no toca la base de datos.
    if target_name and existing_row.payload_hash == hash_value:
        ctx.count("skipped")
        return target_name

    if ctx.dry_run:
        ctx.count("updated" if target_name else "created")
        return target_name

    if target_name:
        doc = frappe.get_doc(doctype, target_name)
        doc.update(values)
        _set_child_tables(doc, child_tables)
        _save(doc)
        ctx.count("updated")
    else:
        doc = frappe.new_doc(doctype)
        doc.update(values)
        _set_child_tables(doc, child_tables)
        _save(doc, insert=True)
        ctx.count("created")
        if on_insert:
            on_insert(doc)

    _apply_timestamps(ctx, doctype, doc.name, created_on, modified_on)
    record(ctx, entity_type, bitrix_id, doctype, doc.name, hash_value)

    return doc.name


# ---------------------------------------------------------------------------
# Internos
# ---------------------------------------------------------------------------


def _set_child_tables(doc, child_tables: dict[str, list[dict]] | None) -> None:
    if not child_tables:
        return
    for fieldname, rows in child_tables.items():
        doc.set(fieldname, [])
        for row in rows or []:
            doc.append(fieldname, row)


def _save(doc, insert: bool = False) -> None:
    """
    Guarda saltándose permisos, obligatoriedad y versionado.

    - `ignore_permissions`: la corrida es de sistema, no de un usuario.
    - `ignore_mandatory`: Bitrix24 tiene registros viejos a los que les
      falta lo que Frappe considera obligatorio. Perderlos sería peor
      que migrarlos incompletos; quedan visibles en el informe.
    - `ignore_version`: el histórico no necesita una fila de Version por
      documento, y con decenas de miles duplicaría el tamaño de la BD.
    """
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.flags.ignore_version = True

    if insert:
        doc.insert(ignore_permissions=True, ignore_mandatory=True)
    else:
        doc.save(ignore_permissions=True)


def _apply_timestamps(ctx, doctype, name, created_on, modified_on) -> None:
    """
    Devuelve al documento sus fechas originales del portal.

    Sin esto, un historial de cinco años aparece entero con la fecha de
    la migración y deja de servir para nada.
    """
    if not ctx.preserve_timestamps:
        return

    updates = {}
    if created_on:
        updates["creation"] = created_on
    if modified_on:
        updates["modified"] = modified_on

    if not updates:
        return

    frappe.db.set_value(doctype, name, updates, update_modified=False)
