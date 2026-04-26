# -*- coding: utf-8 -*-
import requests
import logging
from odoo import fields, models, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    partner_latitude = fields.Float(string='Latitud', digits=(16, 5))
    partner_longitude = fields.Float(string='Longitud', digits=(16, 5))
    nav_google_url = fields.Char(string='Google Maps (Envío)')
    nav_waze_url = fields.Char(string='Waze (Envío)')

    def action_geolocalize(self):
        """Consume OpenStreetMap Nominatim API (Free) to get coordinates."""
        for partner in self:
            if not partner.street or not partner.city:
                continue
            
            address_str = f"{partner.street}, {partner.city}, {partner.state_id.name or ''}, {partner.country_id.name or ''}"
            url = "https://nominatim.openstreetmap.org/search"
            params = {
                'q': address_str,
                'format': 'json',
                'limit': 1
            }
            # Odoo Best Practice: Use a descriptive User-Agent
            headers = {'User-Agent': 'Renace-POS-Shipment/1.0'}
            
            try:
                response = requests.get(url, params=params, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        partner.write({
                            'partner_latitude': float(data[0]['lat']),
                            'partner_longitude': float(data[0]['lon']),
                        })
            except Exception as e:
                _logger.error("OSM Geocoding Error: %s", str(e))
                continue
                
        return True
