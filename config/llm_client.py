"""
config/llm_client.py
--------------------
LLM client abstractions.

CodexClient  - calls OpenAI's Responses API (codex-mini-latest / o4-mini etc.)
"""

import os
import json
import requests


class CodexClient:
    """
    wrapper around the OpenAI Responses API.
    """

    def __init__(self, model: str = "codex-mini-latest", api_key: str | None = None):
        self.model   = model
        raw_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
        self.api_key = raw_key.strip().strip('"').strip("'")
        if not self.api_key:
            raise ValueError(
                "OPENAI_API_KEY is missing or empty. Check your .env file and reload it."
            )
        self.url     = "https://api.openai.com/v1/responses"

    def generate_response(
        self,
        prompt: str,
        temperature: float = 0.2,
        json_schema: dict | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """
        Send a completion request to OpenAI Responses API.

        Supports:
          - optional system prompt (role-separated input)
          - optional strict JSON schema output format

        Note:
          - `temperature` is kept in the method signature only for
            retrocompatibility with other clients (e.g., OllamaClient).
          - For this OpenAI model path, temperature is not sent because this
            model rejects that parameter.
        """
        # NOTE: temperature is kept in the method signature only for retrocompatibility with other clients (OllamaClient).
        # NOTE: TODO REMOVE temperature from signature and all callers once we have a unified client interface.
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }
        if system_prompt:
            # Role-separated input improves policy stability vs single raw prompt.
            input_payload = [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            ]
        else:
            input_payload = prompt

        payload = {
            "model": self.model,
            "input": input_payload,
        }
        if json_schema:
            # Enforce structured output when caller needs deterministic parsing.
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": json_schema.get("name", "structured_output"),
                    "schema": json_schema["schema"],
                    "strict": json_schema.get("strict", True),
                }
            }
        session = requests.Session()
        session.trust_env = False
        resp = session.post(self.url, headers=headers, json=payload, timeout=1000)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            prefix = self.api_key[:12] + "..." if len(self.api_key) > 12 else self.api_key
            detail = resp.text[:500]
            raise RuntimeError(
                f"OpenAI Responses API request failed for model '{self.model}'. "
                f"Key prefix loaded: {prefix}. HTTP {resp.status_code}. Response: {detail}"
            ) from exc
        data = resp.json()
        text = self._extract_text(data)
        self._print_model_output(text, json_schema=json_schema)
        return text

    @staticmethod
    def _print_model_output(text: str, json_schema: dict | None = None) -> None:
        """
        Print model output for logs.

        If caller requested structured JSON, pretty-print it with indentation.
        Fallback to raw text if parsing fails.
        """
        if json_schema:
            try:
                payload = json.loads(text)
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return
            except json.JSONDecodeError:
                pass
        print(text)

    @staticmethod
    def _extract_text(data: dict) -> str:
        """
        Extract assistant text from Responses API payload.
        Compatible with GPT-5 Nano models.
        """

        # Preferred fast path
        text = data.get("output_text")
        if isinstance(text, str) and text.strip():
            return text.strip()

        # GPT-5 Nano commonly returns text inside output[*].content[*].text
        output = data.get("output", [])

        for item in output:
            if item.get("type") != "message":
                continue

            for content in item.get("content", []):
                text = content.get("text")

                if isinstance(text, str) and text.strip():
                    return text.strip()

        raise RuntimeError(
            "OpenAI response did not contain extractable text. "
            f"Payload snippet: {str(data)[:500]}"
        )