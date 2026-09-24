"""Run bundled CLI scripts or pure helpers without installing the skill package."""

import argparse
import importlib
import importlib.util
import inspect
import json
import re
import sys
import types
import typing
from pathlib import Path


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required.")
    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) < 2 or not re.fullmatch(r"[a-z][a-z0-9_]*", sys.argv[1]):
        raise SystemExit("Usage: python /path/to/skill/scripts/run.py SCRIPT [arguments]")
    name = sys.argv[1]
    if name == "run" or not (root / "scripts" / f"{name}.py").is_file():
        raise SystemExit(f"Unknown bundled script: {name}")
    package = types.ModuleType("skill")
    package.__path__ = [str(root)]
    sys.modules["skill"] = package
    module = importlib.import_module(f"skill.scripts.{name}")
    arguments = sys.argv[2:]
    if "--call" in arguments:
        parser = argparse.ArgumentParser(description="Call a pure helper using JSON keyword arguments.")
        parser.add_argument("--call", required=True)
        parser.add_argument("--args-json", required=True)
        parser.add_argument("--output")
        parser.add_argument("--write-arg", help="Save this mutated argument to --output; print the function result as diagnostics")
        args = parser.parse_args(arguments)
        function = getattr(module, args.call, None)
        if not inspect.isfunction(function) or function.__module__ != module.__name__ or args.call.startswith("_"):
            parser.error("Choose a public function defined by the selected script.")
        try:
            kwargs = json.loads(Path(args.args_json).read_text(encoding="utf-8"))
            if not isinstance(kwargs, dict):
                raise ValueError("Arguments must be a JSON object keyed by parameter name.")
            if args.write_arg and (not args.output or args.write_arg not in kwargs):
                raise ValueError("--write-arg needs an existing argument name and --output.")
            for parameter, annotation in typing.get_type_hints(function).items():
                if (annotation is set or typing.get_origin(annotation) is set) and isinstance(kwargs.get(parameter), list):
                    kwargs[parameter] = set(kwargs[parameter])
            returned = function(**kwargs)
            selected = kwargs[args.write_arg] if args.write_arg else returned
            result = json.dumps(selected, ensure_ascii=False, indent=2) + "\n"
            if args.write_arg:
                print(json.dumps({"changes": returned}, ensure_ascii=False, indent=2))
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(result, encoding="utf-8")
            else:
                print(result, end="")
        except (OSError, ValueError, TypeError) as exc:
            parser.exit(2, f"Helper failed: {exc}\n")
    elif hasattr(module, "main"):
        sys.argv = [str(root / "scripts" / f"{name}.py"), *arguments]
        module.main()
    elif arguments == ["--help"]:
        print("No CLI. Use --call FUNCTION --args-json FILE [--output FILE] [--write-arg PARAM]. Public helpers:")
        for fn, function in inspect.getmembers(module, inspect.isfunction):
            if function.__module__ == module.__name__ and not fn.startswith("_"):
                print(f"  {fn}{inspect.signature(function)}")
                print(inspect.getdoc(function) or "")
    else:
        raise SystemExit(f"{name} is a function library. Use --help, then --call FUNCTION --args-json FILE; no work has run.")


if __name__ == "__main__":
    main()
