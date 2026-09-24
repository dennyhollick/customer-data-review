"""Schema validation: jsonschema when installed, else a bundled stdlib subset."""

import json
from pathlib import Path

try:
    import jsonschema
except ImportError:  # plain Python host: use the bundled validator
    jsonschema = None

_SCHEMAS_DIR = Path(__file__).resolve().parent


def load_schema(schema_name: str) -> dict:
    """Load a JSON schema by name from the schemas/ directory.

    Args:
        schema_name: Name without extension, e.g. "plan" loads plan.schema.json.
    """
    path = _SCHEMAS_DIR / f"{schema_name}.schema.json"
    with open(path) as f:
        return json.load(f)


def validate_json(instance: dict, schema_name: str) -> list[str]:
    """Validate a JSON object against a named schema.

    Returns a list of error strings. Empty list means valid.
    """
    schema = load_schema(schema_name)
    if jsonschema is not None:
        found = jsonschema.Draft7Validator(schema).iter_errors(instance)
    else:
        from skill.schemas.mini_schema import iter_errors
        found = iter_errors(instance, schema)
    errors = []
    for error in sorted(found, key=lambda e: [str(p) for p in e.path]):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{path}: {error.message}")
    return errors


def validate_with_conditionals(instance: dict, schema_name: str) -> list[str]:
    """Schema validation + programmatic conditional checks.

    Runs validate_json first, then applies conditional rules that are
    too complex for JSON Schema (e.g., excluded/exclusion_reason dependency).
    """
    errors = validate_json(instance, schema_name)

    # Conditional: plan files — excluded=true requires exclusion_reason
    if schema_name == "plan":
        for i, source in enumerate(instance.get("files", [])):
            if not isinstance(source, dict):
                continue
            if source.get("excluded") is True and not source.get("exclusion_reason"):
                errors.append(
                    f"files.{i}: excluded is true but exclusion_reason is missing"
                )

    return errors
