"""Claude on Amazon Bedrock writes the *gentle* texts: the care nudge and the day summary.

Alerts never wait for, or depend on, a model. If Bedrock is not configured or fails,
HeatWatch falls back to a fixed template and says so in the UI.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("heatwatch.ai")

SYSTEM = (
    "You write short messages for HeatWatch, an app that helps families look after an older "
    "relative who lives alone, especially during heatwaves. Readers are family members or "
    "neighbours. Be warm, calm and concrete. Never alarm, never diagnose, never mention cameras "
    "or surveillance. Use the person's name, not pronouns. Plain text only, no markdown, no emoji."
)


class Writer:
    def __init__(self, *, enabled: bool, region: str, model_id: str, timeout_s: float = 20.0):
        self.model_id = model_id
        self.timeout_s = timeout_s
        self.client = None
        self.status = "Bedrock disabled (BEDROCK_ENABLED=false)"
        if not enabled:
            return
        try:
            import botocore.session

            if botocore.session.get_session().get_credentials() is None:
                self.status = "Amazon Bedrock not configured (no AWS credentials found) - using templates"
                return
        except ImportError:
            pass  # let the SDK resolve credentials itself
        try:
            from anthropic import AnthropicBedrockMantle

            self.client = AnthropicBedrockMantle(aws_region=region, timeout=timeout_s, max_retries=1)
            self.status = f"Claude on Amazon Bedrock ({model_id}, {region})"
        except Exception as exc:  # missing AWS credentials/profile surfaces here
            self.status = f"Bedrock unavailable: {exc}"

    def _complete(self, prompt: str) -> str | None:
        response = self.client.messages.create(
            model=self.model_id,
            max_tokens=2000,
            system=SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or None

    async def write(self, prompt: str, fallback: str) -> tuple[str, str]:
        """Returns (text, author) where author is the model id or 'template'."""
        if self.client is None:
            return fallback, "template"
        try:
            text = await asyncio.wait_for(asyncio.to_thread(self._complete, prompt), self.timeout_s + 5)
        except Exception as exc:  # network, auth, throttling: never block the product on it
            log.warning("Bedrock call failed: %s", exc)
            self.status = f"Bedrock call failed, using templates: {type(exc).__name__}"
            return fallback, "template"
        if not text:
            return fallback, "template"
        self.status = f"Claude on Amazon Bedrock ({self.model_id})"
        return text, self.model_id
