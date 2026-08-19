"""docs/usage.md must mention every subcommand argparse actually defines. An earlier
version of this project's docs documented under half its commands and nobody noticed
until an audit — this makes that specific drift impossible to reintroduce silently."""
import argparse
from pathlib import Path

from vigil import cli


def _subcommand_names() -> set[str]:
    """Run cli.main's argparse setup far enough to see every add_parser() call, then bail
    out via --help before any command actually executes."""
    names: set[str] = set()
    real_add_parser = argparse._SubParsersAction.add_parser

    def spy(self, name, **kwargs):
        names.add(name)
        return real_add_parser(self, name, **kwargs)

    argparse._SubParsersAction.add_parser = spy
    try:
        cli.main(["--help"])
    except SystemExit:
        pass
    finally:
        argparse._SubParsersAction.add_parser = real_add_parser
    return names


def test_every_subcommand_is_documented_in_usage_md():
    names = _subcommand_names()
    assert names, "failed to discover any subcommands — the spy in this test broke"
    usage = (Path(__file__).parent.parent / "docs" / "usage.md").read_text()
    missing = [n for n in sorted(names) if f"vigil {n}" not in usage and f"`{n}`" not in usage]
    assert not missing, f"docs/usage.md is missing these subcommands: {missing}"
