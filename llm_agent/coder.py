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
        "Do not return diffs, markdown, or explanations."
    )

    AFTER_JSON_SCHEMA = {
        "name": "single_file_edit",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
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
                    "minItems": 1,
                    "maxItems": 1,
                },
            },
            "required": ["files"],
            "additionalProperties": False,
        },
    }

    def __init__(self, llm):
        self.llm = llm

    def code_repo_files(
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

Return JSON only. Field: files (array)

Output example:
{{
  "files": [
    {{
      "file_name": "{target_path}",
      "content": "full updated file text here"
    }}
  ]
}}

Rules:
- Return exactly one file: {target_path}
- Each content value must be the complete updated file, not a fragment
- Keep the edit minimal and behavior-preserving unless the hints require otherwise
- Make sure the updated file compiles as valid Java
- Do not invent changes outside the provided file

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
        edited_files = self._extract_files_from_json(response, target_path, original_content)
        return {"filename": "candidate.files.json", "files": edited_files}

    @staticmethod
    def _extract_files_from_json(text: str, target_path: str, original_content: str) -> list[dict]:
        """Parse strict JSON model output and extract validated files."""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model output is not valid JSON: {text[:500]}") from exc

        files = payload.get("files")
        if not isinstance(files, list):
            raise ValueError("Model JSON does not contain array field 'files'.")
        if len(files) != 1:
            raise ValueError(f"Field 'files' must contain exactly one file, got {len(files)}.")

        validated_files = []
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("Each files entry must be an object.")
            path = str(item.get("file_name", "")).strip().replace("\\", "/")
            content = item.get("content")
            if not path:
                raise ValueError("A file entry is missing 'file_name'.")
            if path != target_path:
                raise ValueError(f"Returned file does not match the provided target: {path}")
            if not isinstance(content, str):
                raise ValueError(f"File '{path}' is missing string field 'content'.")
            if not content:
                raise ValueError(f"File '{path}' has empty content.")
            if content == original_content:
                continue
            validated_files.append({"path": path, "content": content})

        if not validated_files:
            raise ValueError("Model returned no actual file changes.")

        return validated_files

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
