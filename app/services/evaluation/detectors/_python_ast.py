"""Small, side-effect-free Python AST helpers used by detectors (source is parsed, never executed)."""

from __future__ import annotations

import ast

def parse(source: str | None) -> ast.Module | None:
    if not source:
        return None
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def count_assertions(tree: ast.Module) -> int:
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            total += 1
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            last = name.rsplit(".", 1)[-1]
            if last.startswith("assert") or name.startswith("pytest.raises"):
                total += 1
    return total


def skip_markers(tree: ast.Module) -> list[tuple[int, str]]:
    """Return (lineno, marker) for skip/xfail decorators and calls."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name in {"pytest.mark.skip", "pytest.mark.xfail", "pytest.skip", "pytest.xfail", "unittest.skip"} or name.endswith(
            ".skipTest"
        ):
            found.append((node.lineno, name))
        elif name == "pytest.mark.skipif":
            found.append((node.lineno, name))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call):
                    continue  # already counted as a Call above
                name = _dotted(deco)
                if name in {"pytest.mark.skip", "pytest.mark.xfail", "unittest.skip"}:
                    found.append((deco.lineno, name))
    return sorted(set(found))


def eq_always_true(tree: ast.Module) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__eq__":
            body = [stmt for stmt in node.body if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))]
            if (
                len(body) == 1
                and isinstance(body[0], ast.Return)
                and isinstance(body[0].value, ast.Constant)
                and body[0].value.value is True
            ):
                lines.append(node.lineno)
    return lines


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and any(isinstance(comp, ast.Constant) and comp.value == "__main__" for comp in test.comparators)
    )


def exit_calls_outside_main(tree: ast.Module) -> list[int]:
    """sys.exit()/sys.exit(0)/os._exit() calls not inside a __main__ guard or a main()/cli() function."""
    lines: list[int] = []

    def visit(node: ast.AST, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_guarded = guarded
            if isinstance(child, ast.If) and _is_main_guard(child):
                child_guarded = True
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in {"main", "cli", "run_cli"}:
                child_guarded = True
            if isinstance(child, ast.Call) and not child_guarded:
                name = _dotted(child.func)
                if name == "os._exit":
                    lines.append(child.lineno)
                elif name == "sys.exit":
                    arg = child.args[0] if child.args else None
                    if arg is None or (isinstance(arg, ast.Constant) and arg.value in (0, None)):
                        lines.append(child.lineno)
            visit(child, child_guarded)

    visit(tree, False)
    return sorted(set(lines))


def broad_except_pass(tree: ast.Module) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        broad = node.type is None or (isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"})
        if not broad:
            continue
        body = node.body
        if len(body) == 1 and (
            isinstance(body[0], ast.Pass) or (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant))
        ):
            lines.append(node.lineno)
    return lines


def line_text(source: str | None, lineno: int) -> str:
    if not source:
        return ""
    lines = source.splitlines()
    return lines[lineno - 1] if 0 < lineno <= len(lines) else ""
