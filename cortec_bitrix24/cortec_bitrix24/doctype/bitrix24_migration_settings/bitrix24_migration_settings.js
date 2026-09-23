// Copyright (C) 2026 Corporación de Tecnología CORTEC S.R.L.
// SPDX-License-Identifier: AGPL-3.0-or-later

frappe.ui.form.on("Bitrix24 Migration Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Probar conexión"), () => probar_conexion(frm));

		frm.add_custom_button(__("Corrida en seco"), () => iniciar(frm, true), __("Migrar"));

		frm.add_custom_button(__("Migración real"), () => confirmar_real(frm), __("Migrar"));

		if (frm.doc.last_run) {
			frm.add_custom_button(__("Ver última corrida"), () => {
				frappe.set_route("Form", "Bitrix24 Migration Run", frm.doc.last_run);
			});
		}

		frm.add_custom_button(__("Incidencias"), () => {
			frappe.set_route("List", "Bitrix24 Migration Log", { level: "Error" });
		});

		if (frm.doc.dry_run) {
			frm.dashboard.set_headline_alert(
				__("Corrida en seco activa: la migración recorre el portal pero no escribe nada."),
				"blue"
			);
		} else {
			frm.dashboard.set_headline_alert(
				__("Corrida en seco DESACTIVADA: la próxima migración escribirá en este sitio."),
				"orange"
			);
		}
	},
});

function probar_conexion(frm) {
	frappe.dom.freeze(__("Consultando el portal…"));
	frappe
		.call(
			"cortec_bitrix24.cortec_bitrix24.doctype.bitrix24_migration_settings." +
				"bitrix24_migration_settings.test_connection"
		)
		.then((r) => {
			frappe.dom.unfreeze();
			if (!r.message) return;
			const d = r.message;
			const filas = Object.entries(d.conteos)
				.map(([k, v]) => `<tr><td>${frappe.utils.escape_html(k)}</td>
					<td style="text-align:right">${frappe.utils.escape_html(String(v))}</td></tr>`)
				.join("");
			frappe.msgprint({
				title: __("Conexión correcta"),
				indicator: "green",
				message: `
					<p><b>${__("Portal")}:</b> ${frappe.utils.escape_html(d.portal)}<br>
					<b>${__("Webhook de")}:</b> ${frappe.utils.escape_html(d.usuario || "")}
					${d.admin ? "(admin)" : ""}</p>
					<table class="table table-bordered"><thead><tr>
						<th>${__("Colección")}</th><th style="text-align:right">${__("Registros")}</th>
					</tr></thead><tbody>${filas}</tbody></table>
					<p class="text-muted">${__(
						"Estas cifras son contra las que se coteja la migración al terminar."
					)}</p>`,
			});
		})
		.catch(() => frappe.dom.unfreeze());
}

function confirmar_real(frm) {
	// La corrida real escribe miles de documentos; pedir confirmación
	// explícita evita el clic accidental sobre un sitio productivo.
	frappe.confirm(
		__(
			"Esto escribirá Leads, Deals, Contactos, Empresas y el historial de " +
				"comunicaciones en <b>{0}</b>.<br><br>¿Ya revisó una corrida en seco completa?",
			[frappe.boot.sitename]
		),
		() => iniciar(frm, false)
	);
}

function iniciar(frm, dry_run) {
	frappe.call({
		method: "cortec_bitrix24.api.start_migration",
		args: { dry_run: dry_run ? 1 : 0 },
		freeze: true,
		freeze_message: __("Encolando la migración…"),
		callback(r) {
			if (!r.message) return;
			frm.reload_doc();
			frappe.show_alert({
				message: __("Corrida {0} encolada", [r.message.run]),
				indicator: "green",
			});
			frappe.set_route("Form", "Bitrix24 Migration Run", r.message.run);
		},
	});
}
