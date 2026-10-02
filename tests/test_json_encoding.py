"""linc-cli-kit#3: typed JSON encoding and strict (allow_nan=False) output.

`default=str` turned dataclasses, pydantic models and sets into repr strings, bytes into
"b'...'" literals and datetimes into space-separated strings, and emitted NaN/Infinity,
which is not JSON at all (jq and strict parsers reject the whole payload).
"""

import base64
import dataclasses
import datetime
import decimal
import io
import json
import math
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pydantic
import pytest

from linc_cli_kit.output import emit


def _strict_loads(text: str):
    def reject(const):
        raise ValueError(f"non-standard JSON constant {const}")

    return json.loads(text, parse_constant=reject)


def _run(result, mode="json"):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = emit(result, mode=mode)
    return code, out.getvalue(), err.getvalue()


@dataclasses.dataclass
class _Inner:
    serial: str


@dataclasses.dataclass
class _Rec:
    count: int
    inner: _Inner
    tags: set


class _Model(pydantic.BaseModel):
    name: str
    when: datetime.datetime


def test_dataclass_becomes_an_object_recursively():
    code, out, _ = _run({"rec": _Rec(2, _Inner("emu-1"), {"b", "a"})})
    assert code == 0
    assert _strict_loads(out) == {
        "rec": {"count": 2, "inner": {"serial": "emu-1"}, "tags": ["a", "b"]}
    }


def test_pydantic_model_uses_its_json_dump():
    when = datetime.datetime(2026, 9, 30, 1, 2, 3)
    code, out, _ = _run({"m": _Model(name="x", when=when)})
    assert code == 0
    assert _strict_loads(out) == {"m": {"name": "x", "when": "2026-09-30T01:02:03"}}


def test_set_is_a_sorted_list_even_with_mixed_types():
    code, out, _ = _run({"s": {3, 1, 2}, "f": frozenset({"z", "y"}), "mixed": {1, "a"}})
    assert code == 0
    doc = _strict_loads(out)
    assert doc["s"] == [1, 2, 3] and doc["f"] == ["y", "z"]
    assert sorted(doc["mixed"], key=repr) == doc["mixed"]  # deterministic, by repr


def test_bytes_are_base64():
    code, out, _ = _run({"raw": b"\x00\xffPNG", "ba": bytearray(b"hi")})
    assert code == 0
    doc = _strict_loads(out)
    assert base64.b64decode(doc["raw"]) == b"\x00\xffPNG"
    assert base64.b64decode(doc["ba"]) == b"hi"


def test_dates_and_times_are_iso_8601():
    code, out, _ = _run({
        "dt": datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC),
        "d": datetime.date(2026, 1, 2),
        "t": datetime.time(3, 4, 5),
    })
    assert code == 0
    assert _strict_loads(out) == {
        "dt": "2026-01-01T12:00:00+00:00", "d": "2026-01-02", "t": "03:04:05",
    }


def test_other_types_still_fall_back_to_str():
    code, out, _ = _run({"p": Path("/tmp/x"), "n": decimal.Decimal("1.5")})
    assert code == 0
    assert _strict_loads(out) == {"p": "/tmp/x", "n": "1.5"}


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_float_is_an_envelope_not_invalid_json(bad):
    code, out, err = _run({"score": bad})
    assert code == 1
    assert out == ""
    env = _strict_loads(err)["error"]
    assert env["code"] == "NON_JSON_VALUE"


def test_non_finite_float_mid_stream_is_an_envelope():
    def records():
        yield {"i": 1}
        yield {"i": math.nan}

    code, out, err = _run(records(), mode="jsonl")
    assert code == 1
    assert [_strict_loads(line) for line in out.splitlines()] == [{"i": 1}]
    assert _strict_loads(err.strip().splitlines()[-1])["error"]["code"] == "NON_JSON_VALUE"


def test_error_envelope_details_are_strict_json():
    """The failure path must never itself emit NaN or a repr: details are tool data."""
    code, out, err = _run({"error": "device offline", "v": math.nan, "tags": {"b", "a"}})
    assert code == 1 and out == ""
    env = _strict_loads(err)["error"]
    assert env["details"]["tags"] == ["a", "b"]
    assert env["details"]["v"] == "nan"


def test_human_mode_still_renders_non_finite():
    code, out, _ = _run({"score": math.nan}, mode="human")
    assert code == 0 and "NaN" in out


def test_parity_execution_consistency_uses_the_same_encoder():
    """Check 7 compares the CLI's output to a direct call; both sides must encode alike, or
    every datetime-returning tool fails parity."""
    import click
    from mcp.server.fastmcp import FastMCP

    from linc_cli_kit import install_globals, mount_tools
    from linc_cli_kit.testing import assert_cli_parity
    from linc_cli_kit.transport import LocalTransport

    server = FastMCP("dt")

    @server.tool()
    def dt_when(device: str) -> dict:
        """When."""
        return {"device": device, "at": datetime.datetime(2026, 1, 1, 0, 0), "tags": {"x"}}

    @click.group()
    def cli() -> None:
        """dt"""

    install_globals(cli)
    mount_tools(cli, server, prefix="dt_", transport=LocalTransport())
    assert assert_cli_parity(cli, server, prefix="dt_", samples={"dt_when": {"device": "X"}})


def test_duck_typed_model_dump_is_not_trusted():
    """Only a real pydantic model is dumped. A MagicMock (adopter tests put them in error
    details) answers every attribute with a callable that returns another mock, so trusting
    any `model_dump` attribute recursed without end and hung linc-crucible's suite."""
    from unittest.mock import MagicMock

    code, out, _ = _run({"m": MagicMock(name="scorer")})
    assert code == 0
    assert "scorer" in _strict_loads(out)["m"]


def test_non_string_keys_are_an_envelope_too():
    code, out, err = _run({("a", 1): "tuple key"})
    assert code == 1 and out == ""
    assert _strict_loads(err)["error"]["code"] == "NON_JSON_VALUE"


def test_envelope_keeps_good_details_when_one_nested_value_is_nan():
    """Review of #9: a NaN inside a dataclass in the details dropped ALL details."""

    @dataclasses.dataclass
    class _Sample:
        v: float

    code, _, err = _run({"error": "x", "s": _Sample(math.nan), "k": 1, ("t", 1): "tuple key"})
    assert code == 1
    details = _strict_loads(err)["error"]["details"]
    assert details["k"] == 1 and details["s"] == {"v": "nan"}
    assert details["('t', 1)"] == "tuple key"


def test_self_referential_details_never_raise():
    """Review of #9: emit is documented as never raising; a cyclic details dict escaped
    as RecursionError."""
    loop: dict = {}
    loop["self"] = loop
    code, out, err = _run({"error": "x", "d": loop})
    assert code == 1 and out == ""
    assert _strict_loads(err)["error"]["message"] == "x"


def test_details_with_hostile_values_and_nan_keys_never_raise():
    """Review of #9, round 2: a value whose __str__ raises must not escape emit(), and a
    NaN dict key must not wipe the good fields."""

    class _Hostile:
        def __str__(self):
            raise RuntimeError("boom")

    code, out, err = _run({"error": "x", "bad": _Hostile(), "good": 2, math.nan: 1})
    assert code == 1 and out == ""
    details = _strict_loads(err)["error"]["details"]
    assert details["good"] == 2 and details["nan"] == 1
    assert details["bad"].startswith("<unencodable")


def test_hostile_value_in_a_success_payload_is_an_envelope():
    class _Hostile:
        def __str__(self):
            raise RuntimeError("boom")

    code, out, err = _run({"ok": _Hostile()})
    assert code == 1 and out == ""
    assert _strict_loads(err)["error"]["code"] == "NON_JSON_VALUE"


def test_hostile_error_value_is_still_an_envelope():
    """Review of #9, round 3: classify() str()s the tool's own error value."""

    class _Hostile:
        def __str__(self):
            raise RuntimeError("boom")

    code, out, err = _run({"error": _Hostile()})
    assert code == 1 and out == ""
    assert "_Hostile" in _strict_loads(err)["error"]["message"]  # repr() fallback


def test_nan_key_does_not_overwrite_a_real_key():
    code, _, err = _run({"error": "x", math.nan: 1, "nan": 2})
    assert code == 1
    details = _strict_loads(err)["error"]["details"]
    assert sorted(details.values()) == [1, 2]


def test_keys_that_json_itself_stringifies_do_not_collide():
    """Review of #9, round 4: json.dumps spells 1/None/True keys as "1"/"null"/"true"."""
    code, _, err = _run({"error": "x", 2: "a", "2": "b", None: 1, "null": 2, True: 3, "true": 4})
    assert code == 1
    line = err.strip().splitlines()[-1]
    pairs = json.loads(line, object_pairs_hook=lambda kv: kv)[0][1]  # [("error", {...})]
    details = dict(pairs)["details"]
    keys = [k for k, _ in details]
    assert len(keys) == len(set(keys)) == 6, keys  # (True == 1 in Python, so no 1 key)
