"""
llm_agent/coder.py
------------------
CoderAgent: generates post-edit contents for a single Java file.
"""

import json


class CoderAgent:
    SYSTEM_PROMPT = (
        "You edit one Java file and return strict JSON only.\n"
        "Return exactly one changed file with its full content.\n"
        "The updated file must compile as valid Java.\n"
        "The updated file must not break existing behavior unless the hints require otherwise.\n"
        "Do not return diffs, markdown, or explanations."
    )

    AFTER_JSON_SCHEMA = {
        "name": "single_file_edit",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                },
                "content": {
                    "type": "string",
                },
            },
            "required": ["file_name", "content"],
            "additionalProperties": False,
        },
    }

    def __init__(self, llm):
        self.llm = llm

    def code_repo_file(
        self,
        context_file: dict,
        hints: str | None = None,
    ) -> dict:
        """
        Produce full post-edit file contents for the repo-based experiment.

        `context_file` should contain:
          - path: repo-relative path
          - content: full file content
        """
        target_path = str(context_file.get("path", "")).strip().replace("\\", "/")
        original_content = str(context_file.get("content", ""))
        if not target_path:
            raise ValueError("Context file is missing 'path'.")

        context_block = self._format_context_file(context_file)
        hint_block = (hints or "").strip()

        prompt = f"""Generate the post-edit contents for one Java file.

Return JSON only.

Output example:
{{
  "file_name": "{target_path}",
  "content": "full updated file text here"
}}

Rules:
- Return exactly one file: {target_path}
- The content value must be the complete updated file, not a fragment
- Keep the edit minimal and behavior-preserving unless the hints require otherwise
- Make sure the updated file compiles as valid Java
- Do not invent changes outside the provided file
- Maintain javadoc notation and comments unless the hints require otherwise

Repository context file:
{context_block}

Hints:
{hint_block}"""
        response = self.llm.generate_response(
            prompt,
            temperature=0.2,
            json_schema=self.AFTER_JSON_SCHEMA,
            system_prompt=self.SYSTEM_PROMPT,
        )
        return self._extract_file_from_json(response, target_path, original_content)

    @staticmethod
    def _extract_file_from_json(text: str, target_path: str, original_content: str) -> dict:
        """Parse strict JSON model output and extract one validated file."""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model output is not valid JSON: {text[:500]}") from exc

        path = str(payload.get("file_name", "")).strip().replace("\\", "/")
        content = payload.get("content")
        if not path:
            raise ValueError("Model JSON is missing 'file_name'.")
        if path != target_path:
            raise ValueError(f"Returned file does not match the provided target: {path}")
        if not isinstance(content, str):
            raise ValueError(f"File '{path}' is missing string field 'content'.")
        if not content:
            raise ValueError(f"File '{path}' has empty content.")
        if content == original_content:
            raise ValueError("Model returned no actual file changes.")

        return {"path": path, "content": content}

    @staticmethod
    def _format_context_file(item: dict) -> str:
        path = str(item.get("path", "")).strip()
        content = str(item.get("content", ""))
        return (
            f"FILE: {path}\n"
            "```java\n"
            f"{content}\n"
            "```"
        )
