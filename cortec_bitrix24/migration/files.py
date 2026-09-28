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
Fase 10 — Adjuntos.

A diferencia de las demás, esta fase casi no hace trabajo por sí misma:
los adjuntos se bajan **en el momento**, junto con la actividad o el
mensaje al que pertenecen, llamando a `attach()` desde activities.py y
openlines.py.

Es deliberado. Un adjunto separado de su correo no le sirve a nadie, y
recorrer el timeline entero una segunda vez solo para los ficheros
duplicaría el gasto de peticiones contra un portal que admite dos por
segundo. Lo que queda aquí es el interruptor (`migrate_files` en
Settings, que `attach()` consulta) y el recuento final para el informe
de la corrida.

Un adjunto que falla nunca tumba el documento que lo lleva: se anota en
el log y el correo se migra igual, sin él. Perder el fichero es malo;
perder el correo por culpa del fichero, peor.
"""

import os

import frappe
import requests

from cortec_bitrix24.migration import idmap
from cortec_bitrix24.utils import as_int

# Tiempo máximo de descarga de un adjunto. Los ficheros grandes de un
# portal lento no deben congelar la corrida entera.
DOWNLOAD_TIMEOUT = 120


def run(ctx) -> None:
    """Recuento de lo que se adjuntó durante la corrida."""
    total = frappe.db.count(
        "Bitrix24 ID Map",
        {"entity_type": idmap.ENTITY_FILE, "run": ctx.run_name},
    )
    ctx.note("adjuntos_migrados_en_esta_corrida", total)
    ctx.note(
        "nota",
        "Los adjuntos se descargan junto con su actividad o mensaje "
        "(fases 8 y 9), no en una pasada aparte.",
    )
    ctx.checkpoint()


# ---------------------------------------------------------------------------
# API que usan activities.py y openlines.py
# ---------------------------------------------------------------------------


def attach(ctx, payload, doctype: str, docname: str) -> None:
    """
    Descarga los adjuntos de una actividad o mensaje y los cuelga del
    documento de Frappe.

    `payload` es el campo FILES de Bitrix24, cuya forma cambia según el
    método: lista de dicts, dict indexado, o lista de listas
    `[id, url, nombre, tamaño]`. `_normalize()` se encarga.
    """
    if ctx.dry_run or not ctx.settings.migrate_files:
        return

    for item in _normalize(payload):
        try:
            _attach_one(ctx, item, doctype, docname)
        except Exception as e:
            ctx.log(
                "error",
                f"No se pudo adjuntar el fichero {item.get('id')} a "
                f"{doctype} {docname}: {e}",
                bitrix_id=item.get("id"),
                target_doctype=doctype,
                target_name=docname,
            )


def _attach_one(ctx, item: dict, doctype: str, docname: str) -> None:
    file_id = item.get("id")
    if not file_id:
        return

    # Ya migrado en una pasada anterior: ni se vuelve a descargar ni se
    # duplica el File. Es lo que hace barata la segunda corrida.
    if idmap.get_target(idmap.ENTITY_FILE, file_id):
        ctx.count("skipped")
        return

    # El documento fue anonimizado por una solicitud de supresión.
    if idmap.is_suppressed(doctype, docname):
        ctx.count("skipped")
        return

    url = item.get("url") or _download_url(ctx, file_id)
    if not url:
        ctx.log(
            "warning",
            f"El fichero {file_id} no tiene URL de descarga; se omite.",
            bitrix_id=file_id,
            target_doctype=doctype,
            target_name=docname,
        )
        return

    limite = (ctx.settings.max_attachment_mb or 25) * 1024 * 1024
    declarado = as_int(item.get("size"))

    if declarado and declarado > limite:
        ctx.log(
            "warning",
            f"El fichero {file_id} ({declarado // 1024} KB) supera el límite "
            f"configurado; se omite.",
            bitrix_id=file_id,
            target_doctype=doctype,
            target_name=docname,
        )
        return

    content = _download(ctx, url, limite, file_id)
    if content is None:
        return

    nombre = _safe_filename(item.get("name"), file_id)

    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": nombre,
            "attached_to_doctype": doctype,
            "attached_to_name": docname,
            "content": content,
            "is_private": 1,
        }
    ).insert(ignore_permissions=True)

    idmap.record(ctx, idmap.ENTITY_FILE, file_id, "File", file_doc.name)
    ctx.count("created")


# ---------------------------------------------------------------------------
# Descarga
# ---------------------------------------------------------------------------


def _download_url(ctx, file_id) -> str | None:
    """
    URL de descarga vía `disk.attachedObject.get`.

    Solo hace falta cuando el campo FILES no trajo la URL. La que
    devuelve el portal ya lleva la autorización del webhook.
    """
    # Las actividades del CRM traen ids de "objeto adjunto"; las tareas
    # (ufTaskWebdavFiles) traen ids de fichero del Disco. Son espacios de
    # ids distintos y no hay forma fiable de saber cuál es cuál a priori,
    # así que se prueban los dos antes de darse por vencido.
    for method in ("disk.attachedObject.get", "disk.file.get"):
        try:
            result = ctx.client.call(method, {"id": file_id})
        except Exception:
            continue

        if not isinstance(result, dict):
            continue

        url = result.get("DOWNLOAD_URL") or result.get("downloadUrl")
        if url:
            return url

    ctx.log("warning", f"No se pudo obtener la URL de descarga de {file_id}.")
    return None


def _download(ctx, url: str, limite: int, file_id) -> bytes | None:
    """
    Baja el fichero comprobando el tamaño sobre la marcha.

    Se lee por trozos y se corta al superar el límite, en vez de fiarse
    del `Content-Length`: un portal puede no enviarlo, y cargar en
    memoria un fichero de 2 GB tumbaría el worker.
    """
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()

        trozos = []
        total = 0

        for trozo in response.iter_content(chunk_size=65536):
            if not trozo:
                continue
            total += len(trozo)
            if total > limite:
                ctx.log(
                    "warning",
                    f"El fichero {file_id} superó el límite durante la descarga "
                    f"({total // 1024} KB); se descarta.",
                    bitrix_id=file_id,
                )
                return None
            trozos.append(trozo)

    return b"".join(trozos)


# ---------------------------------------------------------------------------
# Normalización del campo FILES
# ---------------------------------------------------------------------------


def _normalize(payload) -> list[dict]:
    """
    Unifica las formas que Bitrix24 usa para FILES en
    `{id, name, url, size}`.
    """
    if not payload:
        return []

    if isinstance(payload, dict):
        payload = list(payload.values())

    if not isinstance(payload, list):
        return []

    out = []
    for item in payload:
        if isinstance(item, dict):
            out.append(
                {
                    "id": item.get("id") or item.get("ID") or item.get("fileId"),
                    "name": item.get("name") or item.get("NAME"),
                    "url": item.get("url")
                    or item.get("urlDownload")
                    or item.get("DOWNLOAD_URL"),
                    "size": item.get("size") or item.get("SIZE"),
                }
            )
        elif isinstance(item, (list, tuple)) and item:
            # Forma antigua: [id, url, nombre, tamaño].
            out.append(
                {
                    "id": item[0],
                    "url": item[1] if len(item) > 1 else None,
                    "name": item[2] if len(item) > 2 else None,
                    "size": item[3] if len(item) > 3 else None,
                }
            )
        elif item:
            out.append({"id": item})

    return [x for x in out if x.get("id")]


def _safe_filename(name: str | None, file_id) -> str:
    """
    Nombre de fichero sin componentes de ruta.

    Los nombres vienen de un sistema ajeno y acaban en el disco del
    servidor: un `../` en el nombre no debe poder escaparse del
    directorio de ficheros.
    """
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = base.lstrip(".") or f"b24-{file_id}"
    return base[:140]
