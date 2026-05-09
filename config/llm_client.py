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
        Extract assistant text across known Responses API payload variants.

        Tries output_text first, then walks output/content blocks.
        """
        # Newer Responses API shape can include a top-level output_text string.
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        # Some SDK/server variants return output_text as a list of text chunks.
        if isinstance(output_text, list):
            chunks = [c for c in output_text if isinstance(c, str) and c]
            joined = "".join(chunks).strip()
            if joined:
                return joined

        # Fallback: walk output blocks and collect text from recognized content types.
        out = data.get("output", [])
        collected = []
        if isinstance(out, list):
            for item in out:
                if not isinstance(item, dict):
                    continue
                content = item.get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    txt = block.get("text")
                    if isinstance(txt, str):
                        collected.append(txt)
                        continue
                    if block.get("type") in ("output_text", "text"):
                        val = block.get("value")
                        if isinstance(val, str):
                            collected.append(val)

        joined = "".join(collected).strip()
        if joined:
            return joined

        snippet = str(data)[:500]
        raise RuntimeError(
            "OpenAI response did not contain extractable text in output_text/output content. "
            f"Payload snippet: {snippet}"
        )

