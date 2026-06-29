"""Tests del worker de cola e-CF."""

import json

from ecf_core.queue_worker import _normalizar_items_ecf


class TestNormalizarItemsEcf:
    def test_lista_de_dicts(self):
        raw = [{"linea": 1, "descripcion": "Item A"}]
        assert _normalizar_items_ecf(raw) == raw

    def test_lista_de_strings_json(self):
        raw = [json.dumps({"linea": 1, "descripcion": "Item A"})]
        assert _normalizar_items_ecf(raw) == [{"linea": 1, "descripcion": "Item A"}]

    def test_array_json_como_string(self):
        raw = json.dumps([{"linea": 2, "descripcion": "Item B"}])
        assert _normalizar_items_ecf(raw) == [{"linea": 2, "descripcion": "Item B"}]

    def test_ignora_nulls_de_left_join(self):
        assert _normalizar_items_ecf([None]) == []
        assert _normalizar_items_ecf(None) == []
