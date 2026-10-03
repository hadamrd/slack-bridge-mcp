import json
from pathlib import Path

from slack_bridge_mcp.rich_text import message_text, parse_alert

CARD = json.loads((Path(__file__).parent / "fixtures" / "jsm_alert_card.json").read_text())


def test_card_body_is_not_empty():
    assert "Alert #308370" in message_text(CARD)


def test_card_fields():
    a = parse_alert(CARD)
    assert a["kind"] == "card"
    assert a["id"] == 308370
    # full name from the title, not the 40-char tag
    assert a["alertname"] == "gerrit-multisite-branches-misaligned-between-nodes"
    assert a["status"] == "Acknowledged"
    assert (a["env"], a["priority"], a["app_owner"]) == ("prod", "P3", "codex")
    assert a["runbook"].endswith("Runbook+Gerrit+multi-site+split+brain")
    assert a["grafana_rule"] == "https://grafana.prod.crto.in/alerting/grafana/X-nmz48B5/view"


def test_follow_up_event():
    m = {
        "text": "",
        "attachments": [{
            "fallback": "<https://j.opsg.in/a/criteo/x|Alert #1: foo>\n<@U1> acknowledged the alert",
            "text": "<@U1> acknowledged the alert",
            "pretext": "<https://j.opsg.in/a/criteo/x|Alert #1: foo>",
        }],
    }
    a = parse_alert(m)
    assert a["kind"] == "event" and a["alertname"] == "foo"
    assert a["event"] == "<@U1> acknowledged the alert"


def test_plain_message_is_not_an_alert():
    assert parse_alert({"text": "hello"}) is None
    assert message_text({"text": "hello"}) == "hello"


def _am(text):
    from slack_bridge_mcp.rich_text import parse_alertmanager
    return parse_alertmanager({"text": text})


def test_am_grafana_title_keeps_colons():
    a = _am("[RESOLVED] Concourse: SQL server DB pipelines: Failing builds in preproduction\n*Alerts Resolved:*\n"
            "• More than 30% failed (doc (https://criteo.atlassian.net/wiki/x), "
            "src (https://grafana.prod.crto.in/alerting/grafana/AbC-12/view))")
    assert a["alertname"] == "Concourse: SQL server DB pipelines: Failing builds in preproduction"
    assert a["rule_uid"] == "AbC-12" and a["runbook"] == "https://criteo.atlassian.net/wiki/x"


def test_am_grafana_title_keeps_dashes():
    a = _am("[RESOLVED] PaaS API - Low number of requests answered in fr3\n*Alerts Resolved:*\n"
            "• x (src (https://grafana.prod.crto.in/alerting/grafana/u1/view))")
    assert a["alertname"] == "PaaS API - Low number of requests answered in fr3"


def test_am_prometheus_name_and_summary():
    a = _am("[FIRING:1] SloBroken: SLO of coder.coder_session broken on bigdataflow-app running in  -\n"
            "*Alerts Firing:*\n• broke its SLO (doc (https://criteo.atlassian.net/wiki/spaces/BDF/x),")
    assert a["alertname"] == "SloBroken" and a["summary"].startswith("SLO of coder.coder_session")
    assert a["state"] == "firing" and a["count"] == 1 and "rule_uid" not in a
