"""Telegram Markdown escaping (B2): a dynamic value must not break the channel

Telegram's legacy Markdown rejects a message whose dynamic content contains
`_`, `*`, `` ` `` or `[` with 400 "can't parse entities" - i.e. the channel goes
silent for that validator. Values are escaped where they are interpolated, and
the escapes are undone again for the channels that render text verbatim.
"""

import json
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
import responses

from monad_monitor.alerts import (
    AlertHandler,
    escape_markdown,
    unescape_markdown,
)
from monad_monitor.config import ValidatorConfig
from monad_monitor.main import handle_huginn_timeout
from monad_monitor.state_machine import StateTransition, ValidatorState

MARKDOWN_NAME = "Node_A*B"
ESCAPED_NAME = "Node\\_A\\*B"
TELEGRAM_URL = "https://api.telegram.org/bottest-token/sendMessage"
DISCORD_URL = "https://discord.com/api/webhooks/1/hook"
SLACK_URL = "https://hooks.slack.com/services/T/B/C"


class TestEscapeHelpers:
    def test_escape_covers_every_legacy_markdown_character(self):
        assert escape_markdown("a_b*c`d[e]f\\g") == "a\\_b\\*c\\`d\\[e\\]f\\\\g"

    def test_escape_is_a_no_op_for_plain_names(self):
        assert escape_markdown("MictoNode Testnet") == "MictoNode Testnet"

    def test_unescape_restores_only_our_escapes(self):
        assert unescape_markdown(ESCAPED_NAME) == MARKDOWN_NAME
        # A backslash that is not ours survives
        assert unescape_markdown("C:\\path\\_x") == "C:\\path_x"


class TestAlertSiteEscaping:
    """Every place a validator name lands in a message must escape it"""

    def make_validator(self) -> ValidatorConfig:
        return ValidatorConfig(
            name=MARKDOWN_NAME,
            host="192.168.1.10",
            metrics_port=8889,
            rpc_port=8080,
            node_exporter_port=None,
            validator_secp="0xabc",
            enabled=True,
            network="testnet",
        )

    def test_state_transition_message_escapes_the_name(self):
        transition = StateTransition(
            from_state=ValidatorState.INACTIVE,
            to_state=ValidatorState.ACTIVE,
            validator_name=MARKDOWN_NAME,
            timestamp=time.time(),
        )

        message = transition.get_alert_message()

        assert ESCAPED_NAME in message
        assert f"*{MARKDOWN_NAME}" not in message

    def test_huginn_timeout_message_escapes_the_name(self):
        alerts = MagicMock()
        alerts.alert_critical.return_value = True
        state: Dict[str, Any] = {"last_huginn_timeout_count": 5}

        handle_huginn_timeout(
            state=state,
            validator_name=MARKDOWN_NAME,
            huginn_data={"timeout_count": 6},
            threshold=1,
            alerts=alerts,
        )

        message = alerts.alert_critical.call_args.args[0]
        assert ESCAPED_NAME in message
        assert f"*{MARKDOWN_NAME}" not in message


class TestChannelPayloadEscaping:
    """Telegram gets the escapes; Discord/Slack must not display backslashes"""

    @pytest.fixture
    def handler(self):
        return AlertHandler(
            telegram_token="test-token",
            telegram_chat_id="chat",
            pushover_user_key="user",
            pushover_app_token="app",
            discord_webhook_url=DISCORD_URL,
            slack_webhook_url=SLACK_URL,
        )

    def test_warning_payload_is_escaped_for_telegram_and_clean_elsewhere(self, handler):
        message = f"🟢 *{ESCAPED_NAME} ENTERED ACTIVE SET*"

        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, TELEGRAM_URL, json={"ok": True}, status=200)
            rsps.add(responses.POST, DISCORD_URL, json={}, status=204)
            rsps.add(responses.POST, SLACK_URL, body="ok", status=200)

            assert handler.alert_warning(message) is True

            bodies = {call.request.url: json.loads(call.request.body) for call in rsps.calls}

        telegram_body = bodies[TELEGRAM_URL]["text"]
        assert ESCAPED_NAME in telegram_body

        discord_body = bodies[DISCORD_URL]["embeds"][0]["description"]
        assert MARKDOWN_NAME in discord_body
        assert "\\_" not in discord_body and "\\*" not in discord_body

        slack_body = bodies[SLACK_URL]["attachments"][0]["text"]
        assert MARKDOWN_NAME in slack_body
        assert "\\_" not in slack_body and "\\*" not in slack_body

    def test_plain_messages_are_unchanged(self, handler):
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, TELEGRAM_URL, json={"ok": True}, status=200)
            rsps.add(responses.POST, DISCORD_URL, json={}, status=204)
            rsps.add(responses.POST, SLACK_URL, body="ok", status=200)

            handler.alert_info(f"✅ *{escape_markdown('MictoNode')} RECOVERED*")

            bodies = {call.request.url: json.loads(call.request.body) for call in rsps.calls}

        assert bodies[TELEGRAM_URL]["text"].endswith("*MictoNode RECOVERED*")
        assert bodies[DISCORD_URL]["embeds"][0]["description"] == "✅ *MictoNode RECOVERED*"
