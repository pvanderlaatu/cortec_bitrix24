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
Fase 9 — WhatsApp (Open Channels) → WhatsApp Message.

Recorre los Leads y Deals ya migrados, pide sus chats de Open Channels
(`imopenlines.crm.chat.get`) y de cada chat su historial completo
(`imopenlines.session.history.get`), y lo escribe como WhatsApp Message
vinculados al mismo Lead/Deal. Así la conversación de WhatsApp aparece
en /crm en el mismo hilo que los correos.

⚠️ Por qué esta fase parchea frappe_whatsapp
--------------------------------------------
`WhatsAppMessage.before_insert` llama a `send_outgoing()`, que **envía
el mensaje a Meta** cuando `type == "Outgoing"`. Insertar el histórico
tal cual reenviaría a los clientes, uno a uno, todos los WhatsApp que
los agentes escribieron en los últimos años — con su texto original y
fuera de contexto.

`_dont_send()` sustituye ese método por un no-op mientras dura la fase y
lo restaura al salir. Si el método no existe (frappe_whatsapp cambió de
forma en una versión nueva), la fase **se aborta** en vez de seguir: el
riesgo de equivocarse aquí no es un dato mal migrado, es un mensaje real
a un cliente real.

Si frappe_whatsapp no está instalado, el historial cae a `Communication`
con `communication_medium = "Chat"`: se pierde la vista de burbujas de
chat, pero no el contenido.
"""

from contextlib import contextmanager

import frappe

from cortec_bitrix24.migration import files, idmap
from cortec_bitrix24.migration.users import resolve_user
from cortec_bitrix24.utils import (
    html_to_text,
    normalize_phone,
    parse_datetime_str,
    truncate,
)

# CRM_ENTITY_TYPE que espera imopenlines.crm.chat.get.
ENTITIES = (
    (idmap.ENTITY_LEAD, "LEAD", "CRM Lead"),
    (idmap.ENTITY_DEAL, "DEAL", "CRM Deal"),
)

# Conectores de Open Channels que se consideran WhatsApp. Los demás
# (Telegram, Facebook, el chat de la web) se migran como Communication:
# meterlos en WhatsApp Message los mostraría como si hubieran llegado
# por un canal que no es el suyo.
WHATSAPP_CONNECTORS = {"whatsappbyedna", "whatsapp", "wazzup24", "twilio_whatsapp"}


def run(ctx) -> None:
    usa_whatsapp = _whatsapp_installed()

    if not usa_whatsapp:
        ctx.log(
            "warning",
            "frappe_whatsapp no está instalado: el historial de WhatsApp se "
            "migra como Communication con medio 'Chat'.",
        )

    with _dont_send(ctx, usa_whatsapp):
        for entity_type, crm_entity_type, doctype in ENTITIES:
            _migrate_entity(ctx, entity_type, crm_entity_type, doctype, usa_whatsapp)

    ctx.checkpoint()


def _migrate_entity(ctx, entity_type, crm_entity_type, doctype, usa_whatsapp) -> None:
    cursor_key = f"chats_{entity_type}"
    last = ctx.cursor(cursor_key)

    while True:
        rows = idmap.iter_migrated(entity_type, last, ctx.batch_size)
        if not rows:
            return

        commands = {
            r.bitrix_id: (
                "imopenlines.crm.chat.get",
                {"CRM_ENTITY_TYPE": crm_entity_type, "CRM_ENTITY": r.bitrix_id},
            )
            for r in rows
        }

        try:
            results = ctx.client.batch(commands)
        except Exception as e:
            ctx.log(
                "warning",
                f"No se pudieron leer los chats de un bloque de {entity_type}: {e}",
            )
            results = {}

        by_id = {r.bitrix_id: r.target_name for r in rows}

        for bitrix_id, chats in results.items():
            target = by_id.get(bitrix_id)
            if not target:
                continue

            for chat in _as_chats(chats):
                try:
                    _migrate_chat(ctx, chat, doctype, target, usa_whatsapp)
                except Exception as e:
                    ctx.log(
                        "error",
                        f"No se pudo migrar el chat {chat.get('CHAT_ID')} "
                        f"de {doctype} {target}: {e}",
                        bitrix_id=bitrix_id,
                        target_doctype=doctype,
                        target_name=target,
                    )

        last = rows[-1].bitrix_id
        ctx.set_cursor(last, cursor_key)
        ctx.checkpoint()


# ---------------------------------------------------------------------------
# Un chat
# ---------------------------------------------------------------------------


def _migrate_chat(ctx, chat: dict, doctype: str, target: str, usa_whatsapp) -> None:
    chat_id = chat.get("CHAT_ID") or chat.get("ID")
    if not chat_id:
        return

    es_whatsapp = usa_whatsapp and _is_whatsapp(chat)

    try:
        history = ctx.client.call(
            "imopenlines.session.history.get", {"CHAT_ID": chat_id}
        )
    except Exception as e:
        ctx.log(
            "warning",
            f"No se pudo leer el historial del chat {chat_id}: {e}",
            target_doctype=doctype,
            target_name=target,
        )
        return

    mensajes = _as_messages(history)
    if not mensajes:
        return

    numero_cliente = _client_number(ctx, doctype, target, chat)

    for mensaje in mensajes:
        try:
            if es_whatsapp:
                _sync_whatsapp_message(
                    ctx, mensaje, chat_id, doctype, target, numero_cliente
                )
            else:
                _sync_chat_communication(ctx, mensaje, chat_id, doctype, target)
        except Exception as e:
            ctx.log(
                "error",
                f"No se pudo migrar el mensaje {mensaje.get('ID')} "
                f"del chat {chat_id}: {e}",
                bitrix_id=mensaje.get("ID"),
                target_doctype=doctype,
                target_name=target,
            )


def _sync_whatsapp_message(
    ctx, mensaje: dict, chat_id, doctype: str, target: str, numero_cliente: str
) -> None:
    """
    Un mensaje del historial → WhatsApp Message.

    La dirección se deduce del autor: en Open Channels, `AUTHOR_ID = 0`
    (o ausente) es el cliente; cualquier otro es un operador.
    """
    bitrix_id = mensaje.get("ID")
    author_id = str(mensaje.get("AUTHOR_ID") or "0")
    entrante = author_id in ("0", "")

    texto = (mensaje.get("TEXT") or mensaje.get("MESSAGE") or "").strip()
    if not texto and not mensaje.get("FILES"):
        return

    values = {
        "type": "Incoming" if entrante else "Outgoing",
        "message_type": "Manual",
        "content_type": "text",
        "message": texto,
        "message_id": f"B24-{chat_id}-{bitrix_id}",
        "conversation_id": f"B24-{chat_id}",
        "from": numero_cliente if entrante else None,
        "to": numero_cliente,
        "profile_name": (mensaje.get("AUTHOR_NAME") or "").strip() or None,
        "status": "Migrado",
        "reference_doctype": doctype,
        "reference_name": target,
    }

    name = idmap.upsert(
        ctx,
        idmap.ENTITY_CHAT_MESSAGE,
        f"{chat_id}-{bitrix_id}",
        "WhatsApp Message",
        values,
        created_on=parse_datetime_str(mensaje.get("DATE_CREATE")),
    )

    if name:
        files.attach(ctx, mensaje.get("FILES"), "WhatsApp Message", name)


def _sync_chat_communication(
    ctx, mensaje: dict, chat_id, doctype: str, target: str
) -> None:
    """Respaldo cuando no hay frappe_whatsapp o el canal no es WhatsApp."""
    bitrix_id = mensaje.get("ID")
    author_id = str(mensaje.get("AUTHOR_ID") or "0")
    entrante = author_id in ("0", "")

    texto = (mensaje.get("TEXT") or mensaje.get("MESSAGE") or "").strip()
    if not texto:
        return

    values = {
        "communication_type": "Communication",
        "communication_medium": "Chat",
        "sent_or_received": "Received" if entrante else "Sent",
        "subject": truncate(texto, 140),
        "content": frappe.utils.escape_html(texto).replace("\n", "<br>"),
        "text_content": html_to_text(texto),
        "reference_doctype": doctype,
        "reference_name": target,
        "communication_date": parse_datetime_str(
            mensaje.get("DATE_CREATE"), fallback_now=True
        ),
        "status": "Linked",
        "seen": 1,
        "user": resolve_user(ctx, author_id, fallback=None),
    }

    idmap.upsert(
        ctx,
        idmap.ENTITY_CHAT_MESSAGE,
        f"{chat_id}-{bitrix_id}",
        "Communication",
        values,
        created_on=parse_datetime_str(mensaje.get("DATE_CREATE")),
    )


# ---------------------------------------------------------------------------
# Seguridad: no reenviar el histórico
# ---------------------------------------------------------------------------


@contextmanager
def _dont_send(ctx, usa_whatsapp: bool):
    """
    Desactiva el envío real de WhatsApp mientras dura la fase.

    No hay bandera ni flag en frappe_whatsapp que permita insertar un
    mensaje saliente sin mandarlo, así que se sustituye el método por un
    no-op y se restaura al salir. Es intrusivo a propósito: la
    alternativa es no migrar la mitad saliente de cada conversación.
    """
    if not usa_whatsapp or ctx.dry_run:
        yield
        return

    from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import (
        WhatsAppMessage,
    )

    original = getattr(WhatsAppMessage, "send_outgoing", None)

    if original is None:
        # frappe_whatsapp cambió: no sabemos por dónde envía. Abortar la
        # fase es la única respuesta aceptable — equivocarse aquí manda
        # mensajes reales a clientes reales.
        raise RuntimeError(
            "WhatsAppMessage.send_outgoing no existe en esta versión de "
            "frappe_whatsapp. La fase de WhatsApp se aborta para no arriesgar "
            "el reenvío del histórico a los clientes. Revise "
            "cortec_bitrix24/migration/openlines.py antes de reintentar."
        )

    def _noop(self, *args, **kwargs):
        return None

    WhatsAppMessage.send_outgoing = _noop
    ctx.log(
        "info",
        "Envío real de WhatsApp desactivado durante la fase (parche sobre "
        "WhatsAppMessage.send_outgoing). Se restaura al terminar.",
    )

    try:
        yield
    finally:
        WhatsAppMessage.send_outgoing = original
        ctx.log("info", "Envío real de WhatsApp restaurado.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _whatsapp_installed() -> bool:
    return "frappe_whatsapp" in frappe.get_installed_apps() and frappe.db.exists(
        "DocType", "WhatsApp Message"
    )


def _is_whatsapp(chat: dict) -> bool:
    """
    ¿El chat llegó por WhatsApp?

    El nombre del conector varía según el proveedor contratado, así que
    se compara contra una lista conocida y, además, se acepta cualquier
    identificador que contenga "whatsapp".
    """
    for key in ("CONNECTOR_ID", "ENTITY_TYPE", "ENTITY_ID", "LINE_NAME", "CONNECTOR"):
        value = str(chat.get(key) or "").lower()
        if value in WHATSAPP_CONNECTORS or "whatsapp" in value:
            return True
    return False


def _client_number(ctx, doctype: str, target: str, chat: dict) -> str:
    """
    Número del cliente para el par from/to del WhatsApp Message.

    Open Channels no siempre expone el teléfono en el chat, así que se
    toma del Lead/Deal ya migrado. Sin número, los mensajes se guardan
    igual: el vínculo con el documento es lo que importa, y el teléfono
    es informativo.
    """
    for key in ("PHONE", "USER_PHONE", "CLIENT_PHONE"):
        if chat.get(key):
            return normalize_phone(chat[key], ctx.country_code)

    numero = frappe.db.get_value(doctype, target, "mobile_no")
    return normalize_phone(numero, ctx.country_code) if numero else ""


def _as_chats(value) -> list[dict]:
    if isinstance(value, list):
        return [c for c in value if isinstance(c, dict)]
    if isinstance(value, dict):
        # Puede venir como {"CHAT_ID": n} suelto o como {clave: chat}.
        if "CHAT_ID" in value or "ID" in value:
            return [value]
        return [c for c in value.values() if isinstance(c, dict)]
    return []


def _as_messages(value) -> list[dict]:
    if isinstance(value, dict):
        for key in ("messages", "MESSAGES", "result"):
            if isinstance(value.get(key), list):
                return value[key]
        return [m for m in value.values() if isinstance(m, dict)]
    if isinstance(value, list):
        return [m for m in value if isinstance(m, dict)]
    return []
