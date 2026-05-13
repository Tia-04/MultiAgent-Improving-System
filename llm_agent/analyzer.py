"""
llm_agent/analyzer.py
---------------------
AnalyzerAgent: turns single-file test and Sonar signals into concise repair hints.
"""

import json


class AnalyzerAgent:
    SYSTEM_PROMPT = (
        "You analyze one Java file and return strict JSON only.\n"
        "Keep suggestions minimal, concrete, and limited to the provided file.\n"
        "Treat successful Java compilation as a hard requirement.\n"
        "Do not suggest edits that introduce new bugs, new code smells, or higher cognitive complexity.\n"
        "Do not output code blocks."
    )

    DIAG_JSON_SCHEMA = {
        "name": "single_file_hints",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string"},
                "targeted_changes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                },
                "check_after_fix": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 4,
                },
            },
            "required": [
                "root_cause",
                "targeted_changes",
                "check_after_fix",
            ],
            "additionalProperties": False,
        },
    }

    def __init__(self, llm):
        self.llm = llm

    def analyze_repo_sonar(self, context_file: dict, metrics: dict, issues: list) -> str:
        key_metrics = {k: metrics.get(k, "N/A") for k in ("bugs", "code_smells", "cognitive_complexity")}
        top_issues = "\n".join(
            (
                f"- line={i.get('line', '?')} rule={i.get('rule', '?')} message={i.get('message', '?')}"
            )
            for i in issues[:10]
        ) or "none"
        return self._analyze(
            context=(
                "Context: Sonar quality did not meet the target and this file is being rewritten.\n"
                "Goal: reduce the reported issues without introducing any new Sonar code smells or extra complexity.\n"
                f"METRICS: {key_metrics}\n"
                f"TOP ISSUES:\n{top_issues}\n\n"
                f"FILE:\n{self._format_context_file(context_file)}\n"
            )
        )

    def analyze_compile_errors(self, context_file: dict, compile_errors: str) -> str:
        return self._analyze(
            context=(
                "Context: Maven compilation failed after editing this file.\n"
                f"COMPILE_ERRORS:\n{compile_errors}\n\n"
                f"FILE:\n{self._format_context_file(context_file)}\n"
            )
        )

    def analyze_test_failures(self, context_file: dict, test_output: str) -> str:
        return self._analyze(
            context=(
                "Context: Tests failed after editing this file.\n"
                "Goal: make every remaining enabled test pass.\n"
                f"CURRENT_TEST_OUTPUT:\n{test_output}\n\n"
                f"FILE:\n{self._format_context_file(context_file)}\n"
            )
        )

    def _analyze(self, context: str) -> str:
        prompt = (
            "Return JSON with fields: root_cause, targeted_changes, check_after_fix.\n"
            "Rules:\n"
            "- root_cause: 1-3 short sentences.\n"
            "- targeted_changes: 1-5 short imperative actions for that file only.\n"
            "- Make sure the proposed edit still compiles as valid Java.\n"
            "- Do not propose edits that add new code smells, new duplication, or unnecessary branching.\n"
            "- For each targeted change, include the exact method name when known.\n"
            "- For each targeted change, quote one exact current line or short snippet from the file to anchor the edit.\n"
            "- check_after_fix: 1-5 concrete validations\n"
            "- Favor minimal edits over broad refactors.\n\n"
            f"{context}"
        )
        raw = self.llm.generate_response(
            prompt,
            temperature=0.25,
            system_prompt=self.SYSTEM_PROMPT,
            json_schema=self.DIAG_JSON_SCHEMA,
        ).strip()
        return self._json_to_text(raw)

    @staticmethod
    def _json_to_text(raw: str) -> str:
        payload = json.loads(raw)
        root = str(payload.get("root_cause", "")).strip()
        targeted = "\n".join(str(x).strip() for x in payload.get("targeted_changes", []))
        checks = "\n".join(str(x).strip() for x in payload.get("check_after_fix", []))
        return (
            f"ROOT_CAUSE:\n{root}\n\n"
            f"TARGETED_CHANGES:\n{targeted}\n\n"
            f"CHECK_AFTER_FIX:\n{checks}"
        )

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
