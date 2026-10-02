"""linc-cli-kit#5: `import linc_cli_kit` must not drag in test-only or network modules.

Every adopter CLI invocation (`--help` included) pays for the kit's import graph, so the
parity suite (click.testing, unittest.mock -> asyncio), httpx and anyio load on use only.
Run in a fresh interpreter: this pytest process has already imported all of them.
"""

import json
import subprocess
import sys

_HEAVY = ("httpx", "anyio", "asyncio", "unittest.mock", "click.testing", "linc_cli_kit.testing")


def _loaded_after(code: str) -> list[str]:
    probe = (
        f"{code}\nimport sys, json\n"
        f"print(json.dumps([m for m in {_HEAVY!r} if m in sys.modules]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_bare_import_is_lazy():
    assert _loaded_after("import linc_cli_kit") == []


def test_building_a_cli_is_lazy():
    """install_globals + LocalTransport construction (a CLI's --help path) stay light."""
    code = (
        "import click\n"
        "from linc_cli_kit import install_globals, LocalTransport, HttpDaemonTransport\n"
        "g = click.group()(lambda: None)\n"
        "install_globals(g)\n"
        "LocalTransport(); HttpDaemonTransport('toy', 'http://127.0.0.1:1')"
    )
    assert _loaded_after(code) == []


def test_public_surface_still_resolves():
    import linc_cli_kit
    from linc_cli_kit import testing

    assert linc_cli_kit.assert_cli_parity is testing.assert_cli_parity
    assert linc_cli_kit.ParityReport is testing.ParityReport
    for name in linc_cli_kit.__all__:
        assert getattr(linc_cli_kit, name) is not None, name
    assert set(linc_cli_kit.__all__) <= set(dir(linc_cli_kit))
