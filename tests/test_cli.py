import pytest

from scout.cli import main


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (["scan"], "scan: sources are not wired up yet"),
        (["check"], "check: atlas lookup is not wired up yet"),
        (["file"], "file: issue filing is not wired up yet"),
        (["run"], "run: pipeline is not wired up yet"),
    ],
)
def test_stub_commands(capsys, command, message):
    assert main(command) == 1
    assert message in capsys.readouterr().out


def test_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        main([])


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("scan", "check", "file", "run"):
        assert name in out
