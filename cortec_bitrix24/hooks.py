# Copyright (C) 2026 Corporación de Tecnología CORTEC S.R.L.
# SPDX-License-Identifier: AGPL-3.0-or-later

from . import __version__ as app_version

app_name = "cortec_bitrix24"
app_title = "CORTEC Bitrix24"
app_publisher = "Corporación de Tecnología CORTEC S.R.L."
app_description = (
    "Migración incremental e idempotente de Bitrix24 a Frappe CRM: "
    "entidades CRM, historial de correos y WhatsApp (Open Channels), "
    "adjuntos y mapeo de los formularios web."
)
app_email = "soporte@tecnocr.net"
app_license = "AGPL-3.0"

# ---------------------------------------------------------------------------
# Apps de las que depende esta app.
#
# Solo `crm`: es donde viven los doctypes destino (CRM Lead, CRM Deal,
# CRM Organization, CRM Task, FCRM Note, CRM Call Log).
#
# NO se declaran aquí:
#   - `helpdesk`      — esta migración no crea HD Tickets.
#   - `erpnext`       — licencia GPL-3.0; no se combina con AGPL-3.0.
#   - `frappe_whatsapp` — opcional: se detecta en runtime
#                        (migration/openlines.py). Sin él, el historial de
#                        WhatsApp cae a Communication con medium "Chat".
#   - `cortec_helpdesk` — opcional: si está instalado se respetan sus campos
#                        (custom_account_manager, custom_comentarios) y se
#                        silencian sus avisos durante la corrida
#                        (migration/runner.py).
# ---------------------------------------------------------------------------
required_apps = ["crm"]

# ---------------------------------------------------------------------------
# Fixtures
#
# Los Custom Field que genera la fase 3 (UF_CRM_* → Custom Field) se marcan
# con module "Cortec Bitrix24", de modo que `bench export-fixtures` los
# recoja y viajen al sitio de producción junto con la app.
# ---------------------------------------------------------------------------
fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [["module", "=", "Cortec Bitrix24"]],
    },
]

# ---------------------------------------------------------------------------
# Esta app NO registra doc_events ni sobrescribe permisos.
#
# Es deliberado: solo escribe documentos durante una corrida explícita, y
# cualquier hook propio se dispararía también sobre el tráfico normal del
# CRM una vez terminada la migración.
# ---------------------------------------------------------------------------
