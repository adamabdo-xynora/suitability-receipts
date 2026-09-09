"""Structural tests for the boundary the README promises.

Two promises are made in prose elsewhere in this repository, and prose is not
enforcement. These tests parse every module under `src/` and fail if either is broken:

* `ANTHROPIC_API_KEY` is read in exactly one place. No other module imports `os` at all,
  which is stricter than "no other module reads that variable" and much easier to check
  than a data-flow analysis of where a string ends up.
* Every module that uses a model receives an already-constructed client. Only the client
  module imports `anthropic`; everything else takes a `ModelClient` as an argument.

A layering check comes with them: the determination code must not import the model layer,
and the rationale verifier must not import the model client. The verifier is the part
that says no, and it has to be runnable — and testable — with no client in the room.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "suitability_receipts"
ENVIRONMENT_READER = SRC / "llm" / "client.py"
MODULES = sorted(SRC.rglob("*.py"))


def imported_modules(path: pathlib.Path) -> set[str]:
    """Return the top-level names of every module `path` imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            imported.add(node.module.split(".")[0])
    return imported


def environment_accesses(path: pathlib.Path) -> set[str]:
    """Return every `environ`/`getenv`-shaped name `path` mentions."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {"environ", "environb", "getenv", "putenv"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in wanted:
            found.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in wanted:
            found.add(node.id)
    return found


def test_the_source_tree_is_not_empty() -> None:
    """Guard the guards: an empty file list would make every test below vacuous."""
    assert len(MODULES) >= 5
    assert ENVIRONMENT_READER in MODULES


def test_only_the_client_module_imports_os() -> None:
    """`ANTHROPIC_API_KEY` is read in exactly one place, structurally."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in MODULES
        if path != ENVIRONMENT_READER and "os" in imported_modules(path)
    ]
    assert offenders == []


def test_only_the_client_module_touches_the_environment() -> None:
    """No module other than the client mentions `environ` or `getenv` at all."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in MODULES
        if path != ENVIRONMENT_READER and environment_accesses(path)
    ]
    assert offenders == []


def test_the_client_module_does_read_the_environment() -> None:
    """The check above would pass just as well if nothing ever read a credential."""
    assert "os" in imported_modules(ENVIRONMENT_READER)
    assert "environ" in environment_accesses(ENVIRONMENT_READER)


def test_only_the_client_module_imports_the_sdk() -> None:
    """Every other module takes an already-constructed client by injection."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in MODULES
        if path != ENVIRONMENT_READER and "anthropic" in imported_modules(path)
    ]
    assert offenders == []


@pytest.mark.parametrize("module", ["models.py", "engine.py"])
def test_the_determination_code_does_not_import_the_model_layer(module: str) -> None:
    """Determination is deterministic code over documented data. It calls no model."""
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(name.startswith("suitability_receipts.llm") for name in imported)


def test_the_verifier_does_not_import_the_client() -> None:
    """The verifier refuses rationales; it must run with no client and no key."""
    tree = ast.parse((SRC / "llm" / "verification.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "suitability_receipts.llm.client" not in imported
