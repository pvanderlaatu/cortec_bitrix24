# CORTEC Bitrix24

[English](README.md) · **Español**

Migración de Bitrix24 a Frappe CRM para Corporación de Tecnología CORTEC S.R.L.

## Licencia

AGPL-3.0-or-later — Ver archivo [LICENSE](LICENSE).

## Qué hace

Trae a Frappe CRM el contenido de un portal de Bitrix24 leyéndolo por su
API REST, de forma **incremental e idempotente**: la migración se puede
correr en seco, repetir tantas veces como haga falta, y rematar con una
pasada delta el día del corte.

| Fase | En Bitrix24 | En Frappe |
| ---- | ----------- | --------- |
| 1 | Etapas, fuentes, industrias, embudos | CRM Lead Status, CRM Deal Status, CRM Lead Source, CRM Industry |
| 2 | Usuarios | Bitrix24 User Map (empareja por correo; **no crea usuarios**) |
| 3 | Campos personalizados `UF_CRM_*` | Custom Field en el doctype destino |
| 4 | Empresas | CRM Organization |
| 5 | Contactos | Contact (+ Contact Email / Contact Phone) |
| 6 | Leads | CRM Lead |
| 7 | Negociaciones | CRM Deal (+ tabla de contactos) |
| 8 | Correos, llamadas, to-dos y comentarios del timeline del CRM | Communication, CRM Call Log, CRM Task, FCRM Note |
| 9 | Módulo Tareas y proyectos (`tasks.task.*`) | CRM Task |
| 10 | WhatsApp de Open Channels | WhatsApp Message |
| 11 | Adjuntos | File (se bajan junto con su correo, mensaje o tarea) |
| 12 | Formularios web | Bitrix24 Web Form (mapeo de campos, para el corte) |

**No** migra: tickets de soporte a Helpdesk (Bitrix24 se usa aquí como CRM),
presupuestos ni facturas.

## Estructura

```
cortec_bitrix24/
├── LICENSE
├── setup.py
├── requirements.txt
├── README.md               # en inglés; README.es.md en español
└── cortec_bitrix24/
    ├── __init__.py
    ├── hooks.py
    ├── api.py                  # start_migration, verify_counts, export_webform_mapping
    ├── utils.py                # teléfonos E.164, fechas, BBCode
    ├── bitrix/
    │   ├── client.py           # batch, paginación por ID, rate limit, reintentos
    │   └── errors.py
    ├── migration/
    │   ├── runner.py           # fases, checkpoints, silenciado de avisos
    │   ├── idmap.py            # upsert() — el núcleo de la idempotencia
    │   ├── dicts.py            # fase 1        ├── tasks.py       # fase 9
    │   ├── users.py            # fase 2        ├── openlines.py   # fase 10
    │   ├── userfields.py       # fase 3        ├── files.py       # fase 11
    │   ├── companies.py        # fase 4        └── webforms.py    # fase 12
    │   ├── contacts.py         # fase 5
    │   ├── leads.py            # fase 6
    │   ├── deals.py            # fase 7
    │   └── activities.py       # fase 8
    └── tests/
```

## Instalación

```bash
bench get-app cortec_bitrix24 https://github.com/pvanderlaatu/cortec_bitrix24
bench --site sitio.dominio.com install-app cortec_bitrix24
bench --site sitio.dominio.com migrate
bench restart
```

Requiere `crm` (Frappe CRM). `frappe_whatsapp` y `cortec_helpdesk` son
opcionales y se detectan en tiempo de ejecución.

## Configuración

### 1. Webhook entrante en Bitrix24

En el portal: **Recursos para desarrolladores → Otros → Webhook entrante**.
Marcar los permisos:

| Permiso | Lo necesita |
| --- | --- |
| **CRM** (`crm`) | Leads, negociaciones, contactos, empresas, actividades, formularios |
| **Usuarios** (`user`) | Mapeo de responsables |
| **Drive** (`disk`) | Adjuntos |
| **Canales abiertos** (`imopenlines`) | Conversaciones de WhatsApp |
| **Chat y notificaciones** (`im`) | Los mensajes de esas conversaciones |
| **Tareas** (`task`) | Módulo Tareas y proyectos (fase 9) |
| **Grupos de trabajo** (`sonet_group`) | Proyecto de cada tarea |

Sin el permiso **CRM** la corrida no avanza. Si falta otro, la fase que
lo usa se omite (queda en *fases_omitidas* del informe y en el log, con
el nombre del permiso) y el resto sigue.

Copiar la URL, que tiene esta forma:

```
https://cortec.bitrix24.es/rest/1/xxxxxxxxxxxxxxxx/
```

### 2. Bitrix24 Migration Settings

| Campo | Valor recomendado |
| ----- | ----------------- |
| URL del portal | `https://cortec.bitrix24.es` |
| URL del webhook | la del paso anterior |
| Pausa entre peticiones | `0.5` s (el portal admite ~2 req/s) |
| Sub-llamadas por batch | `50` (tope de Bitrix24) |
| Usuario de respaldo | el agente que reciba lo que no tenga dueño |
| Código de país | `506` |
| Corrida en seco | ✅ hasta haber revisado el informe |
| Etapas de Bitrix24 | *Asignar a los estados de Frappe CRM* (o *Crear como estados nuevos*) |
| Silenciar avisos de CORTEC Helpdesk | ✅ |
| Conservar fechas originales | ✅ |

Pulsar **Probar conexión**: guarda el formulario y devuelve el perfil
del webhook y el número de registros de cada colección. Esas cifras son contra las que se
coteja la migración al terminar.

### 3. Corrida en seco

**Migrar → Corrida en seco.** Las corridas se lanzan siempre desde ese
menú: es el que encola el job. No escribe nada en el CRM, pero **sí
llena los mapas** de usuarios, etapas y campos (*Bitrix24 User Map*,
*Status Map* y *Field Map*), que son configuración y es lo que se revisa
en el paso 4. Produce un `Bitrix24 Migration Run` cuyo campo *Informe*
trae:

- conteos previstos por entidad,
- usuarios de Bitrix24 sin `User` equivalente en Frappe,
- *etapas_propuestas*: a qué estado de Frappe CRM iría cada etapa,
- campos `UF_CRM_*` que se van a crear,
- en *antes_de_empezar*: las **alertas programadas** que hay que
  desactivar y, si las hubiera, las notificaciones que la corrida real
  no podría silenciar (ver «Ni agentes ni clientes reciben avisos»).

### 4. Revisión manual antes del corte

Con el informe en la mano:

1. **Bitrix24 User Map** — rellenar el campo *Usuario de Frappe* de los
   agentes que deban conservar sus leads. Lo que se ponga a mano no se
   vuelve a pisar en las pasadas siguientes.
2. **Bitrix24 Status Map** — revisar el estado de Frappe CRM propuesto
   para cada etapa y corregir el que no encaje. Una fila corregida a mano
   no se vuelve a pisar.
3. **Bitrix24 Field Map** — marcar *Omitir* en los campos personalizados
   que no valga la pena arrastrar. Los requisitos de factura de las
   empresas (`UF_LOGO`, `UF_STAMP`, `UF_DIRECTOR_SIGN`,
   `UF_ACCOUNTANT_SIGN`) ya vienen omitidos.

En seco las actividades salen como «omitidas» y no hay WhatsApp ni
comentarios: esas fases cuelgan cada registro de su Lead o Contacto ya
migrado, y en seco no hay ninguno. La corrida real sí los migra.

### 5. Corrida real

Antes: desactivar las alertas programadas que listó la corrida en seco
(*Notification* y *WhatsApp Notification*). Reactivarlas **al día
siguiente**.

**Migrar → Migración real.** Exige que exista una corrida en seco
completada. Se encola en la cola `long` (los workers de esa cola deben
estar activos) y puede tardar horas: a dos peticiones por segundo, 40 000
negociaciones son unas cuantas.

La corrida se puede parar y relanzar: cada fase guarda su cursor y la
siguiente pasada sigue donde quedó.

### 6. Cotejo

```python
import frappe
frappe.call("cortec_bitrix24.api.verify_counts")
```

### 7. Corte

1. Última pasada delta (el propio botón la lanza usando la fecha de la
   última corrida completa).
2. Exportar el mapeo de formularios:
   `frappe.call("cortec_bitrix24.api.export_webform_mapping")`, y usarlo
   para reapuntar el formulario del sitio al Worker `leads-intake`.
3. Poner el portal de Bitrix24 en solo lectura.

## Decisiones que conviene conocer

### El envío de WhatsApp se desactiva durante la fase 10

`WhatsAppMessage.before_insert` de `frappe_whatsapp` llama a
`send_outgoing()`, que **manda el mensaje a Meta** cuando el tipo es
`Outgoing`. Insertar el histórico sin más reenviaría a los clientes, uno
a uno, todos los WhatsApp que los agentes escribieron en años.

`openlines.py` sustituye ese método por un no-op mientras dura la fase y
lo restaura al salir. Si el método no existe (una versión nueva de
`frappe_whatsapp` lo movió), **la fase se aborta** en lugar de seguir.

### Ni agentes ni clientes reciben avisos

La migración no debe notificar a nadie. Pero Frappe, Frappe CRM y
frappe_whatsapp notifican al crear documentos, y casi nada de eso mira
la bandera `in_import`. Revisado en su código:

| Qué dispara | A quién | Cómo se evita |
| --- | --- | --- |
| CRM Lead, Deal y Task asignan a su responsable al crearse: Notification Log y **correo** («X le asignó…») | Agente | `quiet_notifications` |
| La misma asignación en la campana de `/crm` | Agente | `quiet_notifications` |
| Comentario «X asignó a Y» con la fecha de la migración | Timeline | `quiet_notifications` |
| Reglas de asignación (round-robin) | Agente | `quiet_notifications` |
| Aviso en `/crm` por cada WhatsApp entrante | Agente | `quiet_notifications` |
| Automatizaciones de Frappe CRM sobre correos y WhatsApp nuevos | Cliente o agente | `quiet_notifications` |
| **Plantillas automáticas de WhatsApp** por evento (p. ej. «al crear un Lead») | **Cliente** | `quiet_notifications` |
| Reenvío del histórico de WhatsApp | Cliente | `openlines._dont_send` |
| Avisos de CORTEC Helpdesk (Telegram, navegador, Raven) y Leads por WhatsApp antiguos | Agente | Bandera `in_bitrix24_migration` en cortec_helpdesk ≥ 1.0.14; con versiones anteriores, `silence_cortec_alerts` |
| Alertas *Notification* disparadas por evento | Cliente o agente | `in_import` |
| Alertas **programadas** («Days Before/After», WhatsApp programados) | Cliente o agente | **A mano**: la corrida en seco las lista |

`runner.quiet_notifications` sustituye esas funciones por no-ops **solo
en el proceso del worker que ejecuta la corrida**, y las restaura al
terminar. El resto del sitio sigue notificando con normalidad. Las
asignaciones sí se crean: `/crm` muestra a cada agente lo suyo en
«Asignados a mí».

Si una versión nueva de Frappe, Frappe CRM o frappe_whatsapp renombra
alguna de esas funciones, **la corrida real se aborta** antes de
escribir nada, y la corrida en seco lo avisa.

Las alertas programadas las ejecuta el scheduler, fuera de la corrida:
una alerta «1 día antes del vencimiento» avisaría por cada tarea
migrada que venza mañana. Por eso hay que desactivarlas antes de la
corrida real (paso 5).

`silence_cortec_alerts` sigue apagando `telegram_enabled`,
`browser_alerts_enabled` y `raven_enabled` como respaldo. Si el worker
muere de golpe quedarían apagados: **revisar esos checks al terminar**
es parte del cierre.

### Los consentimientos se importan aparte, con su evidencia

Las fases no tocan `custom_acepta_promociones` ni
`custom_consentimiento`: en cortec_helpdesk ≥ 1.0.12 esos campos se
derivan del registro de consentimientos (`CORTEC Consent Record`), y
bajo la Ley 8968 la carga de la prueba recae sobre el responsable de la
base de datos.

La evidencia sí existe en Bitrix24: los «Acuerdos» de los formularios
guardan un registro por envío con fecha, IP, URL y la actividad del CRM.
La API REST no permite listarlo, así que se exporta a mano **después de
la corrida real**, un CSV por acuerdo:

1. Formularios del CRM → el formulario → **Acuerdos** → el acuerdo →
   lista de consentimientos («Consentimiento del usuario»).
2. **TODO** para ver todas las filas en una página, seleccionar la tabla
   y pegarla en Excel.
3. Guardar como CSV. Hacen falta las columnas Fecha, Dirección IP, Origen
   y Dirección; el separador (`;`, `,` o tabulador) y la codificación dan
   igual.
4. Subir el CSV como File privado e importar, primero en seco:

```bash
bench --site <sitio> execute cortec_bitrix24.api.import_webform_consents \
  --kwargs "{'file_url': '/private/files/promociones.csv', 'agreement': 'promociones', 'dry_run': 1}"
```

El informe dice cuántas filas se resolvieron por doctype y lista las
actividades sin titular migrado. Con `dry_run: 0` se crean los
registros con canal «Bitrix24». Repetir la importación no duplica nada.
Para la Política de Privacidad se usa `'agreement': 'politica-privacidad'`.

Las fechas son la hora del portal.

#### Suscribir a la lista promocional

Para suscribir a quienes aceptaron el acuerdo «promociones»:

1. **Primero, las bajas.** Si se enviaron campañas desde Bitrix24 (CRM
   Marketing), exportar la lista de personas que se dieron de baja a un
   CSV con una columna de correo («Correo», «E-mail» o «Email») y, si la
   trae, «Fecha». Importarla:

   ```bash
   bench --site <sitio> execute cortec_bitrix24.api.import_marketing_unsubscribes \
     --kwargs "{'file_url': '/private/files/bajas.csv', 'dry_run': 1}"
   ```

   Cada baja queda como revocación del consentimiento publicitario. Si
   el CSV no trae fecha se usa la de la importación: la baja gana a
   cualquier consentimiento anterior.

2. **Después, los consentimientos con `'subscribe': 1`.**

cortec_helpdesk decide por el **último evento** de cada correo:

- solo suscribe si es un consentimiento otorgado;
- un consentimiento histórico nunca reactiva a quien se dio de baja;
- nadie con una baja global de correos del sitio se suscribe.

El informe cuenta los suscritos, los que no por una baja posterior y los
que no tienen correo. Dar de alta en la lista **no envía ningún correo**.

### Los documentos suprimidos no se reescriben

Si un titular pidió la supresión de sus datos (cortec_helpdesk ≥ 1.0.13),
sus documentos quedan en `CORTEC Suppressed Document`. `idmap.upsert`
no actualiza esos documentos ni crea notas, correos o adjuntos colgados
de ellos, y la importación de consentimientos los omite. Queda un aviso
en el log de la corrida.

Lo que no se puede evitar desde aquí es que el titular siga en el portal
de Bitrix24 mientras esté activo: hay que borrarlo también allí.

### `custom_account_manager` sí se rellena

El responsable de cada contacto en Bitrix24 se escribe en
`Contact.custom_account_manager` si `cortec_helpdesk` está instalado.
Es lo que hace que su asignación automática de tickets funcione desde el
primer correo que entre tras el corte.

### Las dos cosas que Bitrix24 llama «tarea»

Son colecciones distintas y se migran en fases distintas, ambas a
`CRM Task`:

- **To-dos del timeline del CRM** (`crm.activity.list` con `PROVIDER_ID`
  `CRM_TODO`, `TASK` o `CRM_REMINDER`) — fase 8. Siempre cuelgan de un
  Lead, Deal, Contacto o Empresa.
- **Módulo Tareas y proyectos** (`tasks.task.list`) — fase 9. Se migran
  **todas** las tareas del portal, también las internas sin relación con
  el CRM, que quedan sin referencia (en `CRM Task` solo `title` es
  obligatorio).

Cuidado con las **prioridades**: son dos escalas distintas en el mismo
portal. Las actividades del CRM usan 1/2/3 y las tareas 0/1/2. Aplicar
una a la otra desplaza todas las prioridades un escalón; hay un test que
lo vigila.

El estado 4 de Bitrix24 («supuestamente completada»: el ejecutor la
cerró y falta que el creador la acepte) se migra como `Done`. En un
portal que se va a retirar nadie va a aceptar ya nada, y dejarla
`In Progress` mostraría como pendiente lo que está hecho.

### Lo que no cabe en CRM Task

`CRM Task` tiene ocho campos y una tarea de Bitrix24 tiene muchos más.
Sin inventar Custom Fields, lo que no tiene hueco propio se escribe como
una cabecera dentro de `description`, que es un Text Editor: proyecto,
creador, co-ejecutores, observadores, tiempo estimado e invertido, el
checklist como lista de casillas y los comentarios.

No se migran dependencias entre tareas, sprints, plantillas ni historial
de cambios. Una tarea archivada no necesita sus dependencias: ya no se
va a replanificar nada.

**Los comentarios pueden no llegar.** `task.commentitem.getlist` está
descontinuado desde `tasks 25.700.0` y no funciona con la ficha nueva de
tarea. La fase lo intenta igual y, si el portal ya no lo expone, lo
anota en el informe de la corrida — precisamente para que unas tareas
sin comentarios no se confundan con tareas que nunca los tuvieron.
Recuperarlos exigiría leer el chat de cada tarea con
`im.dialog.messages.get`, que es otra fase entera.

### El importe de los leads

En Bitrix24 un lead tiene importe (`OPPORTUNITY`); en Frappe CRM el
CRM Lead no tiene campo de valor. Se anota dentro de
`custom_comentarios` en vez de forzarlo dentro de `annual_revenue`, que
significa la facturación anual de la empresa y falsearía los informes.

### Las etapas se asignan a los estados de Frappe CRM

Con *Asignar a los estados de Frappe CRM* (por defecto) no se crea
ningún estado. Cada etapa se propone contra los que existen: ganadas al
de tipo *Won* (en leads, que no tienen ninguno, al último en curso),
perdidas al de tipo *Lost* (las de basura a «Junk»), y las en curso
repartidas en orden sobre los *Open* y *Ongoing*. La propuesta queda en
*Bitrix24 Status Map* desde la corrida en seco.

Con *Crear como estados nuevos* cada etapa se crea con su nombre de
Bitrix24, y las en curso con tipo `Open`. `cortec_helpdesk` ≥ 1.0.12
trata como abierto cualquier estado que no sea *Won* ni *Lost*, así que
ninguno de los dos modos duplica Leads por WhatsApp.

## Diagnóstico

### Seguir una corrida

```python
import frappe
frappe.call("cortec_bitrix24.api.migration_status")
```

### Detener una corrida

1. Parar el job: **Trabajo RQ** (`/app/rq-job`) → filtrar el
   *Identificador* por `bitrix24-migration-<corrida>` → **Stop Job**. O
   reiniciar el worker de la cola `long`.
2. Marcar la corrida como abortada, para poder lanzar otra:

```bash
bench --site <sitio> execute cortec_bitrix24.api.abort_migration
```

Sin argumentos aborta la que esté en cola o en curso. Marcarla no
detiene el job: por eso el paso 1 va primero. Una corrida real detenida
se reanuda donde quedó; una en seco no deja nada escrito en el CRM.

### Ver los errores de la última corrida

```python
import frappe
frappe.get_all("Bitrix24 Migration Log",
    filters={"level": "Error"},
    fields=["phase", "bitrix_id", "message"],
    order_by="creation desc", limit=20)
```

### Comprobar la correspondencia de un registro concreto

```python
import frappe
from cortec_bitrix24.migration import idmap

print(idmap.get_target(idmap.ENTITY_LEAD, "1234"))     # ('CRM Lead', 'CRM-LEAD-2024-00042')
print(idmap.get_target(idmap.ENTITY_DEAL, "987"))
```

### Usuarios y etapas sin resolver

```python
import frappe

print(frappe.get_all("Bitrix24 User Map",
    filters={"user": ["is", "not set"]},
    fields=["bitrix_user_id", "bitrix_name", "bitrix_email"]))

print(frappe.get_all("Bitrix24 Status Map",
    filters={"crm_status": ["is", "not set"]},
    fields=["entity", "bitrix_status_id", "bitrix_label"]))
```

### Rehacer una fase

Borrar su entrada del campo *Cursores* del `Bitrix24 Migration Run` y
relanzar. Para rehacer una entidad **desde cero**, borrar además sus
filas del ID Map — lo que hará que la siguiente pasada cree documentos
nuevos, no que actualice los existentes:

```python
import frappe
frappe.db.delete("Bitrix24 ID Map", {"entity_type": "lead"})
frappe.db.commit()
```

## Tests

```bash
bench --site sitio.dominio.com run-tests --app cortec_bitrix24
```

Cubren lo que falla en silencio: la paginación por ID del cliente (un
cursor que no avanza da un bucle infinito; uno que salta pierde
registros), la serialización de los comandos de `batch`, que el token
del webhook nunca salga en un log, y la normalización de teléfonos —de
la que depende que el mismo cliente no acabe siendo tres contactos—, y
que la corrida no notifique a nadie: el silenciado se aplica y se
restaura, y una corrida real se aborta si falta algo por silenciar.

## Notas

- No modifica doctypes existentes de Frappe, CRM, Helpdesk ni ERPNext.
- No declara `erpnext` como dependencia (GPL-3.0) para no mezclarlo con
  AGPL-3.0, igual que `cortec_helpdesk`.
- Compatible con Frappe v15/v16 y Frappe CRM v1.86.0.
- No requiere ERPNext: las tareas van a `CRM Task` de Frappe CRM, y no a
  los doctypes `Task`/`Project` de ERPNext (GPL-3.0).
