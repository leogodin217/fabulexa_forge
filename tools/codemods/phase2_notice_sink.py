#!/usr/bin/env python
"""Codemod: thread notice_sink=discard_notice_sink through every
iter_stream_events / stream_export / seed_mixer_run call site, and ensure the
`from _support.notices import discard_notice_sink` import is present.

Sprint: streaming-authoring-parity, Phase 2.

Run: uv run --with libcst python tools/codemods/phase2_notice_sink.py <files...>
"""

from __future__ import annotations

import sys

import libcst as cst

TARGET_CALLS = {"iter_stream_events", "stream_export", "seed_mixer_run"}


class AddNoticeSinkArg(cst.CSTTransformer):
    def __init__(self) -> None:
        self.changed = False

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        func = updated_node.func
        if not (isinstance(func, cst.Name) and func.value in TARGET_CALLS):
            return updated_node
        for arg in updated_node.args:
            if arg.keyword is not None and arg.keyword.value == "notice_sink":
                return updated_node
        new_arg = cst.Arg(
            keyword=cst.Name("notice_sink"),
            value=cst.Name("discard_notice_sink"),
            equal=cst.AssignEqual(
                whitespace_before=cst.SimpleWhitespace(""),
                whitespace_after=cst.SimpleWhitespace(""),
            ),
        )
        args = list(updated_node.args)
        args[-1] = args[-1].with_changes(
            comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
        )
        args.append(new_arg)
        self.changed = True
        return updated_node.with_changes(args=args)


class EnsureImport(cst.CSTTransformer):
    """Insert `from _support.notices import discard_notice_sink` if missing,
    or add `discard_notice_sink` to an existing `from _support.notices import
    ...` statement.
    """

    def __init__(self) -> None:
        self.has_target_import = False
        self.inserted = False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        module = node.module
        if (
            isinstance(module, cst.Attribute)
            and isinstance(module.value, cst.Name)
            and module.value.value == "_support"
            and module.attr.value == "notices"
        ):
            names = node.names
            if isinstance(names, list):
                for alias in names:
                    if (
                        isinstance(alias.name, cst.Name)
                        and alias.name.value == "discard_notice_sink"
                    ):
                        self.has_target_import = True

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom:
        module = updated_node.module
        if (
            isinstance(module, cst.Attribute)
            and isinstance(module.value, cst.Name)
            and module.value.value == "_support"
            and module.attr.value == "notices"
            and not self.has_target_import
            and not self.inserted
        ):
            names = updated_node.names
            if isinstance(names, list):
                new_names = list(names)
                new_names[-1] = new_names[-1].with_changes(
                    comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
                )
                new_names.append(cst.ImportAlias(name=cst.Name("discard_notice_sink")))
                new_names.sort(key=lambda a: a.name.value)
                self.inserted = True
                self.has_target_import = True
                return updated_node.with_changes(names=new_names)
        return updated_node

    def leave_Module(
        self, original_node: cst.Module, updated_node: cst.Module
    ) -> cst.Module:
        if self.has_target_import:
            return updated_node
        new_import = cst.SimpleStatementLine(
            body=[
                cst.ImportFrom(
                    module=cst.Attribute(
                        value=cst.Name("_support"), attr=cst.Name("notices")
                    ),
                    names=[cst.ImportAlias(name=cst.Name("discard_notice_sink"))],
                )
            ]
        )
        body = list(updated_node.body)
        insert_at = _find_import_insert_index(body)
        body.insert(insert_at, new_import)
        self.inserted = True
        return updated_node.with_changes(body=body)


def _find_import_insert_index(body: list[cst.BaseStatement]) -> int:
    """First-party import block: after stdlib/third-party imports, before the
    first import whose module name sorts after `_support.notices`, staying
    inside the leading run of import statements.
    """
    last_import_index = 0
    for i, stmt in enumerate(body):
        if isinstance(stmt, cst.SimpleStatementLine) and any(
            isinstance(s, (cst.Import, cst.ImportFrom)) for s in stmt.body
        ):
            last_import_index = i + 1
            imp = stmt.body[0]
            if isinstance(imp, cst.ImportFrom) and isinstance(
                imp.module, cst.Attribute
            ):
                module_value = imp.module.value
                assert isinstance(module_value, cst.Name)
                dotted = f"{module_value.value}.{imp.module.attr.value}"
                if dotted > "_support.notices":
                    return i
        else:
            if last_import_index:
                break
    return last_import_index


def process(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        source = f.read()
    module = cst.parse_module(source)

    call_transformer = AddNoticeSinkArg()
    module = module.visit(call_transformer)

    if not call_transformer.changed:
        return "no target calls found — unchanged"

    import_transformer = EnsureImport()
    module = module.visit(import_transformer)

    new_source = module.code
    if new_source == source:
        return "calls already had notice_sink — unchanged"

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_source)
    return "migrated"


def main(argv: list[str]) -> int:
    for path in argv:
        result = process(path)
        print(f"{path}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
