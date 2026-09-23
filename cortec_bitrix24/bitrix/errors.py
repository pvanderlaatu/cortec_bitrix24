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
cortec_bitrix24.bitrix.errors
==============================
Jerarquía de errores del cliente REST.

La distinción importa para el runner: un B24FatalError aborta la corrida
completa (no tiene sentido seguir pidiendo 40 000 registros con un token
inválido), mientras que un B24Error corriente se registra contra el
registro que lo provocó y la fase continúa con el siguiente.
"""


class B24Error(Exception):
    """
    Error devuelto por el portal para una llamada concreta.

    Recuperable: el runner lo anota en Bitrix24 Migration Log y sigue.
    """

    def __init__(self, code: str, description: str = "", method: str = ""):
        self.code = code or "UNKNOWN"
        self.description = description or ""
        self.method = method or ""
        super().__init__(
            f"{self.method or 'bitrix'}: {self.code}"
            + (f" — {self.description}" if self.description else "")
        )


class B24FatalError(B24Error):
    """
    Error que invalida toda la corrida: token revocado, portal caído,
    método inexistente por plan del portal, o agotamiento de reintentos.
    """


class B24AuthError(B24FatalError):
    """Token del webhook inválido, revocado o sin el scope necesario."""


class B24RateLimitError(B24Error):
    """
    QUERY_LIMIT_EXCEEDED. El cliente la maneja internamente con backoff;
    solo escapa al llamador si se agotan los reintentos, y en ese caso
    ya viaja envuelta en un B24FatalError.
    """


# Códigos que nunca mejoran reintentando: son de autenticación/permisos.
AUTH_ERROR_CODES = {
    "INVALID_CREDENTIALS",
    "NO_AUTH_FOUND",
    "INVALID_TOKEN",
    "ACCESS_DENIED",
    "insufficient_scope",
    "WRONG_AUTH_TYPE",
    "PORTAL_DELETED",
    "AUTHORIZATION_ERROR",
}

# Códigos que sí mejoran esperando.
RETRYABLE_ERROR_CODES = {
    "QUERY_LIMIT_EXCEEDED",
    "OPERATION_TIME_LIMIT",
    "INTERNAL_SERVER_ERROR",
    "OVERLOAD_LIMIT",
}
