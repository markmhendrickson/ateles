#!/usr/bin/env python3
"""One-off: print JSON args for Gmail draft_email (multipart/alternative) for
the legal-counsel update to Gaspar Sedeño in the existing thread
"Consulta de asesoramiento legal civil por reclamación de cantidades
(Barcelona)" — supersedes the prior unsent draft after the same-day SEPA
payment of the €477,95 vitrine saldo and the synchronized correction email
sent today to Federico in the "Devolución de adelantos – estanterías y
armario 2ª planta" thread.
"""
import json

PLAIN = """Buenos días, Gaspar,

Te escribo para actualizarte con la situación tras los movimientos de hoy. Te resumo en una sola página lo material y dejo el detalle documental al final por si quieres revisarlo o necesitas algo concreto para preparar el burofax.

## 1) Pago unilateral hoy del saldo Factura 01/2023 — vitrina P_08/2022 (477,95 €)

Hoy, 11 de mayo de 2026 a las 07:34, he ejecutado por Banca Digital de Ibercaja una transferencia SEPA por 477,95 € desde mi cuenta `**2862` (ES47 2085 9305 ******** 2862) a la cuenta de Caixa de Arquitectos `ES90 3183 0801 2410 0268 1920` (titular Ricard Solà Badell, BIC CASDESBBXX), concepto «FACTURA 01/2023 saldo vitrina P08/2022». Ibercaja confirma «Verificación de titularidad: Coincidencia exacta». Adjunto el justificante.

Esa era la única deuda formalmente facturada por Federico hacia mí que quedaba viva (la vitrina P_08/2022 fue entregada a principios de 2023, él la facturó el 14/02/2023 como Factura 01/2023, y el escaneo bancario 2023–2025 de las cinco cuentas confirma que nunca se llegó a pagar). Lo he pagado de forma unilateral por dos motivos estratégicos:

(a) Le quito la única línea «en paz» sustantiva que tenía documentada e impagada en su columna. Cualquier defensa basada en *"hay deudas mutuas que se compensan"* deja de tener base material a partir de hoy.

(b) Demuestro de forma documental, antes del burofax, que cumplo íntegramente con todas las obligaciones que tengo formalmente facturadas por su parte. Eso refuerza el monitorio ulterior, en el que él no podrá invocar reclamaciones cruzadas legítimas.

## 2) Cifra correcta del reclamo: 2.011,39 € (IVA incluido)

Tras la reconciliación forense que terminé anoche (cinco cuentas Ibercaja 2021–2025, transferencia Wise de noviembre 2022, todas las facturas FR_01 a FR_05BIS de 2022, presupuestos P_08/2022 y P_18/2024, y el chat completo de WhatsApp con Federico) y el pago del saldo de la vitrina ejecutado hoy, la cifra correcta del reclamo principal queda así. Sustituye íntegramente las cifras de mi correo del 15 de abril, que mezclaban magnitudes con/sin IVA y atribuían erróneamente al adelanto a Federico el coste del carpintero sustituto:

| Dirección | Concepto | Importe (con IVA) |
|---|---|---|
| Federico me debe | Reembolso adelanto armario habitación de invitados (cancelado por nosotros) | +1.306,00 € |
| Federico me debe | Reembolso adelanto tres estanterías de nogal P_18/2024 (no entregadas) | +1.189,39 € |
| Yo le debo | Aislamiento puerta del despacho (instalado, jamás presupuestado ni facturado) | −484,00 € |
| Pagado hoy 11/05/2026 | Saldo Factura 01/2023 vitrina P_08/2022 — pagado por SEPA desde la 2862 (justificante adjunto). Línea ya cerrada y por tanto no se netea. | (477,95 € — n/a) |
| **PRINCIPAL NETO RECLAMADO** | | **2.011,39 €** |

Mantengo el descuento honesto de los 484 € por el aislamiento de la puerta (instalado en agosto de 2023, sin presupuesto ni factura). Es la última línea que él podría invocar como «trabajo sin cotizar», y está respaldada por sus propias admisiones por WhatsApp del 18–19 de junio de 2024 (*"no se cobró justamente pensando en que habia dineros a favor nuestro … una factura abono con números negativos que van a descontar el adelanto que hiciste"*). Crediteársela neutraliza por completo cualquier defensa de tipo *"estamos en paz"*.

Si en algún momento del procedimiento prefieres reclamar la cifra **bruta** (2.495,39 €, sin descontar siquiera el aislamiento de la puerta, apoyándonos en el RD 1619/2012 que requiere factura para que un trabajo genere deuda exigible), me lo dices y reformulamos. La diferencia entre las dos posturas es de 484 €.

## 3) Correo correctivo a Federico ya enviado hoy en el hilo «Devolución de adelantos – estanterías y armario 2ª planta»

Acabo de enviar a Federico, en el mismo hilo donde el 23 de febrero le formulé la primera reclamación, un correo que:

- Sustituye íntegramente las cifras erróneas de mi correo del 23/02/2026.
- Establece la cifra **neta de 2.011,39 €** como reclamo formal con desglose de las cuatro líneas.
- Confirma el pago de hoy del saldo de la vitrina como acto unilateral de buena fe (con detalle de IBAN, hora, cuenta de origen y concepto).
- Mantiene el crédito de 484 € por el aislamiento de la puerta.
- Fija plazo expreso de respuesta hasta el **viernes 22 de mayo de 2026** para que confirme intención de pago, con calendario.
- Anuncia que, en ausencia de confirmación, mi abogado procederá con burofax a finales de la semana siguiente (**29 de mayo de 2026**).

Te lo reenvío momentáneamente por separado para tu archivo, con la versión completa del texto y el justificante de la transferencia de hoy.

## 4) Pieza clave: la reconciliación aceptada por Federico (14–16 agosto 2024)

El 14 de agosto de 2024 le envié a Federico por WhatsApp una tabla con los proyectos y los importes en juego. Su respuesta el 16 de agosto a las 14:07 fue, textualmente:

> "me parece corecto lo que tienes calculado, falta todavia que te instale los perfiles para los leds en el fresado, no lo hicimos porqué este mes es muy dificil trabajar con los proveedores."

Esto es el reconocimiento explícito por su parte de los cuatro importes en juego (armario invitados, vitrina/back-desk, P_18/2024 nogal, aislamiento puerta). Sigo adjuntando la fotografía de esa tabla.

## 5) Sobre la respuesta de Federico del 5 de febrero de 2026 («estamos en paz»)

El 19 de junio de 2025 le pedí amistosamente «hacer cuentas para quedar en paz». El 5 de febrero de 2026 me respondió por WhatsApp:

> "consideramos que entre idas y venidas de vuestra casa, arreglando cosas y haciendo trabajos que no se han cotizado nunca, podemos considerar que estemos en paz."

Las dos únicas líneas identificables a su favor en tres años de chat y correo son las dos que aparecen en mi columna: el aislamiento de la puerta (484 €, ya descontado) y el saldo de la vitrina (477,95 €, **pagado hoy**). Las «idas y venidas» que menciona corresponden a visitas estándar de instalación e inspección, en cada caso atadas a un presupuesto activo que yo ya había prepagado, y por tanto no son facturables como concepto separado. Tras los movimientos de hoy, no le queda en la mesa ningún importe documentado a su favor que justifique una posición de «en paz» mayor que los 484 €. La diferencia restante de 2.011,39 € queda sin justificación alguna.

## 6) Cómo se pagaron los adelantos (todo bancariamente trazable)

1. **1.306 € (closet) y 716,93 € (60% prepayment de la vitrina)** — fueron parte del **adelanto inicial maestro** de 24.577,24 € que pagué a Federico el **6 de febrero de 2022** desde mi cuenta Ibercaja terminada en `**6912` (concepto "FACTURA 01/2022"). Esa factura FR_01/2022 cubría el 60% inicial de **todos** los proyectos del plan original. Adjunto el comprobante bancario; tengo también la factura FR_01/2022 íntegra con el desglose por mueble si la necesitas.

2. **1.189,39 € (estanterías nogal P_18/2024)** — pagado por transferencia el **19 de junio de 2024** desde mi cuenta Ibercaja `**2862`. Adjunto el comprobante.

3. **477,95 € saldo de la vitrina (Factura 01/2023)** — emitida por Federico el 14/02/2023, sin pago hasta hoy. **Pagado hoy 11/05/2026 a las 07:34** desde la `**2862` por SEPA, justificante adjunto.

4. La sustitución posterior del trabajo no entregado de las estanterías de nogal por un carpintero alternativo (Estudi BCR Ebanisteria i Diseny SL, NIF B66776033) costó en total **5.065,06 €**, documentado en la **Factura 2025-00061** que vuelvo a adjuntar. Es prueba directa de que tuve que rehacer el trabajo a mi costa.

## 7) Plan de acción propuesto

1. **Si Federico confirma pago antes del 22 de mayo**: cierras el expediente sin coste litigation.
2. **Si NO contesta o se mantiene en «en paz»**: te pido que preparéis el **burofax** con la cifra neta **2.011,39 €** y este desglose, para enviarlo a finales de la semana del 25–29 de mayo. Tras el plazo legal del burofax, procedimiento monitorio ante el Juzgado de Primera Instancia de Barcelona (importe inferior a 15.000 €, cauce rápido).

## Adjuntos en este correo (5)

1. **2024-08-14_Mark_reconciliation_accepted_by_Federico.jpg** — fotografía de mi tabla de reconciliación de agosto de 2024.
2. **2022-02-06_TransferenciaDestinatario_24577.pdf** — comprobante bancario del adelanto maestro FR_01/2022 (24.577,24 €).
3. **2024-06-19_TransferenciaDestinatario_1189_for_P18_2024.pdf** — comprobante bancario del adelanto P_18/2024 (1.189,39 €).
4. **2026-05-11_TransferenciaDestinatario_477_95_vitrina_saldo.pdf** — *NUEVO*: comprobante de la transferencia SEPA de hoy del saldo de la vitrina (477,95 €).
5. **SUBSTITUTE_FAC_2025_00061_EstudiBCR.pdf** — factura del carpintero sustituto (5.065,06 €).

Disponibles bajo petición: la Factura 01/2023 PDF original que ahora ya no es deuda viva (sustituida por su justificante de pago de hoy), la factura FR_01/2022 con desglose por mueble, el chat completo con Federico (texto plano), las facturas FR_02 a FR_05BIS de 2022 (todas pagadas y con sus comprobantes), los presupuestos individuales de los muebles no entregados, los WhatsApps de junio de 2024 con admisiones de Federico, y el escaneo bancario completo 2021–2025.

Quedo atento a tu indicación y, en cualquier caso, te iré informando puntualmente si Federico responde antes del 22 de mayo.

Un saludo,
Mark"""

HTML = """<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#222;max-width:760px;\">
<p>Buenos días, Gaspar,</p>
<p>Te escribo para actualizarte con la situación tras los movimientos de hoy. Te resumo en una sola página lo material y dejo el detalle documental al final por si quieres revisarlo o necesitas algo concreto para preparar el burofax.</p>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">1) Pago unilateral hoy del saldo Factura 01/2023 — vitrina P_08/2022 (477,95&nbsp;€)</h2>
<div style=\"margin:0.5em 0;padding:10px 14px;border-left:3px solid #2e7d32;background:#f1f8f3;\">
<p style=\"margin:0;\">Hoy, <strong>11 de mayo de 2026 a las 07:34</strong>, he ejecutado por Banca Digital de Ibercaja una transferencia SEPA por <strong>477,95&nbsp;€</strong> desde mi cuenta <strong>`**2862`</strong> (ES47 2085 9305 ******** 2862) a la cuenta de Caixa de Arquitectos <strong>ES90 3183 0801 2410 0268 1920</strong> (titular Ricard Solà Badell, BIC CASDESBBXX), concepto «<em>FACTURA 01/2023 saldo vitrina P08/2022</em>». Ibercaja confirma «Verificación de titularidad: Coincidencia exacta». Adjunto el justificante.</p>
</div>
<p>Esa era <strong>la única deuda formalmente facturada por Federico hacia mí que quedaba viva</strong> (la vitrina P_08/2022 fue entregada a principios de 2023, él la facturó el 14/02/2023 como Factura 01/2023, y el escaneo bancario 2023–2025 de las cinco cuentas confirma que nunca se llegó a pagar). Lo he pagado de forma unilateral por dos motivos estratégicos:</p>
<ol style=\"margin-left:0;padding-left:1.2em;\">
<li style=\"margin-bottom:0.5em;\"><strong>Le quito la única línea «en paz» sustantiva</strong> que tenía documentada e impagada en su columna. Cualquier defensa basada en <em>«hay deudas mutuas que se compensan»</em> deja de tener base material a partir de hoy.</li>
<li style=\"margin-bottom:0.5em;\"><strong>Demuestro documentalmente, antes del burofax, que cumplo íntegramente</strong> con todas las obligaciones que tengo formalmente facturadas por su parte. Eso refuerza el monitorio ulterior, en el que él no podrá invocar reclamaciones cruzadas legítimas.</li>
</ol>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">2) Cifra correcta del reclamo: 2.011,39&nbsp;€ (IVA incluido)</h2>
<p>Tras la reconciliación forense que terminé anoche (cinco cuentas Ibercaja 2021–2025, transferencia Wise de noviembre 2022, todas las facturas FR_01 a FR_05BIS de 2022, presupuestos P_08/2022 y P_18/2024, y el chat completo de WhatsApp con Federico) y el pago del saldo de la vitrina ejecutado hoy, la cifra correcta del reclamo principal queda así. Sustituye íntegramente las cifras de mi correo del 15 de abril, que mezclaban magnitudes con/sin IVA y atribuían erróneamente al adelanto a Federico el coste del carpintero sustituto:</p>
<table cellpadding=\"8\" cellspacing=\"0\" style=\"border-collapse:collapse;width:100%;max-width:720px;font-size:13px;border:1px solid #bbb;\">
<thead><tr style=\"background:#f3f3f3;\">
<th align=\"left\" style=\"border:1px solid #bbb;\">Dirección</th>
<th align=\"left\" style=\"border:1px solid #bbb;\">Concepto</th>
<th align=\"right\" style=\"border:1px solid #bbb;white-space:nowrap;\">Importe (con IVA)</th>
</tr></thead>
<tbody>
<tr><td style=\"border:1px solid #bbb;vertical-align:top;\">Federico me debe</td><td style=\"border:1px solid #bbb;\">Reembolso adelanto armario habitación de invitados (cancelado por nosotros)</td><td align=\"right\" style=\"border:1px solid #bbb;\">+1.306,00&nbsp;€</td></tr>
<tr><td style=\"border:1px solid #bbb;vertical-align:top;\">Federico me debe</td><td style=\"border:1px solid #bbb;\">Reembolso adelanto <strong>tres</strong> estanterías de nogal P_18/2024 (no entregadas)</td><td align=\"right\" style=\"border:1px solid #bbb;\">+1.189,39&nbsp;€</td></tr>
<tr><td style=\"border:1px solid #bbb;vertical-align:top;\">Yo le debo</td><td style=\"border:1px solid #bbb;\">Aislamiento puerta del despacho (instalado, jamás presupuestado ni facturado)</td><td align=\"right\" style=\"border:1px solid #bbb;\">−484,00&nbsp;€</td></tr>
<tr><td style=\"border:1px solid #bbb;vertical-align:top;color:#666;font-style:italic;\">Pagado hoy 11/05/2026</td><td style=\"border:1px solid #bbb;color:#666;font-style:italic;\">Saldo Factura 01/2023 vitrina P_08/2022 — pagado por SEPA desde la 2862 (justificante adjunto). Línea ya cerrada y por tanto no se netea.</td><td align=\"right\" style=\"border:1px solid #bbb;color:#666;font-style:italic;\">(477,95&nbsp;€ — n/a)</td></tr>
<tr style=\"background:#fafafa;font-weight:bold;\"><td style=\"border:1px solid #bbb;\" colspan=\"2\">PRINCIPAL NETO RECLAMADO</td><td align=\"right\" style=\"border:1px solid #bbb;\">2.011,39&nbsp;€</td></tr>
</tbody></table>
<p>Mantengo el descuento honesto de los <strong>484&nbsp;€</strong> por el aislamiento de la puerta (instalado en agosto de 2023, sin presupuesto ni factura). Es la última línea que él podría invocar como «trabajo sin cotizar», y está respaldada por sus propias admisiones por WhatsApp del 18–19 de junio de 2024 (<em>«no se cobró justamente pensando en que habia dineros a favor nuestro … una factura abono con números negativos que van a descontar el adelanto que hiciste»</em>). Crediteársela neutraliza por completo cualquier defensa de tipo «estamos en paz».</p>
<p>Si en algún momento del procedimiento prefieres reclamar la cifra <strong>bruta</strong> (<strong>2.495,39&nbsp;€</strong>, sin descontar siquiera el aislamiento de la puerta, apoyándonos en el RD 1619/2012 que requiere factura para que un trabajo genere deuda exigible), me lo dices y reformulamos. La diferencia entre las dos posturas es de 484&nbsp;€.</p>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">3) Correo correctivo a Federico ya enviado hoy en el hilo «Devolución de adelantos – estanterías y armario 2ª planta»</h2>
<p>Acabo de enviar a Federico, en el mismo hilo donde el 23 de febrero le formulé la primera reclamación, un correo que:</p>
<ul style=\"margin-left:0;padding-left:1.2em;\">
<li style=\"margin-bottom:0.4em;\">Sustituye íntegramente las cifras erróneas de mi correo del 23/02/2026.</li>
<li style=\"margin-bottom:0.4em;\">Establece la cifra <strong>neta de 2.011,39&nbsp;€</strong> como reclamo formal con desglose de las cuatro líneas.</li>
<li style=\"margin-bottom:0.4em;\">Confirma el pago de hoy del saldo de la vitrina como acto unilateral de buena fe (con detalle de IBAN, hora, cuenta de origen y concepto).</li>
<li style=\"margin-bottom:0.4em;\">Mantiene el crédito de 484&nbsp;€ por el aislamiento de la puerta.</li>
<li style=\"margin-bottom:0.4em;\">Fija plazo expreso de respuesta hasta el <strong>viernes 22 de mayo de 2026</strong> para que confirme intención de pago, con calendario.</li>
<li style=\"margin-bottom:0.4em;\">Anuncia que, en ausencia de confirmación, mi abogado procederá con <strong>burofax a finales de la semana siguiente (29 de mayo de 2026)</strong>.</li>
</ul>
<p>Te lo reenvío momentáneamente por separado para tu archivo, con la versión completa del texto y el justificante de la transferencia de hoy.</p>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">4) Pieza clave: la reconciliación aceptada por Federico (14–16 agosto 2024)</h2>
<p>El 14 de agosto de 2024 le envié a Federico por WhatsApp una tabla con los proyectos y los importes en juego. Su respuesta el 16 de agosto a las 14:07 fue, textualmente:</p>
<blockquote style=\"margin:0.5em 0 1em 1em;padding:6px 12px;border-left:3px solid #888;color:#444;font-style:italic;\">«me parece corecto lo que tienes calculado, falta todavia que te instale los perfiles para los leds en el fresado, no lo hicimos porqué este mes es muy dificil trabajar con los proveedores.»</blockquote>
<p>Esto es el reconocimiento explícito por su parte de los cuatro importes en juego (armario invitados, vitrina/back-desk, P_18/2024 nogal, aislamiento puerta). Sigo adjuntando la fotografía de esa tabla.</p>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">5) Sobre la respuesta de Federico del 5 de febrero de 2026 («estamos en paz»)</h2>
<p>El 19 de junio de 2025 le pedí amistosamente «hacer cuentas para quedar en paz». El 5 de febrero de 2026 me respondió por WhatsApp:</p>
<blockquote style=\"margin:0.5em 0 1em 1em;padding:6px 12px;border-left:3px solid #888;color:#444;font-style:italic;\">«consideramos que entre idas y venidas de vuestra casa, arreglando cosas y haciendo trabajos que no se han cotizado nunca, podemos considerar que estemos en paz.»</blockquote>
<p>Las dos únicas líneas identificables a su favor en tres años de chat y correo son las dos que aparecen en mi columna: el aislamiento de la puerta (484&nbsp;€, ya descontado) y el saldo de la vitrina (477,95&nbsp;€, <strong>pagado hoy</strong>). Las «idas y venidas» que menciona corresponden a visitas estándar de instalación e inspección, en cada caso atadas a un presupuesto activo que yo ya había prepagado, y por tanto no son facturables como concepto separado. Tras los movimientos de hoy, no le queda en la mesa ningún importe documentado a su favor que justifique una posición de «en paz» mayor que los 484&nbsp;€. La diferencia restante de <strong>2.011,39&nbsp;€</strong> queda sin justificación alguna.</p>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">6) Cómo se pagaron los adelantos (todo bancariamente trazable)</h2>
<ol style=\"margin-left:0;padding-left:1.2em;\">
<li style=\"margin-bottom:0.6em;\"><strong>1.306&nbsp;€ (closet) y 716,93&nbsp;€ (60% prepayment de la vitrina)</strong> — fueron parte del <strong>adelanto inicial maestro</strong> de 24.577,24&nbsp;€ que pagué a Federico el <strong>6 de febrero de 2022</strong> desde mi cuenta Ibercaja terminada en <strong>`**6912`</strong> (concepto «FACTURA 01/2022»). Esa factura FR_01/2022 cubría el 60% inicial de <strong>todos</strong> los proyectos del plan original. Adjunto el comprobante bancario.</li>
<li style=\"margin-bottom:0.6em;\"><strong>1.189,39&nbsp;€ (estanterías nogal P_18/2024)</strong> — pagado por transferencia el <strong>19 de junio de 2024</strong> desde mi cuenta Ibercaja <strong>`**2862`</strong>. Adjunto el comprobante.</li>
<li style=\"margin-bottom:0.6em;\"><strong>477,95&nbsp;€ saldo de la vitrina (Factura 01/2023)</strong> — emitida por Federico el 14/02/2023, sin pago hasta hoy. <strong>Pagado hoy 11/05/2026 a las 07:34</strong> desde la <strong>`**2862`</strong> por SEPA, justificante adjunto.</li>
<li style=\"margin-bottom:0.6em;\">La sustitución posterior del trabajo no entregado de las estanterías de nogal por un carpintero alternativo (<strong>Estudi BCR Ebanisteria i Diseny SL</strong>, NIF B66776033) costó en total <strong>5.065,06&nbsp;€</strong>, documentado en la <strong>Factura 2025-00061</strong> que vuelvo a adjuntar. Es prueba directa de que tuve que rehacer el trabajo a mi costa.</li>
</ol>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">7) Plan de acción propuesto</h2>
<ol style=\"margin-left:0;padding-left:1.2em;\">
<li style=\"margin-bottom:0.5em;\"><strong>Si Federico confirma pago antes del 22 de mayo</strong>: cierras el expediente sin coste de litigio.</li>
<li style=\"margin-bottom:0.5em;\"><strong>Si NO contesta o se mantiene en «en paz»</strong>: te pido que preparéis el <strong>burofax</strong> con la cifra neta <strong>2.011,39&nbsp;€</strong> y este desglose, para enviarlo a finales de la semana del 25–29 de mayo. Tras el plazo legal del burofax, procedimiento monitorio ante el Juzgado de Primera Instancia de Barcelona (importe inferior a 15.000&nbsp;€, cauce rápido).</li>
</ol>
<h2 style=\"font-size:16px;font-weight:bold;margin:1.4em 0 0.6em 0;border-bottom:1px solid #ccc;padding-bottom:4px;\">Adjuntos en este correo (5)</h2>
<ol style=\"margin-left:0;padding-left:1.2em;\">
<li><strong>2024-08-14_Mark_reconciliation_accepted_by_Federico.jpg</strong> — fotografía de mi tabla de reconciliación de agosto de 2024.</li>
<li><strong>2022-02-06_TransferenciaDestinatario_24577.pdf</strong> — comprobante bancario del adelanto maestro FR_01/2022 (24.577,24&nbsp;€).</li>
<li><strong>2024-06-19_TransferenciaDestinatario_1189_for_P18_2024.pdf</strong> — comprobante bancario del adelanto P_18/2024 (1.189,39&nbsp;€).</li>
<li><strong>2026-05-11_TransferenciaDestinatario_477_95_vitrina_saldo.pdf</strong> — <em>NUEVO</em>: comprobante de la transferencia SEPA de hoy del saldo de la vitrina (477,95&nbsp;€).</li>
<li><strong>SUBSTITUTE_FAC_2025_00061_EstudiBCR.pdf</strong> — factura del carpintero sustituto (5.065,06&nbsp;€).</li>
</ol>
<p>Disponibles bajo petición: la Factura 01/2023 PDF original que ahora ya no es deuda viva (sustituida por su justificante de pago de hoy), la factura FR_01/2022 con desglose por mueble, el chat completo con Federico (texto plano), las facturas FR_02 a FR_05BIS de 2022 (todas pagadas y con sus comprobantes), los presupuestos individuales de los muebles no entregados, los WhatsApps de junio de 2024 con admisiones de Federico, y el escaneo bancario completo 2021–2025.</p>
<p>Quedo atento a tu indicación y, en cualquier caso, te iré informando puntualmente si Federico responde antes del 22 de mayo.</p>
<p>Un saludo,<br/>Mark</p>
</div>"""

payload = {
    "to": ["gsedeno@example.com"],
    "subject": "RE: Consulta de asesoramiento legal civil por reclamación de cantidades (Barcelona)",
    "threadId": "19d000e46959fce8",
    "inReplyTo": "19e1568f232f8672",
    "mimeType": "multipart/alternative",
    "body": PLAIN,
    "htmlBody": HTML,
    "attachments": [
        "/Users/markmhendrickson/repos/ateles/.cursor/federico_dispute_evidence_2026/2024-08-14_Mark_reconciliation_accepted_by_Federico.jpg",
        "/Users/markmhendrickson/repos/ateles/.cursor/federico_dispute_evidence_2026/2022-02-06_TransferenciaDestinatario_24577.pdf",
        "/Users/markmhendrickson/repos/ateles/.cursor/federico_dispute_evidence_2026/2024-06-19_TransferenciaDestinatario_1189_for_P18_2024.pdf",
        "/Users/markmhendrickson/repos/ateles/.cursor/federico_dispute_evidence_2026/2026-05-11_TransferenciaDestinatario_477_95_vitrina_saldo.pdf",
        "/Users/markmhendrickson/repos/ateles/.cursor/federico_dispute_evidence_2026/SUBSTITUTE_FAC_2025_00061_EstudiBCR.pdf",
    ],
}
print(json.dumps(payload, ensure_ascii=False))
