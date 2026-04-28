"""
pdf_service.py — Generación de Representación Impresa e-CF
Responsabilidades:
- Generar PDF con el formato legal de la DGII.
- Incluir CUFE, Código de Seguridad y QR Code.
- Diseño premium acorde a Renace Tech.
"""

import os
import qrcode
import base64
from io import BytesIO
from datetime import datetime
from lxml import etree
from jinja2 import Template

# Plantilla HTML para la Representación Impresa (Basada en Estándares DGII)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <style>
        body { font-family: 'Inter', sans-serif; color: #333; margin: 0; padding: 20px; font-size: 10px; }
        .header { display: flex; justify-content: space-between; border-bottom: 2px solid #10b981; padding-bottom: 10px; }
        .logo { font-size: 24px; font-weight: bold; color: #10b981; }
        .company-info { text-align: left; }
        .ecf-info { text-align: right; background: #f9fafb; padding: 10px; border-radius: 8px; border: 1px solid #e5e7eb; }
        .ecf-info h2 { margin: 0; color: #111; font-size: 16px; }
        .ecf-info p { margin: 2px 0; font-weight: bold; }
        
        .section { margin-top: 20px; }
        .section-title { font-weight: bold; text-transform: uppercase; color: #6b7280; border-bottom: 1px solid #e5e7eb; margin-bottom: 5px; }
        
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th { background: #f3f4f6; text-align: left; padding: 8px; border-bottom: 1px solid #e5e7eb; }
        td { padding: 8px; border-bottom: 1px solid #f3f4f6; }
        
        .totals { margin-top: 20px; display: flex; justify-content: flex-end; }
        .totals-table { width: 250px; }
        .totals-table td { text-align: right; border: none; }
        .totals-table .grand-total { font-size: 14px; font-weight: bold; color: #10b981; }
        
        .footer { margin-top: 40px; display: flex; border-top: 1px solid #e5e7eb; padding-top: 20px; }
        .qr-section { width: 120px; }
        .legal-section { flex-grow: 1; padding-left: 20px; font-size: 8px; color: #6b7280; }
        .cufe-box { background: #f3f4f6; padding: 5px; border-radius: 4px; font-family: monospace; font-size: 9px; margin-top: 5px; word-break: break-all; }
    </style>
</head>
<body>
    <div class="header">
        <div class="company-info">
            <div class="logo">RENACE TECH</div>
            <p><strong>{{ emisor_nombre }}</strong></p>
            <p>RNC: {{ emisor_rnc }}</p>
            <p>{{ emisor_direccion }}</p>
            <p>Tel: {{ emisor_telefono }}</p>
        </div>
        <div class="ecf-info">
            <h2>{{ tipo_nombre }}</h2>
            <p>NCF: {{ ncf }}</p>
            <p>FECHA: {{ fecha_emision }}</p>
            <p>VENCE: {{ fecha_vencimiento }}</p>
        </div>
    </div>

    <div class="section">
        <div class="section-title">Receptor</div>
        <p><strong>{{ receptor_nombre }}</strong></p>
        <p>RNC/Cédula: {{ receptor_rnc }}</p>
        {% if receptor_direccion %}<p>Dirección: {{ receptor_direccion }}</p>{% endif %}
    </div>

    <table>
        <thead>
            <tr>
                <th>Descripción</th>
                <th style="text-align: center;">Cant.</th>
                <th style="text-align: right;">Precio</th>
                <th style="text-align: right;">ITBIS</th>
                <th style="text-align: right;">Total</th>
            </tr>
        </thead>
        <tbody>
            {% for item in items %}
            <tr>
                <td>{{ item.descripcion }}</td>
                <td style="text-align: center;">{{ item.cantidad }}</td>
                <td style="text-align: right;">{{ item.precio_unitario }}</td>
                <td style="text-align: right;">{{ item.itbis_monto }}</td>
                <td style="text-align: right;">{{ item.total }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <div class="totals">
        <table class="totals-table">
            <tr>
                <td>Subtotal:</td>
                <td><strong>{{ subtotal }}</strong></td>
            </tr>
            <tr>
                <td>ITBIS:</td>
                <td><strong>{{ itbis }}</strong></td>
            </tr>
            <tr class="grand-total">
                <td>TOTAL:</td>
                <td><strong>{{ total }} {{ moneda }}</strong></td>
            </tr>
        </table>
    </div>

    <div class="footer">
        <div class="qr-section">
            <img src="data:image/png;base64,{{ qr_base64 }}" width="110" height="110">
        </div>
        <div class="legal-section">
            <p>ESTE DOCUMENTO ES UNA REPRESENTACIÓN IMPRESA DE UN COMPROBANTE FISCAL ELECTRÓNICO (e-CF)</p>
            <p><strong>CUFE:</strong></p>
            <div class="cufe-box">{{ cufe }}</div>
            <p style="margin-top: 10px;">Código de Seguridad: <strong>{{ security_code }}</strong></p>
            <p>Certificación DGII RD</p>
        </div>
    </div>
</body>
</html>
"""

class ECFPDFService:
    def __init__(self):
        self.template = Template(HTML_TEMPLATE)

    def generar_qr(self, url: str) -> str:
        """Genera un código QR en base64."""
        qr = qrcode.QRCode(version=1, box_size=10, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode()

    def generar_pdf_html(self, data: dict) -> str:
        """Genera el HTML final con los datos inyectados."""
        # Generar QR para validación DGII
        # URL formato DGII: https://dgii.gov.do/verify?rnc=...&ncf=...&cufe=...
        qr_url = f"https://dgii.gov.do/verificaeCF?RncEmisor={data['emisor_rnc']}&ncf={data['ncf']}&FechaEmision={data['fecha_emision']}&MontoTotal={data['total']}&CUFE={data['cufe']}"
        data['qr_base64'] = self.generar_qr(qr_url)
        
        return self.template.render(**data)

    async def exportar_a_pdf(self, data: dict) -> bytes:
        """
        Exporta el HTML a PDF. 
        Nota: En un entorno real usaríamos weasyprint o similar.
        Por ahora retornamos el HTML para visualización o usamos una herramienta mock.
        """
        html_content = self.generar_pdf_html(data)
        # Mock de PDF (en un sistema real se llamaría a una librería de PDF)
        return html_content.encode('utf-8')
