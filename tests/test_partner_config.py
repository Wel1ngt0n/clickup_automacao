import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from partner_config import build_snapshot, existing_client, load_config

MODELS = (
    Path(__file__).resolve().parents[1] / "templates" / "parceiros" / "modelos.json"
)
CLIENT_FIELD = "c30d5569-ea0b-4799-b904-80815d513bfb"
YES_ID = "0144f848-2585-46d1-a966-daceaf49108d"
NO_ID = "f93c28ca-f8e1-4814-9d48-6f1d1092ae0d"


def parent_task(value=1):
    return {
        "id": "partner-parent",
        "name": "[IMPLANTAÇÃO] Loja teste",
        "url": "https://app.clickup.com/t/partner-parent",
        "list": {"id": "901327379509"},
        "date_created": "1900000000000",
        "status": {"status": "onboarding", "type": "open"},
        "dependencies": [],
        "custom_fields": [
            {"id": CLIENT_FIELD, "value": value},
            {"id": "2e3506bb-f020-47f2-9950-06140dc09e6a", "value": "Loja teste"},
        ],
    }


def ready_config():
    config = copy.deepcopy(load_config(MODELS))
    config.update({"enabled": True, "review_status": "approved"})
    for stage in config["stages"]:
        stage["review_status"] = "approved"
        if stage["key"] == "configuracao_financeira":
            stage["list_id"] = stage["list_id"] or "finance-test"
    return config


class ExistingClientTests(unittest.TestCase):
    def test_yes_supports_zero_uuid_and_connector_object(self):
        for value in (
            0,
            "0",
            YES_ID,
            {"id": YES_ID, "name": "Sim", "orderindex": 0},
            {"orderindex": 0},
            {"name": "Sim"},
        ):
            with self.subTest(value=value):
                self.assertIs(existing_client(parent_task(value), CLIENT_FIELD), True)

    def test_no_supports_one_uuid_and_connector_object(self):
        for value in (
            1,
            "1",
            NO_ID,
            {"id": NO_ID, "name": "Não", "orderindex": 1},
            {"orderindex": 1},
            {"name": "Não"},
        ):
            with self.subTest(value=value):
                self.assertIs(existing_client(parent_task(value), CLIENT_FIELD), False)

    def test_missing_or_invalid_answer_never_becomes_new_client(self):
        for value in (
            None,
            "",
            True,
            False,
            0.5,
            1.5,
            "unknown",
            2,
            [],
            {"id": "unknown"},
            {"orderindex": 0.5},
        ):
            with self.subTest(value=value):
                self.assertIsNone(existing_client(parent_task(value), CLIENT_FIELD))
        self.assertIsNone(existing_client({"custom_fields": []}, CLIENT_FIELD))


class ModelConfigurationTests(unittest.TestCase):
    def test_catalog_has_nine_ordered_stages_with_finance_before_post(self):
        config = load_config(MODELS)
        self.assertEqual(
            [stage["order"] for stage in config["stages"]], list(range(1, 10))
        )
        self.assertEqual(
            [stage["key"] for stage in config["stages"]][-3:],
            ["treinamento", "configuracao_financeira", "pos_implantacao"],
        )

    def test_existing_client_snapshot_skips_only_products(self):
        config = ready_config()
        snapshot = build_snapshot(config, parent_task(0), True)
        self.assertEqual(len(snapshot["stages"]), 8)
        self.assertNotIn(
            "cadastro_produtos", [stage["key"] for stage in snapshot["stages"]]
        )

    def test_new_client_snapshot_renders_descriptions_and_checklists(self):
        snapshot = build_snapshot(ready_config(), parent_task(1), False)
        self.assertEqual(len(snapshot["stages"]), 9)
        for stage in snapshot["stages"]:
            with self.subTest(stage=stage["key"]):
                payload = stage["payload"]
                self.assertIn("Loja teste", payload["name"])
                self.assertNotIn("{{", payload["markdown_content"])
                self.assertIn("partner-parent", payload["markdown_content"])
                self.assertGreater(len(stage["checklists"][0]["items"]), 0)
                self.assertIn(
                    {
                        "id": "17f49983-94ed-47a2-9b94-8c83a0d99d79",
                        "value": "partner-parent",
                    },
                    payload["custom_fields"],
                )

    def write_catalog(self, update):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name) / "models"
        shutil.copytree(MODELS.parent, folder)
        path = folder / "modelos.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        update(data)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_activation_rejects_missing_financial_list(self):
        def update(data):
            data.update({"enabled": True, "review_status": "approved"})
            for stage in data["stages"]:
                stage["review_status"] = "approved"
            data["stages"][7]["list_id"] = None

        with self.assertRaises((ValueError, RuntimeError)):
            load_config(self.write_catalog(update), require_ready=True)

    def test_duplicate_stage_destination_is_rejected(self):
        path = self.write_catalog(
            lambda data: data["stages"][1].update(
                {"list_id": data["stages"][0]["list_id"]}
            )
        )
        with self.assertRaises((ValueError, RuntimeError)):
            load_config(path)

    def test_description_cannot_escape_template_directory(self):
        path = self.write_catalog(
            lambda data: data["stages"][0].update(
                {"description_file": "../../outside.md"}
            )
        )
        with self.assertRaises((ValueError, RuntimeError)):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
