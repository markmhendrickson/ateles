#!/usr/bin/env python3
"""One-off: print JSON args for Gmail draft_email (multipart/alternative).

Updated 2026-05-11: incorporates same-day SEPA payment of the €477,95 vitrine
saldo, so the Mark→Federico credit list shrinks to one line (office door
isolation €484) and the net principal rises to €2.011,39.
"""
import json

PLAIN = """Hola Federico,

Después del intercambio de febrero y de mi seguimiento del 4 de marzo, he hecho una revisión forense completa de toda nuestra historia: las facturas que me emitiste entre 2022 y 2024, los presupuestos pendientes (P_08/2022, P_18/2024 entre otros), todos mis movimientos bancarios de Ibercaja en cinco cuentas distintas durante 2021–2025, la transferencia Wise de noviembre de 2022, y el chat completo de WhatsApp entre nosotros desde 2021. El resultado es que mi reclamación inicial del 23/02/2026 contenía errores numéricos y omisiones que debo corregir ahora, antes de que mi abogado proceda con el burofax.

Esta carta, por tanto, sustituye íntegramente las cifras del 23 de febrero. La nueva cifra reclamada es menor que la anterior, y ya incorpora crédito honesto a tu favor por el único trabajo específico identificable que aún queda pendiente entre nosotros (el aislamiento de la puerta del despacho).

Antes de entrar en el desglose: hoy mismo, 11/05/2026 a las 07:34 horas, he ejecutado la transferencia SEPA del saldo del 40% de la Factura 01/2023 sobre la vitrina P_08/2022 — 477,95 €, desde mi cuenta Ibercaja terminada en 2862 a tu IBAN de Caixa de Arquitectos ES90 3183 0801 2410 0268 1920 (titular Ricard Solà Badell), con el concepto «FACTURA 01/2023 saldo vitrina P08/2022». Esa línea queda por tanto cerrada por mi parte de forma unilateral y de buena fe, con independencia de cómo se resuelva el resto. Adjunto el justificante bancario.

────────────────────────────────────────
RECONCILIACIÓN CORRECTA Y COMPLETA (con crédito honesto a tu favor)
────────────────────────────────────────

Tomo como base la tabla de reconciliación que te envié el 14 de agosto de 2024 por WhatsApp, y que tú aceptaste explícitamente el 16 de agosto a las 14:07 ("me parece corecto lo que tienes calculado"). Esa tabla sigue siendo la base correcta, y las líneas pendientes se resuelven así:

• Tú me debes — Reembolso adelanto armario habitación de invitados (cancelado por nosotros, línea 1 de la tabla de agosto): +1.306,00 €
• Tú me debes — Reembolso adelanto tres estanterías de nogal P_18/2024 (no instaladas; me obligó a contratar a otro carpintero por 5.065,06 €): +1.189,39 €
• Yo te debo — Aislamiento puerta despacho (instalado en agosto de 2023, jamás presupuestado ni facturado por ti): −484,00 €
• (Saldo Factura 01/2023 sobre vitrina P_08/2022 — 477,95 € — pagado hoy 11/05/2026 desde mi cuenta 2862 vía SEPA. Línea ya cerrada.)

NETO A MI FAVOR: 2.011,39 €

Algunas precisiones importantes sobre esta cifra y cómo difiere de mi correo del 23 de febrero:

1) El importe de las estanterías correcto es 1.189,39 €, no 2.532,53 €. Ese 2.532,53 € que cité en febrero corresponde en realidad al pago que tuve que hacer al carpintero sustituto (Estudi BCR Ebanisteria i Diseny, NIF B66776033) en septiembre de 2025 — no a un adelanto a ti. El adelanto real que te hice por las tres estanterías de nogal del P_18/2024 fue 1.189,39 €, pagado el 19/06/2024 desde mi cuenta Ibercaja terminada en 2862 por SEPA a la misma cuenta de Caixa de Arquitectos.

2) El importe del armario correcto es 1.306 € (con IVA), no 1.079,25 €. Esa cifra de 1.079,25 € mezclaba conceptos sin/con IVA y se asociaba erróneamente al P_37/2021 (ropero del dormitorio principal), que sí se entregó y no está en disputa. La cantidad correcta del adelanto del armario de invitados es 1.306 €, exactamente como figura en mi tabla del 14/08/2024 que aceptaste por escrito.

3) Acepto el crédito de 484 € por el aislamiento de la puerta del despacho. En febrero te dije que era un favor; al revisar nuestro intercambio de WhatsApp del 18–19 de junio de 2024 veo que el planteamiento que tú mismo propusiste fue «una factura abono con números negativos que van a descontar el adelanto que hiciste» — es decir, netear ese trabajo contra mis adelantos pendientes. Aplico esa misma lógica simétricamente y descuento los 484 € de la cifra reclamada.

4) El saldo de la vitrina P_08/2022 (Factura 01/2023, 477,95 €) ha quedado pagado hoy de mi parte, como he indicado al principio. No procede ya descontarlo, porque no es deuda viva. Al pagarlo, te quito por mi propia voluntad la única línea «en paz» que estaba documentada e impagada en mi columna y, simétricamente, la diferencia restante a mi favor es por tanto 2.011,39 € sin contraprestación posible.

5) No acepto los otros conceptos que mencionaste el 23 de febrero (mantención de las puertas correderas del pasillo y desmontaje de la estantería de nogal): nunca presupuestaste ni facturaste ninguno de los dos, no hay rastro documental que los soporte como obras facturables independientes, y la mantención de las correderas en su momento la presentaste expresamente como cortesía. Si los hubieras presupuestado y facturado en su día, hoy formarían parte de esta misma reconciliación.

La cifra 2.011,39 € es por tanto la posición final, calculada de la forma más favorable a ti que las pruebas permiten. El cálculo bruto (sin descontar el crédito de 484 € por el aislamiento de la puerta) sería 2.495,39 €.

────────────────────────────────────────
PRÓXIMOS PASOS Y PLAZO
────────────────────────────────────────

Estoy ya en contacto con mis abogados (Secod, Barcelona) sobre este asunto. Antes de avanzar, te doy una última oportunidad de resolverlo de mutuo acuerdo:

• Plazo: viernes 22 de mayo de 2026 — para que confirmes por escrito tu intención de pago de los 2.011,39 € y propongas un calendario concreto (pago único o, si tu situación de tesorería lo requiere, un fraccionamiento razonable a corto plazo, p. ej. tres mensualidades).

• Si no recibo confirmación de pago dentro de ese plazo, mi abogado procederá a enviarte un burofax con acuse de recibo y certificación de contenido a finales de la semana siguiente (29 de mayo de 2026), con la misma cifra y el mismo desglose que figuran arriba.

• A partir del burofax, si no hay pago dentro del plazo legal, se iniciará el correspondiente procedimiento monitorio ante el Juzgado de Primera Instancia de Barcelona. Por el importe en juego (inferior a 15.000 €), el monitorio es el cauce rápido y típico.

Te aclaro que, si llegamos al monitorio, mi reclamación se basará íntegramente en (a) tu propia aceptación por escrito de la tabla de reconciliación del 14/08/2024 («me parece corecto»), (b) los comprobantes bancarios de los adelantos efectivamente realizados, (c) la factura del carpintero sustituto que muestra el coste de rehacer el trabajo no entregado, (d) tus propias admisiones por WhatsApp de junio de 2024 sobre cómo se tenían que netear los trabajos no facturados, y (e) el justificante del pago de hoy del saldo de la vitrina, que demuestra mi cumplimiento íntegro de toda obligación documentada por mi parte. Todo el material está ya organizado.

Espero sinceramente que podamos cerrar esto sin necesidad de llegar al burofax. La cifra es razonable, está documentada por ambas partes, y la línea «en paz» sustantiva que podías invocar (el saldo de la vitrina) ha quedado hoy mismo cerrada por mi parte.

Quedo a la espera de tu respuesta antes del viernes 22 de mayo.

Saludos,
Mark"""

HTML = """<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#222;max-width:720px;\">
<p>Hola Federico,</p>
<p>Después del intercambio de febrero y de mi seguimiento del 4 de marzo, he hecho una revisión forense completa de toda nuestra historia: las facturas que me emitiste entre 2022 y 2024, los presupuestos pendientes (P_08/2022, P_18/2024 entre otros), todos mis movimientos bancarios de Ibercaja en cinco cuentas distintas durante 2021–2025, la transferencia Wise de noviembre de 2022, y el chat completo de WhatsApp entre nosotros desde 2021. El resultado es que mi reclamación inicial del 23/02/2026 contenía errores numéricos y omisiones que debo corregir ahora, antes de que mi abogado proceda con el burofax.</p>
<p>Esta carta, por tanto, sustituye íntegramente las cifras del 23 de febrero. La nueva cifra reclamada es <strong>menor</strong> que la anterior, y ya incorpora crédito honesto a tu favor por el único trabajo específico identificable que aún queda pendiente entre nosotros (el aislamiento de la puerta del despacho).</p>
<div style=\"margin:1em 0;padding:10px 14px;border-left:3px solid #2e7d32;background:#f1f8f3;\">
<p style=\"margin:0;\"><strong>Pago del saldo de la vitrina ya ejecutado.</strong> Hoy mismo, <strong>11/05/2026 a las 07:34 horas</strong>, he ejecutado la transferencia SEPA del saldo del 40% de la Factura 01/2023 sobre la vitrina P_08/2022 — <strong>477,95&nbsp;€</strong>, desde mi cuenta Ibercaja terminada en <strong>2862</strong> a tu IBAN de Caixa de Arquitectos <strong>ES90 3183 0801 2410 0268 1920</strong> (titular Ricard Solà Badell), con el concepto «<em>FACTURA 01/2023 saldo vitrina P08/2022</em>». Esa línea queda por tanto cerrada por mi parte de forma unilateral y de buena fe, con independencia de cómo se resuelva el resto. Adjunto el justificante bancario.</p>
</div>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">Reconciliación correcta y completa (con crédito honesto a tu favor)</h2>
<p>Tomo como base la tabla de reconciliación que te envié el <strong>14 de agosto de 2024</strong> por WhatsApp, y que tú aceptaste explícitamente el <strong>16 de agosto</strong> a las 14:07 (<em>«me parece corecto lo que tienes calculado»</em>). Esa tabla sigue siendo la base correcta, y las líneas pendientes se resuelven así:</p>
<table cellpadding=\"8\" cellspacing=\"0\" style=\"border-collapse:collapse;width:100%;max-width:680px;font-size:13px;border:1px solid #bbb;\">
<thead><tr style=\"background:#f3f3f3;\">
<th align=\"left\" style=\"border:1px solid #bbb;\">Dirección</th>
<th align=\"left\" style=\"border:1px solid #bbb;\">Concepto</th>
<th align=\"right\" style=\"border:1px solid #bbb;white-space:nowrap;\">Importe (IVA incl.)</th>
</tr></thead>
<tbody>
<tr><td style=\"border:1px solid #bbb;vertical-align:top;\">Tú me debes</td><td style=\"border:1px solid #bbb;\">Reembolso adelanto armario habitación de invitados (cancelado por nosotros, línea 1 de la tabla de agosto)</td><td align=\"right\" style=\"border:1px solid #bbb;\">+1.306,00&nbsp;€</td></tr>
<tr><td style=\"border:1px solid #bbb;vertical-align:top;\">Tú me debes</td><td style=\"border:1px solid #bbb;\">Reembolso adelanto <strong>tres</strong> estanterías de nogal P_18/2024 (no instaladas; me obligó a contratar a otro carpintero por 5.065,06&nbsp;€)</td><td align=\"right\" style=\"border:1px solid #bbb;\">+1.189,39&nbsp;€</td></tr>
<tr><td style=\"border:1px solid #bbb;vertical-align:top;\">Yo te debo</td><td style=\"border:1px solid #bbb;\">Aislamiento puerta despacho (instalado en agosto de 2023, jamás presupuestado ni facturado por ti)</td><td align=\"right\" style=\"border:1px solid #bbb;\">−484,00&nbsp;€</td></tr>
<tr><td style=\"border:1px solid #bbb;vertical-align:top;color:#666;font-style:italic;\">Pagado hoy</td><td style=\"border:1px solid #bbb;color:#666;font-style:italic;\">Saldo Factura 01/2023 sobre vitrina P_08/2022 — pagado por SEPA el 11/05/2026 desde la 2862. Línea ya cerrada.</td><td align=\"right\" style=\"border:1px solid #bbb;color:#666;font-style:italic;\">(477,95&nbsp;€ — n/a)</td></tr>
<tr style=\"background:#fafafa;font-weight:bold;\"><td style=\"border:1px solid #bbb;\" colspan=\"2\">NETO A MI FAVOR</td><td align=\"right\" style=\"border:1px solid #bbb;\">2.011,39&nbsp;€</td></tr>
</tbody></table>
<p>Algunas precisiones importantes sobre esta cifra y cómo difiere de mi correo del 23 de febrero:</p>
<ol style=\"margin-left:0;padding-left:1.2em;\">
<li style=\"margin-bottom:0.75em;\"><strong>Importe estanterías:</strong> el correcto es <strong>1.189,39&nbsp;€</strong>, no 2.532,53&nbsp;€. Ese 2.532,53&nbsp;€ que cité en febrero corresponde en realidad al pago que tuve que hacer al carpintero sustituto (Estudi BCR Ebanisteria i Diseny, NIF B66776033) en septiembre de 2025 — no a un adelanto a ti. El adelanto real que te hice por las <strong>tres</strong> estanterías de nogal del P_18/2024 fue 1.189,39&nbsp;€, pagado el 19/06/2024 desde mi cuenta Ibercaja terminada en <strong>2862</strong> por SEPA a la misma cuenta de Caixa de Arquitectos.</li>
<li style=\"margin-bottom:0.75em;\"><strong>Importe armario:</strong> el correcto es <strong>1.306&nbsp;€</strong> (con IVA), no 1.079,25&nbsp;€. Esa cifra de 1.079,25&nbsp;€ mezclaba conceptos sin/con IVA y se asociaba erróneamente al P_37/2021 (ropero del dormitorio principal), que <strong>sí se entregó y no está en disputa</strong>. La cantidad correcta del adelanto del armario de invitados es 1.306&nbsp;€, exactamente como figura en mi tabla del 14/08/2024 que aceptaste por escrito.</li>
<li style=\"margin-bottom:0.75em;\"><strong>Crédito 484&nbsp;€ (aislamiento puerta):</strong> lo acepto. En febrero te dije que era un favor; al revisar nuestro intercambio de WhatsApp del 18–19 de junio de 2024 veo que el planteamiento que tú mismo propusiste fue «una factura abono con números negativos que van a descontar el adelanto que hiciste» — es decir, netear ese trabajo contra mis adelantos pendientes. Aplico esa misma lógica simétricamente y descuento los 484&nbsp;€ de la cifra reclamada.</li>
<li style=\"margin-bottom:0.75em;\"><strong>Saldo vitrina P_08/2022 (Factura 01/2023, 477,95&nbsp;€):</strong> ha quedado <strong>pagado hoy</strong> de mi parte, como he indicado al principio. No procede ya descontarlo, porque no es deuda viva. Al pagarlo, te quito por mi propia voluntad la única línea «en paz» que estaba documentada e impagada en mi columna y, simétricamente, la diferencia restante a mi favor es por tanto <strong>2.011,39&nbsp;€</strong> sin contraprestación posible.</li>
<li style=\"margin-bottom:0.75em;\"><strong>Otros conceptos del 23 de febrero:</strong> no acepto la mantención de las puertas correderas del pasillo ni el desmontaje de la estantería de nogal: nunca presupuestaste ni facturaste ninguno de los dos, no hay rastro documental que los soporte como obras facturables independientes, y la mantención de las correderas en su momento la presentaste expresamente como cortesía. Si los hubieras presupuestado y facturado en su día, hoy formarían parte de esta misma reconciliación.</li>
</ol>
<p>La cifra <strong>2.011,39&nbsp;€</strong> es por tanto la posición final, calculada de la forma más favorable a ti que las pruebas permiten. El cálculo bruto (sin descontar el crédito de 484&nbsp;€ por el aislamiento de la puerta) sería <strong>2.495,39&nbsp;€</strong>.</p>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">Próximos pasos y plazo</h2>
<p>Estoy ya en contacto con mis abogados (Secod, Barcelona) sobre este asunto. Antes de avanzar, te doy una última oportunidad de resolverlo de mutuo acuerdo:</p>
<ul style=\"margin-left:0;padding-left:1.2em;\">
<li style=\"margin-bottom:0.65em;\"><strong>Plazo: viernes 22 de mayo de 2026</strong> — para que confirmes por escrito tu intención de pago de los 2.011,39&nbsp;€ y propongas un calendario concreto (pago único o, si tu situación de tesorería lo requiere, un fraccionamiento razonable a corto plazo, p. ej. tres mensualidades).</li>
<li style=\"margin-bottom:0.65em;\">Si no recibo confirmación de pago dentro de ese plazo, mi abogado procederá a enviarte un <strong>burofax</strong> con acuse de recibo y certificación de contenido a finales de la semana siguiente (<strong>29 de mayo de 2026</strong>), con la misma cifra y el mismo desglose que figuran arriba.</li>
<li style=\"margin-bottom:0.65em;\">A partir del burofax, si no hay pago dentro del plazo legal, se iniciará el correspondiente <strong>procedimiento monitorio</strong> ante el Juzgado de Primera Instancia de Barcelona. Por el importe en juego (inferior a 15.000&nbsp;€), el monitorio es el cauce rápido y típico.</li>
</ul>
<p>Te aclaro que, si llegamos al monitorio, mi reclamación se basará íntegramente en (a) tu propia aceptación por escrito de la tabla de reconciliación del 14/08/2024 («me parece corecto»), (b) los comprobantes bancarios de los adelantos efectivamente realizados, (c) la factura del carpintero sustituto que muestra el coste de rehacer el trabajo no entregado, (d) tus propias admisiones por WhatsApp de junio de 2024 sobre cómo se tenían que netear los trabajos no facturados, y (e) el justificante del pago de hoy del saldo de la vitrina, que demuestra mi cumplimiento íntegro de toda obligación documentada por mi parte. Todo el material está ya organizado.</p>
<p>Espero sinceramente que podamos cerrar esto sin necesidad de llegar al burofax. La cifra es razonable, está documentada por ambas partes, y la línea «en paz» sustantiva que podías invocar (el saldo de la vitrina) ha quedado hoy mismo cerrada por mi parte.</p>
<p>Quedo a la espera de tu respuesta antes del viernes 22 de mayo.</p>
<p>Saludos,<br/>Mark</p>
</div>"""

payload = {
    "to": ["federico@anotheragain.es"],
    "subject": "Re: Devolución de adelantos – estanterías y armario 2ª planta",
    "threadId": "19c8a69be45623e8",
    "inReplyTo": "19cb8e9d8570d6a3",
    "mimeType": "multipart/alternative",
    "body": PLAIN,
    "htmlBody": HTML,
    "attachments": [
        "/Users/markmhendrickson/repos/ateles/.cursor/federico_dispute_evidence_2026/2026-05-11_TransferenciaDestinatario_477_95_vitrina_saldo.pdf",
    ],
}
print(json.dumps(payload, ensure_ascii=False))
