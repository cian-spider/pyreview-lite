#!/usr/bin/env python3
"""pyreview-lite: mechanical checks on one Python file, each with the command that shows it.

This is the free half of a code review. It finds the issues a rule can decide, and it
prints, for every finding, a command you can run to see the same thing yourself. It does
not decide whether your code is correct: that needs running it against your inputs, which
is the part I do by hand.

    python3 pyreview_lite.py path/to/file.py
    python3 pyreview_lite.py path/to/file.py --json

Exit code is 0 whether or not findings exist, 2 on bad input. No dependencies, stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

# Each rule reports: line, a short label, the line as written, why it matters, and the
# command that reproduces the observation. Keep the "why" to one sentence; a rule that
# cannot say why in one sentence is a style preference, not a finding.


def _demo(source: str) -> str:
    """Turn a multi-line snippet into a `python3 -c` command that survives a shell.

    A raw newline inside a double-quoted shell string is not a newline, so a demo written
    as `python3 -c "def f():\\n    ..."` fails with a SyntaxError instead of showing the
    thing it claims. Wrapping the snippet in exec() with escaped newlines keeps the whole
    command on one shell line.
    """
    if "$" in source or "`" in source:
        raise ValueError("demo source must not contain shell specials")
    body = source.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f'python3 -c "exec(\'{body}\')"'


def _rule_mutable_default(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple]:
    out = []
    for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
        if isinstance(default, (ast.List, ast.Dict, ast.Set)):
            out.append((
                default.lineno,
                "mutable-default",
                f"{node.name}() has a mutable default {type(default).__name__.lower()}",
                "The same object is reused across calls, so a caller that mutates it changes "
                "the default for every later call.",
                _demo("def f(x=[]):\n    x.append(1); return x\nprint(f(), f())"),
            ))
    return out


def _rule_bare_except(node: ast.ExceptHandler) -> list[tuple]:
    if node.type is not None:
        return []
    swallowed = all(isinstance(b, ast.Pass) for b in node.body)
    label = "except-pass" if swallowed else "bare-except"
    why = (
        "The handler does nothing, so a failure becomes a silent success and the caller "
        "cannot tell the difference."
        if swallowed
        else "A bare except catches KeyboardInterrupt and SystemExit as well as your error, "
        "so an interrupted run continues with a broken state."
    )
    return [(node.lineno, label, "except:", why,
             _demo("try:\n    raise KeyboardInterrupt\nexcept:\n    print('swallowed')"))]


def _rule_none_compare(node: ast.Compare) -> list[tuple]:
    out = []
    for op, right in zip(node.ops, node.comparators):
        if isinstance(right, ast.Constant) and right.value is None and isinstance(op, (ast.Eq, ast.NotEq)):
            out.append((
                node.lineno,
                "none-comparison",
                ast.unparse(node),
                "`== None` calls __eq__, which a class can override to return True for "
                "anything; `is None` cannot be overridden.",
                _demo("class X:\n def __eq__(s,o): return True\n"
                      "print(X() == None, X() is None)"),
            ))
    return out


def _rule_eval(node: ast.Call) -> list[tuple]:
    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    if name not in ("eval", "exec"):
        return []
    literal = len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
    if literal:
        return []
    return [(node.lineno, "dynamic-eval", ast.unparse(node),
             f"{name}() on a value that is not a literal runs whatever text it is given, "
             "so the trust boundary is the input, not this file.",
             "python3 -c \"print(eval('1+1'), 'runs whatever you pass it')\"")]


def _rule_shell_true(node: ast.Call) -> list[tuple]:
    for kw in node.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return [(node.lineno, "shell-true", ast.unparse(node),
                     "shell=True passes the string to a shell, so any space or $ in the "
                     "arguments is interpreted rather than passed through.",
                     "python3 -c \"import shlex;print(shlex.split(\\\"a b; whoami\\\"))\"")]
    return []


def _rule_assert_validation(node: ast.Assert) -> list[tuple]:
    return [(node.lineno, "assert-as-validation", ast.unparse(node),
             "`python -O` strips assert statements, so a check written this way silently "
             "disappears in optimised builds.",
             "python3 -O -c \"assert False; print('this line ran anyway')\"")]


def _rule_float_equality(node: ast.Compare) -> list[tuple]:
    for op, right in zip(node.ops, node.comparators):
        if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(right, ast.Constant) and isinstance(right.value, float):
            return [(node.lineno, "float-equality", ast.unparse(node),
                     "Two floats that print the same can compare unequal; compare with a "
                     "tolerance instead.",
                     "python3 -c \"print(0.1+0.2 == 0.3, 0.1+0.2)\"")]
    return []


def _rule_fstring_noop(node: ast.JoinedStr) -> list[tuple]:
    if any(isinstance(v, ast.FormattedValue) for v in node.values):
        return []
    return [(node.lineno, "f-string-without-fields", ast.unparse(node),
             "An f-string with no placeholders is a plain string; the f is noise that "
             "suggests an interpolation was meant to be there.",
             "python3 -c \"import ast;print([n.lineno for n in ast.walk(ast.parse('x = f\\\"abc\\\"')) if type(n).__name__=='JoinedStr'])\"")]


def _rule_shadowed_builtin(node: ast.Name) -> list[tuple]:
    if not isinstance(node.ctx, ast.Store):
        return []
    if node.id not in {"list", "dict", "set", "str", "int", "float", "id", "type", "input", "open", "sum", "min", "max", "all", "any", "format"}:
        return []
    return [(node.lineno, "shadowed-builtin", node.id,
             f"`{node.id}` now refers to your value, so the builtin is unreachable in this "
             "scope and any later use is a bug waiting for a branch.",
             _demo(f"{node.id} = 5\ntry:\n    print({node.id}(1))\n"
                   "except TypeError as e:\n    print('the builtin is gone:', e)"))]


def _rule_compare_is_literal(node: ast.Compare) -> list[tuple]:
    for op, right in zip(node.ops, node.comparators):
        if isinstance(op, ast.Is) and isinstance(right, ast.Constant) and not isinstance(right.value, (type(None), type(True), type(False))):
            return [(node.lineno, "is-with-literal", ast.unparse(node),
                     "`is` compares identity, and small integers and short strings are "
                     "interned only by accident of the interpreter.",
                     "python3 -c \"a='x'*10;b=''.join(['x']*10);print(a is b, a==b)\"")]
    return []


# A rule is only asked about the node types it can actually judge. Running every rule
# against every node looks harmless and produces a report where `assert-as-validation`
# fires on an `import` line, which is how a linter loses its reader in one run.
RULES: tuple[tuple[type, object], ...] = (
    ((ast.FunctionDef, ast.AsyncFunctionDef), _rule_mutable_default),
    (ast.ExceptHandler, _rule_bare_except),
    (ast.Compare, _rule_none_compare),
    (ast.Compare, _rule_float_equality),
    (ast.Compare, _rule_compare_is_literal),
    (ast.Call, _rule_eval),
    (ast.Call, _rule_shell_true),
    (ast.Assert, _rule_assert_validation),
    (ast.JoinedStr, _rule_fstring_noop),
    (ast.Name, _rule_shadowed_builtin),
)


def check(source: str) -> list[dict]:
    tree = ast.parse(source)
    findings: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        for types, rule in RULES:
            if not isinstance(node, types):
                continue
            try:
                hits = rule(node)
            except (TypeError, AttributeError, ValueError):
                continue
            for line, label, shown, why, reproduce in hits:
                if (line, label) in seen:
                    continue
                seen.add((line, label))
                findings.append({
                    "line": line,
                    "rule": label,
                    "code": shown,
                    "why": why,
                    "reproduce": reproduce,
                })
    findings.sort(key=lambda f: (f["line"], f["rule"]))
    return findings


def render(path: Path, findings: list[dict]) -> str:
    if not findings:
        return f"{path}: no mechanical findings. That is not a clean bill of health.\n"
    lines = [f"{path}: {len(findings)} mechanical finding(s)\n"]
    for f in findings:
        lines.append(f"{path}:{f['line']}  [{f['rule']}]  {f['code']}")
        lines.append(f"    why: {f['why']}")
        lines.append(f"    see it: {f['reproduce']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mechanical checks on one Python file, each with a reproducing command.")
    ap.add_argument("file", type=Path)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if not args.file.is_file():
        print(f"not a file: {args.file}", file=sys.stderr)
        return 2
    source = args.file.read_text(encoding="utf-8", errors="replace")
    try:
        findings = check(source)
    except SyntaxError as exc:
        print(f"{args.file}:{exc.lineno}: does not parse, no checks ran: {exc.msg}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"file": str(args.file), "findings": findings}, indent=1))
    else:
        sys.stdout.write(render(args.file, findings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
