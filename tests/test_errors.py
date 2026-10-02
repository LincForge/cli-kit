from linc_cli_kit.errors import Outcome, ToolError, UsageError, classify, classify_exception


def test_success_passthrough():
    out = classify({"ok": True})
    assert out == Outcome(0, {"ok": True}, None)


def test_none_becomes_empty_object():
    assert classify(None).payload == {}


def test_error_dict_is_exit_1_internal():
    out = classify({"error": "device offline"})
    assert out.exit_code == 1
    assert out.payload is None
    assert out.envelope["error"]["message"] == "device offline"
    assert out.envelope["error"]["category"] == "internal"
    assert out.envelope["error"]["retryable"] is False
    assert out.envelope["error"]["code"] == "TOOL_ERROR"


def test_validation_error_dict_is_exit_2():
    out = classify({"error": "bad input", "validation_error": True})
    assert out.exit_code == 2
    assert out.envelope["error"]["category"] == "validation"


def test_tool_error_exception_carries_fields():
    exc = ToolError(
        "no daemon",
        code="DAEMON_DOWN",
        hint="run nerve daemon",
        category="unavailable",
        retryable=True,
    )
    out = classify_exception(exc)
    assert out.exit_code == 1
    assert out.envelope["error"] == {
        "code": "DAEMON_DOWN", "message": "no daemon", "hint": "run nerve daemon",
        "category": "unavailable", "retryable": True, "details": {},
    }


def test_usage_error_is_exit_2():
    assert classify_exception(UsageError("missing --device")).exit_code == 2


def test_generic_exception_code_is_class_name_upper_snake():
    out = classify_exception(FileNotFoundError("x.png"))
    assert out.exit_code == 1
    assert out.envelope["error"]["code"] == "FILE_NOT_FOUND_ERROR"
    assert out.envelope["error"]["message"] == "x.png"


def test_falsy_error_value_is_success():
    """Tools that always carry an "error" key and null it on success are not failures."""
    assert classify({"error": None, "ok": 1}) == Outcome(0, {"error": None, "ok": 1}, None)
    assert classify({"error": ""}) == Outcome(0, {"error": ""}, None)
    assert classify({"error": "real"}).exit_code == 1
