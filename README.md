# CORTEC Bitrix24

**English** · [Español](README.es.md)

Bitrix24 to Frappe CRM migration for Corporación de Tecnología CORTEC S.R.L.

The app's interface (field labels, buttons, messages and run reports) is
in Spanish. This document quotes those labels as they appear on screen.

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).

## What it does

It brings the contents of a Bitrix24 portal into Frappe CRM by reading
its REST API, **incrementally and idempotently**: the migration can be
run as a dry run, repeated as many times as needed, and finished with a
delta pass on cutover day.

| Phase | In Bitrix24 | In Frappe |
| ----- | ----------- | --------- |
| 1 | Stages, sources, industries, pipelines | CRM Lead Status, CRM Deal Status, CRM Lead Source, CRM Industry |
| 2 | Users | Bitrix24 User Map (matched by email; **does not create users**) |
| 3 | Custom fields `UF_CRM_*` | Custom Field on the target doctype |
| 4 | Companies | CRM Organization |
| 5 | Contacts | Contact (+ Contact Email / Contact Phone) |
| 6 | Leads | CRM Lead |
| 7 | Deals | CRM Deal (+ contacts table) |
| 8 | Emails, calls, to-dos and comments from the CRM timeline | Communication, CRM Call Log, CRM Task, FCRM Note |
| 9 | Tasks and Projects module (`tasks.task.*`) | CRM Task |
| 10 | WhatsApp from Open Channels | WhatsApp Message |
| 11 | Attachments | File (downloaded along with their email, message or task) |
| 12 | Web forms | Bitrix24 Web Form (field mapping, for the cutover) |

It does **not** migrate support tickets to Helpdesk (Bitrix24 is used
here as a CRM), quotes or invoices.

## Structure

```
cortec_bitrix24/
├── LICENSE
├── setup.py
├── requirements.txt
├── README.md               # English; README.es.md in Spanish
└── cortec_bitrix24/
    ├── __init__.py
    ├── hooks.py
    ├── api.py                  # start_migration, verify_counts, export_webform_mapping
    ├── utils.py                # E.164 phones, dates, BBCode
    ├── bitrix/
    │   ├── client.py           # batch, ID pagination, rate limit, retries
    │   └── errors.py
    ├── migration/
    │   ├── runner.py           # phases, checkpoints, notification silencing
    │   ├── idmap.py            # upsert() — the core of idempotency
    │   ├── dicts.py            # phase 1       ├── tasks.py       # phase 9
    │   ├── users.py            # phase 2       ├── openlines.py   # phase 10
    │   ├── userfields.py       # phase 3       ├── files.py       # phase 11
    │   ├── companies.py        # phase 4       └── webforms.py    # phase 12
    │   ├── contacts.py         # phase 5
    │   ├── leads.py            # phase 6
    │   ├── deals.py            # phase 7
    │   └── activities.py       # phase 8
    └── tests/
```

## Installation

```bash
bench get-app cortec_bitrix24 https://github.com/pvanderlaatu/cortec_bitrix24
bench --site site.domain.com install-app cortec_bitrix24
bench --site site.domain.com migrate
bench restart
```

Requires `crm` (Frappe CRM). `frappe_whatsapp` and `cortec_helpdesk` are
optional and detected at runtime.

## Setup

### 1. Inbound webhook in Bitrix24

In the portal: **Developer resources → Other → Inbound webhook**.
Grant these permissions:

| Permission | Needed for |
| --- | --- |
| **CRM** (`crm`) | Leads, deals, contacts, companies, activities, forms |
| **Users** (`user`) | Mapping owners |
| **Drive** (`disk`) | Attachments |
| **Open Channels** (`imopenlines`) | WhatsApp conversations |
| **Chat and notifications** (`im`) | The messages in those conversations |
| **Tasks** (`task`) | Tasks and Projects module (phase 9) |
| **Workgroups** (`sonet_group`) | The project of each task |

Without the **CRM** permission the run cannot proceed. If another one is
missing, the phase that uses it is skipped (listed under
*fases_omitidas* in the report and in the log, naming the permission)
and the rest carry on.

Copy the URL, which looks like this:

```
https://cortec.bitrix24.es/rest/1/xxxxxxxxxxxxxxxx/
```

### 2. Bitrix24 Migration Settings

| Field | Recommended value |
| ----- | ----------------- |
| Portal URL | `https://cortec.bitrix24.es` |
| Webhook URL (*URL del webhook entrante*) | the one from the previous step |
| Pause between requests | `0.5` s (the portal allows ~2 req/s) |
| Sub-calls per batch | `50` (Bitrix24's cap) |
| Fallback user (*Usuario de respaldo*) | the agent who receives whatever has no owner |
| Country code | `506` |
| Dry run (*Corrida en seco*) | ✅ until the report has been reviewed |
| Bitrix24 stages (*Etapas de Bitrix24*) | *Asignar a los estados de Frappe CRM* (map to Frappe CRM's statuses) or *Crear como estados nuevos* (create new ones) |
| Silence CORTEC Helpdesk alerts | ✅ |
| Preserve original dates | ✅ |

Press **Probar conexión** (test connection). It saves the form first,
then returns the webhook's profile and the number of records in each
collection. Those are the figures the migration is checked against when
it finishes.

### 3. Dry run

**Migrar → Corrida en seco.** Runs are always launched from that menu:
it is what queues the job. It writes nothing to the CRM, but it **does
fill the maps** of users, stages and fields (*Bitrix24 User Map*,
*Status Map* and *Field Map*), which are configuration and what step 4
reviews. It produces a `Bitrix24 Migration Run` whose *Informe* (report)
field contains:

- expected counts per entity,
- Bitrix24 users with no matching `User` in Frappe,
- *etapas_propuestas*: which Frappe CRM status each stage would go to,
- the `UF_CRM_*` fields that will be created,
- under *antes_de_empezar*: the **scheduled alerts** that must be
  disabled and, if any, the notifications the real run would not be able
  to silence (see "Neither agents nor customers are notified").

### 4. Manual review before the cutover

With the report in hand:

1. **Bitrix24 User Map** — fill in the *Usuario de Frappe* field for the
   agents who should keep their leads. Anything entered by hand is not
   overwritten on later passes.
2. **Bitrix24 Status Map** — check the Frappe CRM status proposed for
   each stage and fix any that does not fit. A row fixed by hand is not
   overwritten again.
3. **Bitrix24 Field Map** — tick *Omitir* (skip) on the custom fields
   that are not worth carrying over. The companies' invoice requisites
   (`UF_LOGO`, `UF_STAMP`, `UF_DIRECTOR_SIGN`, `UF_ACCOUNTANT_SIGN`) come
   already skipped.

In a dry run, activities show up as "omitidas" and there are no WhatsApp
messages or comments: those phases hang each record from its already
migrated Lead or Contact, and a dry run has none. The real run does
migrate them.

### 5. Real run

Beforehand: disable the scheduled alerts the dry run listed
(*Notification* and *WhatsApp Notification*). Re-enable them **the next
day**.

**Migrar → Migración real.** It requires a completed dry run. It is
queued on the `long` queue (the workers for that queue must be running)
and can take hours: at two requests per second, 40,000 deals add up.

The run can be stopped and relaunched: each phase saves its cursor and
the next pass resumes where it left off.

### 6. Verification

```python
import frappe
frappe.call("cortec_bitrix24.api.verify_counts")
```

### 7. Cutover

1. Final delta pass (the button launches it using the date of the last
   completed run).
2. Export the form mapping:
   `frappe.call("cortec_bitrix24.api.export_webform_mapping")`, and use
   it to repoint the site's form to the `leads-intake` Worker.
3. Set the Bitrix24 portal to read-only.

## Decisions worth knowing about

### WhatsApp sending is disabled during phase 10

`frappe_whatsapp`'s `WhatsAppMessage.before_insert` calls
`send_outgoing()`, which **sends the message to Meta** when the type is
`Outgoing`. Inserting the history as is would resend to customers, one
by one, every WhatsApp message agents wrote over the years.

`openlines.py` replaces that method with a no-op for the duration of the
phase and restores it on exit. If the method does not exist (a newer
`frappe_whatsapp` moved it), **the phase aborts** instead of carrying on.

### Neither agents nor customers are notified

The migration must not notify anyone. But Frappe, Frappe CRM and
frappe_whatsapp notify when documents are created, and almost none of
that checks the `in_import` flag. Verified in their code:

| What fires | To whom | How it is prevented |
| --- | --- | --- |
| CRM Lead, Deal and Task assign their owner on creation: Notification Log and **email** ("X assigned … to you") | Agent | `quiet_notifications` |
| The same assignment in the `/crm` bell | Agent | `quiet_notifications` |
| "X assigned Y" comment dated on migration day | Timeline | `quiet_notifications` |
| Assignment rules (round-robin) | Agent | `quiet_notifications` |
| `/crm` notification for every inbound WhatsApp | Agent | `quiet_notifications` |
| Frappe CRM automations on new emails and WhatsApp messages | Customer or agent | `quiet_notifications` |
| **Automatic WhatsApp templates** on document events (e.g. "on Lead creation") | **Customer** | `quiet_notifications` |
| Resending the WhatsApp history | Customer | `openlines._dont_send` |
| CORTEC Helpdesk alerts (Telegram, browser, Raven) and Leads created from old WhatsApp messages | Agent | `in_bitrix24_migration` flag in cortec_helpdesk ≥ 1.0.14; with earlier versions, `silence_cortec_alerts` |
| *Notification* alerts triggered by events | Customer or agent | `in_import` |
| **Scheduled** alerts ("Days Before/After", scheduled WhatsApp) | Customer or agent | **By hand**: the dry run lists them |

`runner.quiet_notifications` replaces those functions with no-ops **only
in the worker process running the migration**, and restores them when it
finishes. The rest of the site keeps notifying as usual. Assignments are
still created: `/crm` shows each agent their own under "Assigned to me".

If a newer version of Frappe, Frappe CRM or frappe_whatsapp renames any
of those functions, **the real run aborts** before writing anything, and
the dry run warns about it.

Scheduled alerts are run by the scheduler, outside the migration: a
"1 day before due date" alert would fire for every migrated task due
tomorrow. That is why they must be disabled before the real run
(step 5).

`silence_cortec_alerts` still switches off `telegram_enabled`,
`browser_alerts_enabled` and `raven_enabled` as a fallback. If the
worker is killed abruptly they would stay off: **checking those
checkboxes when the run ends** is part of the wrap-up.

### Consents are imported separately, with their evidence

The phases do not touch `custom_acepta_promociones` or
`custom_consentimiento`: in cortec_helpdesk ≥ 1.0.12 those fields are
derived from the consent registry (`CORTEC Consent Record`), and under
Costa Rica's Law 8968 the burden of proof lies with whoever is
responsible for the database.

The evidence does exist in Bitrix24: the forms' "Agreements" keep one
record per submission with date, IP, URL and the CRM activity. The REST
API cannot list them, so they are exported by hand **after the real
run**, one CSV per agreement:

1. CRM forms → the form → **Agreements** (*Acuerdos*) → the agreement →
   list of consents (*Consentimiento del usuario*).
2. **ALL** (*TODO*) to show every row on one page, select the table and
   paste it into Excel.
3. Save as CSV. The columns *Fecha*, *Dirección IP*, *Origen* and
   *Dirección* are required, with those Spanish headers; the separator
   (`;`, `,` or tab) and the encoding do not matter.
4. Upload the CSV as a private File and import it, dry run first:

```bash
bench --site <site> execute cortec_bitrix24.api.import_webform_consents \
  --kwargs "{'file_url': '/private/files/promociones.csv', 'agreement': 'promociones', 'dry_run': 1}"
```

The report says how many rows were resolved per doctype and lists the
activities whose subject was not migrated. With `dry_run: 0` the records
are created with the channel "Bitrix24". Repeating the import duplicates
nothing. For the Privacy Policy use `'agreement': 'politica-privacidad'`.

Dates are in the portal's time.

#### Subscribing to the promotional list

To subscribe those who accepted the `promociones` agreement:

1. **Unsubscribes first.** If campaigns were sent from Bitrix24 (CRM
   Marketing), export the list of people who unsubscribed to a CSV with
   an email column (header *Correo*, *E-mail* or *Email*) and, if
   available, *Fecha* (date). Import it:

   ```bash
   bench --site <site> execute cortec_bitrix24.api.import_marketing_unsubscribes \
     --kwargs "{'file_url': '/private/files/bajas.csv', 'dry_run': 1}"
   ```

   Each unsubscribe becomes a revocation of the promotional consent. If
   the CSV has no date, the import date is used: the unsubscribe wins
   over any earlier consent.

2. **Then the consents, with `'subscribe': 1`.**

cortec_helpdesk decides by the **latest event** for each email:

- it only subscribes if that event is a granted consent;
- a historical consent never reactivates someone who unsubscribed;
- nobody with a site-wide email unsubscribe is subscribed.

The report counts those subscribed, those left out because of a later
unsubscribe, and those with no email. Adding someone to the list
**sends no email**.

### Suppressed documents are not rewritten

If a data subject requested the erasure of their data
(cortec_helpdesk ≥ 1.0.13), their documents are listed in
`CORTEC Suppressed Document`. `idmap.upsert` does not update those
documents or create notes, emails or attachments hanging from them, and
the consent import skips them. A notice is left in the run log.

What cannot be prevented from here is the data subject remaining in the
Bitrix24 portal while it is still active: they must be deleted there
too.

### `custom_account_manager` is filled in

The owner of each contact in Bitrix24 is written to
`Contact.custom_account_manager` if `cortec_helpdesk` is installed.
That is what makes its automatic ticket assignment work from the first
email that arrives after the cutover.

### The two things Bitrix24 calls a "task"

They are different collections and are migrated in different phases,
both to `CRM Task`:

- **To-dos from the CRM timeline** (`crm.activity.list` with
  `PROVIDER_ID` `CRM_TODO`, `TASK` or `CRM_REMINDER`) — phase 8. They
  always hang from a Lead, Deal, Contact or Company.
- **Tasks and Projects module** (`tasks.task.list`) — phase 9. **All**
  the portal's tasks are migrated, including internal ones unrelated to
  the CRM, which are left without a reference (in `CRM Task` only
  `title` is mandatory).

Beware of **priorities**: they are two different scales in the same
portal. CRM activities use 1/2/3 and tasks use 0/1/2. Applying one to
the other shifts every priority by one step; a test guards against it.

Bitrix24 status 4 ("supposedly completed": the assignee closed it and
the creator has yet to accept it) is migrated as `Done`. In a portal
that is being retired nobody will accept anything any more, and leaving
it `In Progress` would show finished work as pending.

### What does not fit in CRM Task

`CRM Task` has eight fields and a Bitrix24 task has many more. Without
inventing Custom Fields, whatever has no slot of its own is written as a
header inside `description`, which is a Text Editor: project, creator,
co-assignees, observers, estimated and spent time, the checklist as a
list of checkboxes, and the comments.

Task dependencies, sprints, templates and change history are not
migrated. An archived task does not need its dependencies: nothing is
going to be replanned.

**Comments may not make it.** `task.commentitem.getlist` has been
deprecated since `tasks 25.700.0` and does not work with the new task
card. The phase tries anyway and, if the portal no longer exposes it,
records that in the run report — precisely so that tasks without
comments are not mistaken for tasks that never had any. Recovering them
would require reading each task's chat with `im.dialog.messages.get`,
which is a whole phase of its own.

### The amount on leads

In Bitrix24 a lead has an amount (`OPPORTUNITY`); in Frappe CRM the CRM
Lead has no value field. It is written inside `custom_comentarios`
rather than forced into `annual_revenue`, which means the company's
yearly turnover and would distort reports.

### Stages are mapped to Frappe CRM's statuses

With *Asignar a los estados de Frappe CRM* (the default) no status is
created. Each stage is proposed against the existing ones: won stages to
the *Won* type (for leads, which have none, to the last in-progress
status), lost stages to the *Lost* type (junk ones to "Junk"), and
in-progress stages spread in order over the *Open* and *Ongoing* ones.
The proposal is in *Bitrix24 Status Map* from the dry run on.

With *Crear como estados nuevos* each stage is created with its Bitrix24
name, and in-progress ones get type `Open`. `cortec_helpdesk` ≥ 1.0.12
treats any status other than *Won* and *Lost* as open, so neither mode
duplicates Leads from WhatsApp.

## Troubleshooting

### Following a run

```python
import frappe
frappe.call("cortec_bitrix24.api.migration_status")
```

### Stopping a run

1. Stop the job: **RQ Job** (`/app/rq-job`) → filter *ID* by
   `bitrix24-migration-<run>` → **Stop Job**. Or restart the `long`
   queue worker.
2. Mark the run as aborted, so another one can be launched:

```bash
bench --site <site> execute cortec_bitrix24.api.abort_migration
```

With no arguments it aborts whichever run is queued or running. Marking
it does not stop the job, which is why step 1 comes first. A stopped
real run resumes where it left off; a dry run leaves nothing written in
the CRM.

### Seeing the errors of the last run

```python
import frappe
frappe.get_all("Bitrix24 Migration Log",
    filters={"level": "Error"},
    fields=["phase", "bitrix_id", "message"],
    order_by="creation desc", limit=20)
```

### Checking the mapping of a specific record

```python
import frappe
from cortec_bitrix24.migration import idmap

print(idmap.get_target(idmap.ENTITY_LEAD, "1234"))     # ('CRM Lead', 'CRM-LEAD-2024-00042')
print(idmap.get_target(idmap.ENTITY_DEAL, "987"))
```

### Unresolved users and stages

```python
import frappe

print(frappe.get_all("Bitrix24 User Map",
    filters={"user": ["is", "not set"]},
    fields=["bitrix_user_id", "bitrix_name", "bitrix_email"]))

print(frappe.get_all("Bitrix24 Status Map",
    filters={"crm_status": ["is", "not set"]},
    fields=["entity", "bitrix_status_id", "bitrix_label"]))
```

### Redoing a phase

Delete its entry from the *Cursores* field of the `Bitrix24 Migration
Run` and relaunch. To redo an entity **from scratch**, also delete its
rows from the ID Map — which makes the next pass create new documents
rather than update the existing ones:

```python
import frappe
frappe.db.delete("Bitrix24 ID Map", {"entity_type": "lead"})
frappe.db.commit()
```

## Tests

```bash
bench --site site.domain.com run-tests --app cortec_bitrix24
```

They cover what fails silently: the client's ID pagination (a cursor
that does not advance loops forever; one that skips loses records), the
serialization of `batch` commands, the webhook token never showing up in
a log, phone normalization — which is what keeps the same customer from
ending up as three contacts — and the run not notifying anyone: the
silencing is applied and restored, and a real run aborts if anything is
left unsilenced.

## Notes

- It does not modify existing Frappe, CRM, Helpdesk or ERPNext doctypes.
- It does not declare `erpnext` as a dependency (GPL-3.0) so as not to
  mix it with AGPL-3.0, same as `cortec_helpdesk`.
- Compatible with Frappe v15/v16 and Frappe CRM v1.86.0.
- It does not require ERPNext: tasks go to Frappe CRM's `CRM Task`, not
  to ERPNext's `Task`/`Project` doctypes (GPL-3.0).
