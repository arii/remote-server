import ast
import json
import re
import unittest
from pathlib import Path

from cli import repo_root
from jsonschema import Draft202012Validator

from backend.skill_format import SKILL_SCHEMA_PATH, validate_skill

ROOT = Path(repo_root())
DOC = ROOT / "docs" / "skill-contract.md"


class SkillContractDocsTest(unittest.TestCase):
    def test_complete_json_example_matches_schema_and_validator(self):
        examples = re.findall(r"```json\n(.*?)\n```", DOC.read_text(), re.DOTALL)
        self.assertEqual(len(examples), 1)
        skill = json.loads(examples[0])
        self.assertEqual(validate_skill(skill), [])
        schema = json.loads(SKILL_SCHEMA_PATH.read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(skill)

    def test_named_environment_variables_are_implemented(self):
        doc = DOC.read_text()
        configuration = doc.split("## Configuration\n", 1)[1].split("## Driver", 1)[0]
        documented = set(re.findall(r"`([A-Z][A-Z_]+)`", configuration))
        documented.update(re.findall(r"\b(?:BROWSER|AGENT_CONTROL)_[A-Z_]+\b", doc))
        self.assertIn("BROWSER_GRID_URL", documented)
        self.assertIn("AGENT_CONTROL_STATE_FILE", documented)
        implemented = set()
        for path in (ROOT / "backend").rglob("*.py"):
            if "tests" in path.relative_to(ROOT / "backend").parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function = ast.unparse(node.func)
                if function not in ("os.environ.get", "os.getenv", "data_path"):
                    continue
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value, str
                ):
                    implemented.add(argument.value)
        self.assertEqual(documented - implemented, set())
