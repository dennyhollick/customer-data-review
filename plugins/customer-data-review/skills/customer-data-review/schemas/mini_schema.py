"""Small JSON Schema (draft 7 subset) validator using only the standard library.

Used when the `jsonschema` package is not installed, so the skill runs on a
plain Python 3.10+ host. Covers the keywords the bundled schemas use.
"""

import re

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer()),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _same(a, b):
    """JSON equality: booleans never equal numbers (Python treats True == 1)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


class Error:
    def __init__(self, path, message):
        self.absolute_path = list(path)
        self.path = list(path)
        self.message = message


def iter_errors(instance, schema, path=()):
    """Yield Error objects for every violation (mirrors Draft7Validator.iter_errors)."""
    if schema is True or schema == {}:
        return
    if schema is False:
        yield Error(path, "False schema does not allow any value")
        return
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_TYPES[k](instance) for k in kinds):
            yield Error(path, f"{instance!r} is not of type {', '.join(repr(k) for k in kinds)}")
            return
    if "enum" in schema and not any(_same(instance, option) for option in schema["enum"]):
        yield Error(path, f"{instance!r} is not one of {schema['enum']!r}")
    if "const" in schema and not _same(instance, schema["const"]):
        yield Error(path, f"{schema['const']!r} was expected")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            yield Error(path, f"{instance!r} is too short")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            yield Error(path, f"{instance!r} is too long")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            yield Error(path, f"{instance!r} does not match {schema['pattern']!r}")
    if _TYPES["number"](instance):
        if "minimum" in schema and instance < schema["minimum"]:
            yield Error(path, f"{instance!r} is less than the minimum of {schema['minimum']!r}")
        if "maximum" in schema and instance > schema["maximum"]:
            yield Error(path, f"{instance!r} is greater than the maximum of {schema['maximum']!r}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            yield Error(path, f"{instance!r} is less than or equal to the minimum of {schema['exclusiveMinimum']!r}")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            yield Error(path, f"{instance!r} is greater than or equal to the maximum of {schema['exclusiveMaximum']!r}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            yield Error(path, f"{instance!r} is too short")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            yield Error(path, f"{instance!r} is too long")
        if schema.get("uniqueItems"):
            seen = []
            for item in instance:
                if any(_same(item, other) for other in seen):
                    yield Error(path, f"{instance!r} has non-unique elements")
                    break
                seen.append(item)
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                yield from iter_errors(item, items, (*path, index))
    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                yield Error(path, f"{name!r} is a required property")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                yield from iter_errors(value, properties[name], (*path, name))
        extra = schema.get("additionalProperties", True)
        unknown = [name for name in instance if name not in properties]
        if extra is False and unknown:
            yield Error(path, f"Additional properties are not allowed ({', '.join(repr(u) for u in unknown)} were unexpected)")
        elif isinstance(extra, dict):
            for name in unknown:
                yield from iter_errors(instance[name], extra, (*path, name))
    for sub in schema.get("allOf", []):
        yield from iter_errors(instance, sub, path)
    if "anyOf" in schema and not any(not list(iter_errors(instance, sub, path)) for sub in schema["anyOf"]):
        yield Error(path, f"{instance!r} is not valid under any of the given schemas")
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if not list(iter_errors(instance, sub, path)))
        if matches != 1:
            yield Error(path, f"{instance!r} is not valid under exactly one of the given schemas")
