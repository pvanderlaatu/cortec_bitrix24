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
Fase 2 — Usuarios.

Construye Bitrix24 User Map emparejando por correo a los usuarios del
portal con los `User` que ya existen en Frappe.

**No crea usuarios.** Un User de Frappe es una licencia y un acceso al
sitio; darlo de alta automáticamente porque alguien figuraba como
responsable de un lead de 2019 es exactamente lo que no debe hacer una
migración. Los que no casan quedan listados en el informe de la corrida
para que el equipo decida uno por uno, y mientras tanto sus registros
caen al usuario de respaldo configurado.
"""

import frappe

from cortec_bitrix24.utils import is_truthy

# Caché por corrida: resolve_user() se llama una vez por cada lead,
# deal, actividad y mensaje. Sin caché serían cientos de miles de
# SELECT sobre una tabla de veinte filas.
_CACHE_KEY = "_b24_user_cache"


def run(ctx) -> None:
    users = _fetch_all_users(ctx)
    ctx.note("usuarios_en_bitrix", len(users))

    sin_mapear = []

    for row in users:
        bitrix_id = str(row.get("ID") or "").strip()
        if not bitrix_id:
            continue

        email = (row.get("EMAIL") or "").strip().lower()
        nombre = " ".join(
            x for x in (row.get("NAME"), row.get("LAST_NAME")) if x
        ).strip()

        frappe_user = _match_user(email)

        if not frappe_user:
            sin_mapear.append(f"{bitrix_id} — {nombre or '(sin nombre)'} <{email}>")

        if ctx.dry_run:
            continue

        _upsert_user_map(
            ctx,
            bitrix_id=bitrix_id,
            bitrix_name=nombre,
            bitrix_email=email,
            user=frappe_user,
            is_active=is_truthy(row.get("ACTIVE")),
        )

    ctx.note("usuarios_sin_equivalente_en_frappe", sin_mapear)

    if sin_mapear:
        ctx.log(
            "warning",
            f"{len(sin_mapear)} usuarios de Bitrix24 no tienen un User de Frappe "
            f"con el mismo correo. Sus Leads y Deals quedarán a nombre de "
            f"{ctx.default_owner}. Complete a mano el campo 'user' en "
            f"Bitrix24 User Map antes de la corrida real si alguno debe "
            f"conservar su responsable.",
        )

    _clear_cache()
    ctx.checkpoint()


def _fetch_all_users(ctx) -> list[dict]:
    """
    `user.get` no admite el filtro `>ID` del modo rápido, así que aquí
    sí toca el offset clásico. Son decenas de filas, no decenas de
    miles: el coste es irrelevante.
    """
    users: list[dict] = []
    seen: set[str] = set()
    start = 0

    while True:
        raw = ctx.client.call_raw("user.get", {"start": start, "ADMIN_MODE": True})
        page = raw.get("result") or []

        # Si el portal repite la página (ignora `start`), parar: sin
        # esto el bucle no terminaría, como pasó con crm.status.list.
        new = [u for u in page if str(u.get("ID")) not in seen]
        if not new:
            break

        for user in new:
            seen.add(str(user.get("ID")))
        users.extend(new)

        next_start = raw.get("next")
        if next_start is None or int(next_start) <= start:
            break
        start = int(next_start)

    return users


def _match_user(email: str) -> str | None:
    """
    Busca el User de Frappe por correo.

    Solo por correo, y solo exacto: emparejar por nombre daría falsos
    positivos, y asignarle a un agente los leads de otro es un error
    que nadie detecta hasta que ya está en producción.
    """
    if not email:
        return None

    if frappe.db.exists("User", email):
        return email

    return frappe.db.get_value("User", {"email": email}, "name")


def _upsert_user_map(ctx, bitrix_id, bitrix_name, bitrix_email, user, is_active):
    """
    Crea o refresca la fila del mapa.

    Si la fila ya tiene un `user` puesto a mano, se respeta: es la
    corrección manual que el propio informe pidió, y volver a pisarla en
    la siguiente pasada la haría inútil.
    """
    if frappe.db.exists("Bitrix24 User Map", bitrix_id):
        existing_user = frappe.db.get_value("Bitrix24 User Map", bitrix_id, "user")
        updates = {
            "bitrix_name": bitrix_name,
            "bitrix_email": bitrix_email,
            "is_active": 1 if is_active else 0,
        }
        if not existing_user and user:
            updates["user"] = user
        frappe.db.set_value("Bitrix24 User Map", bitrix_id, updates)
        ctx.count("updated")
        return

    frappe.get_doc(
        {
            "doctype": "Bitrix24 User Map",
            "bitrix_user_id": bitrix_id,
            "bitrix_name": bitrix_name,
            "bitrix_email": bitrix_email,
            "user": user,
            "is_active": 1 if is_active else 0,
        }
    ).insert(ignore_permissions=True)
    ctx.count("created")


# ---------------------------------------------------------------------------
# Consulta (la usan todas las fases posteriores)
# ---------------------------------------------------------------------------


def resolve_user(ctx, bitrix_user_id, fallback: str | None = None) -> str | None:
    """
    User de Frappe responsable, o el usuario de respaldo.

    `fallback=None` explícito devuelve None en vez del respaldo, para
    los campos donde "sin responsable" es una respuesta válida (p. ej.
    el autor de un comentario antiguo).
    """
    if not bitrix_user_id:
        return fallback

    cache = frappe.local.__dict__.setdefault(_CACHE_KEY, {})
    key = str(bitrix_user_id)

    if key not in cache:
        cache[key] = frappe.db.get_value("Bitrix24 User Map", key, "user")

    return cache[key] or fallback


def resolve_owner(ctx, bitrix_user_id) -> str | None:
    """Responsable de un Lead/Deal, con el usuario de respaldo de Settings."""
    return resolve_user(ctx, bitrix_user_id, fallback=ctx.default_owner)


def _clear_cache() -> None:
    frappe.local.__dict__.pop(_CACHE_KEY, None)
