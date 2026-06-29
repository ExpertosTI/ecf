"""Tests de helpers compartidos."""

from ecf_core.utils import normalize_odoo_webhook_url, normalize_rnc_digits


class TestNormalizeRncDigits:
    def test_con_guiones(self):
        assert normalize_rnc_digits("132-84231-6") == "132842316"

    def test_sin_formato(self):
        assert normalize_rnc_digits("132842316") == "132842316"


class TestNormalizeOdooWebhookUrl:
    def test_dominio_solo(self):
        assert normalize_odoo_webhook_url("https://app.renace.tech") == (
            "https://app.renace.tech/ecf/webhook/callback"
        )

    def test_path_completo(self):
        url = "https://app.renace.tech/ecf/webhook/callback"
        assert normalize_odoo_webhook_url(url) == url
