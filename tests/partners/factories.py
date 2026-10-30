import copy

from automations.partners.config import CATALOG_PATH, load_config

MODELS = CATALOG_PATH
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
