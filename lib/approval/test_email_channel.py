"""Effect tests for lib.approval.email_channel.

Fully mock-based: `subprocess.run` and `shutil.which` are always patched, so no
real `gws` process runs and no network call is made. Every test asserts an
observable effect — the exact gws argv, the env gate, RE:-only filtering, the
explicit --to on replies, and fail-open behavior.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from lib.approval import email_channel as ec


def triage_contract_response(shape: str, fault: str, row: dict, max_msgs: int = 2):
    """Mirror the installed CLI's object output and its documented array form.

    The real read-only output has messages, query, resultSizeEstimate; each row
    has date, from, id, subject. All values below are synthetic.
    """
    rows = [dict(row)]
    if fault == "invalid_row":
        rows[0]["id"] = []
    if fault == "invalid_date":
        rows[0]["date"] = []
    if fault == "full_page":
        rows.extend({**row, "id": f"synthetic-{index}"}
                    for index in range(1, max_msgs))
    if shape == "array":
        return rows
    if shape == "real_object":
        result = {"messages": rows, "query": "synthetic-query",
                  "resultSizeEstimate": len(rows)}
    elif shape == "results":
        result = {"results": rows, "totalCount": len(rows)}
    else:
        raise AssertionError(shape)
    if fault == "continuation":
        result["nextPageToken"] = "synthetic-next"
    if fault == "estimate":
        result["resultSizeEstimate"] = len(rows) + 1
    if fault == "invalid_envelope":
        result.pop("messages" if shape == "real_object" else "results")
    if fault == "invalid_token":
        result["nextPageToken"] = 42
    return result


def test_correction_query_failure_holds_tokened_approval(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", "/private/tmp")
    monkeypatch.setattr(ec, "_gws", lambda: "/synthetic/gws")
    monkeypatch.setattr(ec, "_fetch_auth_results", lambda _mid: [
        "mx.google.com; dmarc=pass header.from=example.com"])

    def fake(args, timeout=45):
        if "+triage" in args:
            query = args[args.index("--query") + 1]
            if "subject:" in query:
                return None
            return {"complete": True, "messages": [{
                "id": "m1", "subject": "RE: READY TOKEN", "from": "op@example.com"}]}
        if "+read" in args:
            return {"body_text": "APPROVE"}
        raise AssertionError(args)

    monkeypatch.setattr(ec, "gws_json", fake)
    result = ec.read_replies_with_status(
        ["TOKEN"], correction_queries=["READY"], require_enabled=False)
    assert result.kind == "transport_error"
    assert result.texts == []


@pytest.mark.parametrize("shape,fault", (
    *((shape, fault) for shape in ("real_object", "array", "results")
      for fault in ("none", "full_page", "invalid_row", "invalid_date")),
    *((shape, fault) for shape in ("real_object", "results")
      for fault in ("continuation", "estimate", "invalid_envelope", "invalid_token")),
))
def test_triage_cli_shape_and_completeness_contract(shape, fault):
    row = {"date": "2026-11-18T10:00:00Z", "from": "Op <op@example.com>",
           "id": "synthetic-one", "subject": "RE: synthetic approval"}
    response = triage_contract_response(shape, fault, row)
    rows, detail = ec._complete_triage_rows(response, 2)
    if fault == "none":
        assert rows == [row] and detail == ""
    else:
        assert rows is None
        assert detail == ("gws_triage_invalid_response" if fault.startswith("invalid")
                          else "gws_triage_truncated")


@pytest.mark.parametrize("colour", (
    "#0000", "#fff0", "hsla(0,0%,0%,0)", "hwb(0 0% 0% / 0)",
    "lab(0 0 0 / 0)", "lch(0 0 0 / 0)", "color(srgb 0 0 0 / 0)",
    "#333333", "rgb(34,34,34)", "black", "hsl(0,0%,0%)",
))
def test_html_colour_policy_rejects_unsafe_foreground_or_background(colour):
    for property_name in ("color", "background"):
        html = (f"<body><p style='{property_name}:{colour}'>APPROVE</p>"
                "<blockquote>quoted token</blockquote></body>")
        body, _ = ec._extract_authored_html(html)
        if property_name == "color" and colour in {"#333333", "rgb(34,34,34)", "black"}:
            assert body == "APPROVE"
        else:
            assert body == ""


@pytest.mark.parametrize("style", (
    "color:#333;background:#333", "color:rgb(34,34,34);background:#222222",
    "color:black;background:rgb(0,0,0)", "color:white;background:#fff",
    "font-size:0", "margin-left:-9999px", "opacity:0", "color:var(--ink)",
    "color:#222!important", "background-image:url(x)",
))
def test_html_unsafe_style_is_unreadable(style):
    assert ec._extract_authored_html(
        f"<body><p style='{style}'>APPROVE</p></body>")[0] == ""


def test_html_normal_mail_styles_and_terminal_quote_are_readable():
    html = ("<body style='background:#fff'><p style='color:rgb(34,34,34);"
            "font-family:Arial;font-size:14px;margin:0'>APPROVE</p>"
            "<blockquote>quoted token</blockquote></body>")
    assert ec._extract_authored_html(html)[0] == "APPROVE"


@pytest.mark.parametrize("html", (
    "I withdraw<html><body><p>APPROVE</p></body></html>",
    "<html><body><p>APPROVE</p></body>I withdraw</html>",
    "<html><body><p>APPROVE</p></body></html>I withdraw",
    "<p>APPROVE</p>I withdraw<blockquote>quoted</blockquote>",
))
def test_html_text_outside_accounted_container_is_unreadable(html):
    assert ec._extract_authored_html(html)[0] == ""


@pytest.mark.parametrize("tag", ("body", "table", "td"))
def test_html_background_attribute_is_unreadable(tag):
    html = f"<{tag} background='image.png'>APPROVE</{tag}>"
    assert ec._extract_authored_html(html)[0] == ""


@pytest.mark.parametrize("attribute", (
    "bgcolor='#fff'", "color='#222'", "text='#222'", "link='#222'",
    "vlink='#222'", "alink='#222'", "face='Arial'", "size='14'",
    "hidden", "aria-hidden='false'", "class='ordinary'", "id='ordinary'",
    "data-style='display:block'", "stylex='color:#222'", "dir='rtl'",
))
def test_html_unaccounted_presentational_attribute_is_unreadable(attribute):
    assert ec._extract_authored_html(f"<body><p {attribute}>APPROVE</p></body>")[0] == ""


@pytest.mark.parametrize("size", ("50%", ".5em", "smaller", "90%", ".9em"))
def test_nested_relative_font_sizes_cannot_hide_approval(size):
    html = "<body>" + f"<span style='font-size:{size}'>" * 20
    html += "APPROVE" + "</span>" * 20 + "</body>"
    assert ec._extract_authored_html(html)[0] == ""


@pytest.mark.parametrize("style", (
    "font-size:7px", "font-size:5pt", "font-size:60%",
    "width:99999px", "height:99999px", "max-width:99999px",
    "max-height:99999px", "min-width:99999px", "min-height:99999px",
    "top:99999px", "left:99999px", "right:99999px", "bottom:99999px",
    "inset:99999px", "text-indent:99999px", "letter-spacing:99999px",
    "word-spacing:99999px", "line-height:99999px", "transform:translateX(99999px)",
    "translate:99999px", "scale:0", "rotate:180deg", "zoom:0",
    "float:right", "position:absolute", "position:relative;left:99999px",
    "overflow:hidden", "overflow-x:hidden", "overflow-y:hidden",
    "margin:calc(1px)", "padding:var(--space)",
))
def test_unbounded_or_conditional_layout_is_unreadable(style):
    assert ec._extract_authored_html(f"<body><p style='{style}'>APPROVE</p></body>")[0] == ""


@pytest.mark.parametrize("property_name", (
    "margin", "padding", "margin-left", "margin-right", "margin-top",
    "margin-bottom", "padding-left", "padding-right", "padding-top",
    "padding-bottom",
))
@pytest.mark.parametrize("value", ("99999px", "101px", "76pt", "9em", "11%", "-1px"))
def test_every_margin_and_padding_side_is_bounded(property_name, value):
    assert ec._extract_authored_html(
        f"<body><p style='{property_name}:{value}'>APPROVE</p></body>")[0] == ""


@pytest.mark.parametrize("property_name", ("margin", "padding"))
@pytest.mark.parametrize("value", ("0 99999px", "0 0 99999px", "0 0 0 99999px"))
def test_each_shorthand_side_is_bounded(property_name, value):
    assert ec._extract_authored_html(
        f"<body><p style='{property_name}:{value}'>APPROVE</p></body>")[0] == ""


@pytest.mark.parametrize("html", (
    "<details><p>APPROVE</p></details>", "<dialog>APPROVE</dialog>",
    "<menu>APPROVE</menu>", "<slot>APPROVE</slot>",
    "<noembed>APPROVE</noembed>", "<map><area>APPROVE</map>",
    "<object>APPROVE</object>", "<picture>APPROVE</picture>",
    "<audio>APPROVE</audio>", "<video>APPROVE</video>",
    "<frameset>APPROVE</frameset>",
    '<p style="font-size:14px" style="font-size:0">APPROVE</p>',
    '<p class="ordinary" class="gmail_quote">APPROVE</p>',
    '<p hidden="" hidden="false">APPROVE</p>',
    '<p id="ordinary" id="divRplyFwdMsg">APPROVE</p>',
))
def test_ambiguous_visibility_and_duplicate_attributes_are_unreadable(html):
    assert ec._extract_authored_html(f"<body>{html}</body>")[0] == ""


def test_open_details_summary_and_regular_mail_sizes_are_readable():
    html = ("<body><details open><summary>APPROVE</summary></details>"
            "<p style='font-size:14px;margin:8px;padding:4px'>ordinary</p></body>")
    assert ec._extract_authored_html(html)[0].startswith("APPROVE")


@pytest.mark.parametrize("size", ("14px", "small", "medium", "1em", "100%"))
def test_normal_mail_font_sizes_remain_readable(size):
    assert ec._extract_authored_html(
        f"<body><p style='font-size:{size}'>APPROVE</p></body>")[0] == "APPROVE"


@pytest.mark.parametrize("tail", (
    "From: Swarm <swarm@example.net>\nI withdraw approval",
    "From: Swarm <swarm@example.net>\n> old text\nI withdraw approval",
    "On Tuesday Example wrote:\n> old text\nI withdraw approval",
))
def test_plain_header_with_later_authored_text_is_unreadable(tail):
    body, safe = ec._authored_plain_checked("APPROVE\n" + tail)
    assert "I withdraw approval" in body or not safe
    assert body != "APPROVE" or not safe


def test_unproven_header_and_signature_are_authored():
    assert ec._authored_plain_checked("APPROVE\nFrom: Swarm <swarm@example.net>\n> old")[1] is False
    assert ec._authored_plain_checked("APPROVE\n-- \nSignature") == (
        "APPROVE\n--\nSignature", True)


@pytest.mark.parametrize("separator", ("-----Original Message-----", "Begin forwarded message:"))
def test_header_boundary_requires_quoted_tail(separator):
    headers = "From: Example\nSent: Tuesday\nTo: Operator\nSubject: Request"
    assert ec._authored_plain_checked(f"APPROVE\n{separator}\n{headers}\n> old")[1] is False
    assert ec._authored_plain_checked(f"APPROVE\n{separator}\n{headers}\nI withdraw") == (
        "APPROVE", False)
    assert ec._authored_plain_checked(f"APPROVE\n{headers}\n> old")[1] is False


def test_matching_sent_header_and_quoted_body_prove_boundary():
    sent = ec.SentRequest("old", "Swarm <swarm@example.net>",
                          "Operator <op@example.com>", "Request",
                          "Tue, 17 Nov 2026 12:00:00 +0000")
    headers = ("From: Swarm <swarm@example.net>\n"
               "Sent: Tue, 17 Nov 2026 12:00:00 +0000\n"
               "To: Operator <op@example.com>\nSubject: Request")
    assert ec._authored_plain_checked(
        f"APPROVE\n-----Original Message-----\n{headers}\n> old",
        sent_request=sent) == ("APPROVE", True)
    assert ec._authored_plain_checked(
        f"APPROVE\n-----Original Message-----\n{headers}\nI withdraw",
        sent_request=sent)[1] is False


@pytest.mark.parametrize("tail", (
    "I withdraw approval", "One\nTwo\nThree\nFour\nFive",
    "This line is deliberately more than eighty characters long and makes the signature boundary unsafe.",
    "Manager, approve later", "Example v1.2.3", "Review PR #7",
))
def test_signature_tail_must_be_short_and_benign(tail):
    assert ec._authored_plain_checked("APPROVE\n-- \n" + tail)[0].endswith(tail)


def test_benign_signature_after_affirmative_stays_authored():
    assert ec._authored_plain_checked("APPROVE\n-- \nExample Name\nEngineer") == (
        "APPROVE\n--\nExample Name\nEngineer", True)


@pytest.mark.parametrize("tail", (
    "Subject: I rescind my consent", "From: I rescind my consent",
    "Date: I rescind my consent", "To: I rescind my consent",
    "Cc: I rescind my consent", "Sent: I rescind my consent",
    "-- \nI rescind my consent", "--\nI rescind my consent",
    "-----Original Message-----\nI rescind my consent",
    "From: Example\nSent: Tuesday\nTo: Operator\nSubject: Request\nI rescind my consent",
))
def test_unproven_quote_shapes_preserve_authored_withdrawal(tail):
    body, safe = ec._authored_plain_checked("approve\n" + tail)
    assert not safe or "I rescind my consent" in body


@pytest.mark.parametrize("quote", (
    "On Tuesday Example wrote:\n> old request",
    "Le mardi Exemple a écrit :\n> ancienne demande",
    "El martes Ejemplo escribió:\n> solicitud anterior",
    "-----Original Message-----\nFrom: Example\nSent: Tuesday\nTo: Operator\nSubject: Request\n> old request",
))
def test_proven_bottom_quote_is_removed(quote):
    if quote.startswith(("-----", "On ", "Le ", "El ")):
        assert ec._authored_plain_checked("approve\n" + quote)[1] is False


@pytest.mark.parametrize("font", (
    "Wingdings", "Arial, Webdings", "Symbol", "Emoji", "Unknown Font",
))
def test_unsafe_font_family_and_font_face_are_unreadable(font):
    assert ec._extract_authored_html(
        f"<body><p style='font-family:{font}'>APPROVE</p></body>")[0] == ""
    assert ec._extract_authored_html(
        f"<body><font face='{font}'>APPROVE</font></body>")[0] == ""


@pytest.mark.parametrize("markup", (
    "<style>@font-face { font-family: Custom; src:url(x) }</style>",
    "<p style='font:14px Wingdings'>APPROVE</p>",
    "<p style='font-variant:small-caps'>APPROVE</p>",
    "<p style='font-feature-settings:normal'>APPROVE</p>",
    "<p style='font-synthesis:none'>APPROVE</p>",
    "<p style='unicode-range:U+0-FFFF'>APPROVE</p>",
    "<p style='text-transform:uppercase'>APPROVE</p>",
    "<p style='direction:rtl'>APPROVE</p>",
    "<p style='unicode-bidi:bidi-override'>APPROVE</p>",
    "<p style='writing-mode:vertical-rl'>APPROVE</p>",
    "<p style='text-orientation:sideways'>APPROVE</p>",
))
def test_unsafe_font_and_text_rendering_are_unreadable(markup):
    assert ec._extract_authored_html(f"<body>{markup}</body>")[0] == ""


@pytest.mark.parametrize("font", (
    "Arial", "'Helvetica Neue', sans-serif", '"Times New Roman", serif',
    "Calibri, Arial", "system-ui, -apple-system", "Menlo, monospace",
))
def test_common_mail_fonts_remain_readable(font):
    escaped_font = font.replace('"', "&quot;")
    assert ec._extract_authored_html(
        f"<body><p style=\"font-family:{escaped_font}\">APPROVE</p></body>")[0] == "APPROVE"


def test_html_bottom_post_after_quote_is_unreadable():
    assert ec._extract_authored_html(
        "<body><blockquote>old</blockquote><p>APPROVE</p></body>")[0] == ""


def _ok(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr="boom"):
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


_AUTH_PASS_FOR_OPERATOR = object()


def _google_pass_for_operator_domain() -> dict:
    import os as _os
    domain = _os.environ.get("OPERATOR_EMAIL", "").rpartition("@")[2].strip().lower()
    value = (f"mx.google.com; dkim=pass header.i=@{domain} header.s=s; "
             f"dmarc=pass (p=NONE) header.from={domain}")
    return {"id": "m1", "payload": {"headers": [
        {"name": "Authentication-Results", "value": value}]}}


@contextmanager
def _mock_inbox(gws_json: Any = None, *, side_effect: Any = None,
                auth: Any = _AUTH_PASS_FOR_OPERATOR) -> Iterator[None]:
    """Patch the inbox path the way production reaches it.

    ``read_replies_with_status`` early-returns ``gws_cli_missing`` when
    ``_gws()`` is falsy, before any mocked ``gws_json``. Tests that only
    patch ``gws_json`` therefore pass on a machine with ``gws`` on PATH and
    fail in CI (ateles#1202). Always stub ``_gws`` present when exercising
    triage/+read behavior.

    ``auth``: by default the Authentication-Results metadata fetch is answered
    with a Google-stamped DMARC/DKIM pass for OPERATOR_EMAIL's domain, so tests
    about sender/body/transport keep testing exactly that. Pass ``auth=None``
    to leave the metadata call to the test's own ``side_effect``.
    """
    if side_effect is not None:
        inner = side_effect
    else:
        def inner(args, timeout=45, _v=gws_json):
            return _v

    def dispatch(args, timeout=45):
        if auth is _AUTH_PASS_FOR_OPERATOR and \
                list(args[:4]) == ["gmail", "users", "messages", "get"]:
            return _google_pass_for_operator_domain()
        return inner(args, timeout=timeout)

    with patch.object(ec, "_gws", return_value="/bin/gws"), \
         patch.object(ec, "gws_json", side_effect=dispatch):
        yield


class TestGate:
    def test_email_enabled_true_only_when_flag_1(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        assert ec.email_enabled() is True
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        assert ec.email_enabled() is False
        monkeypatch.delenv("ATELES_NOTIFY_EMAIL", raising=False)
        assert ec.email_enabled() is False


class TestSendRequest:
    def test_disabled_noops(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        with patch.object(ec.subprocess, "run") as run:
            assert ec.send_request("s", "b") is False
            run.assert_not_called()

    def test_missing_operator_email_returns_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.delenv("OPERATOR_EMAIL", raising=False)
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run") as run:
            assert ec.send_request("s", "b") is False
            run.assert_not_called()

    def test_missing_gws_returns_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value=None), \
             patch.object(ec.subprocess, "run") as run:
            assert ec.send_request("s", "b") is False
            run.assert_not_called()

    def test_sends_with_expected_argv(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_ok()) as run:
            assert ec.send_request("Subj [APPROVE-TOK]", "body text") is True
        argv = run.call_args.args[0]
        assert argv[:3] == ["/bin/gws", "gmail", "+send"]
        assert "--to" in argv and argv[argv.index("--to") + 1] == "op@example.com"
        assert "--subject" in argv and argv[argv.index("--subject") + 1] == "Subj [APPROVE-TOK]"
        assert "--from" in argv and argv[argv.index("--from") + 1] == "swarm@example.com"

    def test_explicit_to_overrides_operator(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_ok()) as run:
            ec.send_request("s", "b", to="other@example.com")
        argv = run.call_args.args[0]
        assert argv[argv.index("--to") + 1] == "other@example.com"

    def test_nonzero_exit_is_fail_open_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_fail()):
            assert ec.send_request("s", "b") is False

    def test_exception_is_fail_open_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", side_effect=OSError("nope")):
            assert ec.send_request("s", "b") is False


class TestReadReplies:
    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        assert ec.read_replies(["TOK"]) == []

    def test_empty_tokens_returns_empty(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        assert ec.read_replies([]) == []

    def test_only_re_subjects_are_read(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        triage = {"complete": True, "messages": [
            {"id": "m1", "subject": "RE: [ATELES] Approve [APPROVE-TOK]",
             "from": "op@example.com"},
            {"id": "m2", "subject": "[ATELES] Approve [APPROVE-TOK]",
             "from": "op@example.com"},  # our own outbound
        ]}

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return triage
            if "+read" in args:
                return {"body_text": "APPROVE"}
            return None

        with _mock_inbox(side_effect=fake_gws_json):
            texts = ec.read_replies(["TOK"])
        # Only the RE: message's body is read; the outbound one is skipped.
        assert len(texts) == 1
        assert "APPROVE" in texts[0] and texts[0].startswith("RE:")

    def test_on_reply_message_callback_fires_with_token_and_id(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        seen = []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            return {"body_text": "SKIP"}

        with _mock_inbox(side_effect=fake_gws_json):
            ec.read_replies(["TOK"], on_reply_message=lambda tok, mid: seen.append((tok, mid)))
        assert seen == [("TOK", "m1")]

    def test_triage_failure_is_fail_open_empty(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        with _mock_inbox(None):
            assert ec.read_replies(["TOK"]) == []

    def test_multipart_requires_matching_authored_text(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            return {"body_text": "approve v0.20.0", "body_html": "<p>APPROVE v0.20.0</p>"}

        with _mock_inbox(side_effect=fake_gws_json):
            texts = ec.read_replies(["TOK"])
        assert "approve v0.20.0" in texts[0]

    @pytest.mark.parametrize("plain,html", (
        ("APPROVE", "<p>hold</p>"),
        ("hold", "<p>APPROVE</p>"),
        ("APPROVE", "<p hidden>APPROVE</p>"),
        ("APPROVE", {"unexpected": "shape"}),
    ))
    def test_multipart_disagreement_is_unreadable(self, monkeypatch, plain, html):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            return {"body_text": plain, "body_html": html}

        with _mock_inbox(side_effect=fake_gws_json):
            outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "transport_error"

    @pytest.mark.parametrize("html", ("", " \n ", "<p></p>"))
    def test_present_empty_html_disagrees_with_plain_approval(self, monkeypatch, html):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            return {"body_text": "APPROVE", "body_html": html}

        with _mock_inbox(side_effect=fake_gws_json):
            outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "transport_error"
        assert outcome.texts == []
        assert [reply.message_id for reply in outcome.unreadable_replies] == ["m1"]
        assert outcome.detail == "reply_quote_boundary_unknown"
        assert outcome.texts == []
        assert len(outcome.unreadable_replies) == 1

    @pytest.mark.parametrize("plain", ("", " \n "))
    def test_present_empty_plain_holds_html_approval(self, monkeypatch, plain):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                     "from": "op@example.com"}]}
            return {"body_text": plain, "body_html": "<p>APPROVE</p>"}

        with _mock_inbox(side_effect=fake):
            outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "transport_error"
        assert outcome.texts == []
        assert [r.message_id for r in outcome.unreadable_replies] == ["m1"]

    def test_falls_back_to_body_html_when_no_plaintext(self, monkeypatch):
        # An HTML-only reply must not read as an empty body and silently drop the
        # approval — the ateles#286 live-release failure mode. Tags are stripped
        # so the verdict actually PARSES, not merely so the body is non-empty
        # (Loxia #298: `<p>approve` would leave parse_verdict returning None).
        from lib.approval import parse_verdict
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            return {"body_html": "<div dir=\"ltr\">approve v0.20.0</div>"}

        with _mock_inbox(side_effect=fake_gws_json):
            texts = ec.read_replies(["TOK"])
        assert "approve v0.20.0" in texts[0]
        # The real end-to-end guarantee: this HTML-only reply registers as APPROVE.
        assert parse_verdict(texts[0], "v0.20.0") is True


class TestReplyInThread:
    def test_passes_explicit_to_operator(self, monkeypatch, tmp_path):
        # The known-quirk guard: +reply must carry --to OPERATOR_EMAIL explicitly.
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_ok()) as run:
            assert ec.reply_in_thread("m1", "done", cwd=str(tmp_path)) is True
        argv = run.call_args.args[0]
        assert argv[:3] == ["/bin/gws", "gmail", "+reply"]
        assert "--message-id" in argv and argv[argv.index("--message-id") + 1] == "m1"
        assert "--to" in argv and argv[argv.index("--to") + 1] == "op@example.com"

    def test_disabled_noops(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        with patch.object(ec.subprocess, "run") as run:
            assert ec.reply_in_thread("m1", "b") is False
            run.assert_not_called()

    def test_missing_message_id_returns_false(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"):
            assert ec.reply_in_thread("", "b") is False

    def test_failure_is_fail_open_false(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_fail()):
            assert ec.reply_in_thread("m1", "b", cwd=str(tmp_path)) is False


class TestGwsJson:
    def test_strips_banner_before_json(self, monkeypatch):
        # gws_json makes no call without a configured swarm mailbox, which
        # needs an operator address to compare against.
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        out = "keyring banner line\nWARNING: something\n{\"ok\": true}"
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_ok(out)):
            assert ec.gws_json(["gmail", "+triage"]) == {"ok": True}

    def test_bracketed_banner_before_array(self, monkeypatch):
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        out = '[gws keyring]\n[{"id":"synthetic-one"}]'
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_ok(out)):
            assert ec.gws_json(["gmail", "+triage"]) == [{"id": "synthetic-one"}]

    def test_missing_gws_returns_none(self):
        with patch.object(ec.shutil, "which", return_value=None):
            assert ec.gws_json(["gmail", "+triage"]) is None

    def test_nonzero_exit_returns_none(self):
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run", return_value=_fail()):
            assert ec.gws_json(["gmail", "+triage"]) is None


class TestStripHtml:
    def test_verb_leads_line_after_strip(self):
        # The core requirement: the verb must lead its line so parse_verdict sees it.
        assert ec._strip_html("<p>approve v0.20.0</p>") == "approve v0.20.0"
        assert ec._strip_html('<div dir="ltr">approve v0.20.0</div>') == "approve v0.20.0"

    def test_block_boundaries_become_newlines(self):
        out = ec._strip_html("<p>approve v0.20.0</p><p>thanks</p>")
        assert out.splitlines()[0] == "approve v0.20.0"

    def test_scripts_and_styles_dropped(self):
        html = "<style>.x{}</style><p>approve v0.20.0</p><script>x()</script>"
        assert ec._strip_html(html).strip() == "approve v0.20.0"

    def test_entities_unescaped(self):
        assert "&" in ec._strip_html("<p>a &amp; b</p>")

    def test_empty_and_none_safe(self):
        assert ec._strip_html("") == ""


class TestSenderVerification:
    """A valid token must NOT be sufficient to approve.

    The Gmail query `read_replies` issues is a full-text mailbox search for the
    token (`newer_than:3d <token>`), so ANY message carrying the token string
    matches — including one from a third party. Before this guard, the only
    filter was `subject.startswith("RE:")`, which anyone replying satisfies.
    Possession of the token was therefore sufficient to approve: authentication
    with no authorization.

    The token is not a secret that stays secret. It rides in the SUBJECT LINE of
    an email, so it is present in every forward, every auto-reply, every "on
    vacation" bounce, and any thread the operator CCs someone on. Treating it as
    proof of identity conflates "saw the request" with "is the operator".
    """

    @staticmethod
    def _triage(from_addr: str) -> dict:
        return {"complete": True, "messages": [
            {"id": "m1", "subject": "RE: [ATELES] Approve [APPROVE-TOK]",
             "from": from_addr},
        ]}

    def _run(self, monkeypatch, from_addr: str, operator: str = "op@example.com"):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", operator)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage(from_addr)
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json):
            return ec.read_replies(["TOK"])

    # ── The core requirement ────────────────────────────────────────────────

    def test_reply_from_a_stranger_with_a_valid_token_does_not_approve(
            self, monkeypatch):
        """THE deliberate deliverable: valid token + wrong sender ⇒ no approval.

        This is the attack. An attacker who learns the token — by being CC'd, by
        receiving a forward, by compromising any mailbox the thread touched —
        replies "APPROVE". Before the fix this returned the body and the caller
        approved a payment or a release.
        """
        assert self._run(monkeypatch, "attacker@evil.example") == []

    def test_reply_from_the_operator_still_approves(self, monkeypatch):
        """The guard must not break the legitimate path — otherwise it is just
        an outage, and the next person to debug it will remove it."""
        texts = self._run(monkeypatch, "op@example.com")
        assert len(texts) == 1
        assert "APPROVE" in texts[0]

    # ── Fail closed on absent / malformed identity ──────────────────────────

    def test_absent_sender_does_not_approve(self, monkeypatch):
        """An unverifiable sender must not approve. `gws` omitting the field, or
        a triage backend that never populated it, must take the RESTRICTIVE
        branch — the alternative is that a tooling change silently reopens the
        hole."""
        assert self._run(monkeypatch, "") == []

    def test_malformed_sender_does_not_approve(self, monkeypatch):
        assert self._run(monkeypatch, "not-an-address") == []

    def test_unset_operator_email_does_not_approve(self, monkeypatch):
        """No configured principal ⇒ nothing can be authorized. Fail closed on
        the field that carries the safety meaning: if we cannot say who the
        operator IS, we cannot say a reply came from them."""
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.delenv("OPERATOR_EMAIL", raising=False)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage("op@example.com")
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json):
            assert ec.read_replies(["TOK"]) == []

    # ── Address-form handling (must not become a bypass) ────────────────────

    def test_display_name_form_from_the_operator_approves(self, monkeypatch):
        """Real Gmail returns `From: Mark H <op@example.com>`. Parsing must
        extract the address, or the guard rejects every genuine reply and gets
        reverted."""
        assert len(self._run(monkeypatch, "Mark H <op@example.com>")) == 1

    def test_case_and_whitespace_differences_still_approve(self, monkeypatch):
        assert len(self._run(monkeypatch, "  <OP@Example.COM>  ")) == 1

    def test_lookalike_domain_does_not_approve(self, monkeypatch):
        """Substring matching would accept this. The comparison must be on the
        parsed address, whole and exact."""
        assert self._run(monkeypatch, "op@example.com.evil.example") == []

    def test_operator_address_in_display_name_does_not_approve(
            self, monkeypatch):
        """The classic spoof: put the operator's address in the DISPLAY NAME so
        a naive `operator in from_field` check passes while the real envelope
        address is the attacker's."""
        assert self._run(
            monkeypatch, "op@example.com <attacker@evil.example>") == []

    def test_callback_does_not_fire_for_an_unverified_sender(self, monkeypatch):
        """The callback persists which message to reply to. Firing it for a
        stranger's message would point the in-thread confirmation at the
        attacker — leaking that the request exists and what it is for."""
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        seen = []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage("attacker@evil.example")
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json):
            ec.read_replies(["TOK"],
                            on_reply_message=lambda t, m: seen.append((t, m)))
        assert seen == []

    def test_body_is_never_fetched_for_an_unverified_sender(self, monkeypatch):
        """Rejection happens BEFORE the `+read`. A sender we will not trust
        should not cost us a round trip, and the body should never enter the
        process."""
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        reads = []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage("attacker@evil.example")
            reads.append(args)
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json):
            ec.read_replies(["TOK"])
        assert reads == []

    def test_malformed_operator_email_authorizes_nobody(self, monkeypatch):
        """A MISCONFIGURED principal must not become a usable one.

        `parseaddr` is lenient: it returns a bare word like "not-an-address"
        unchanged rather than rejecting it. Without structural validation, an
        OPERATOR_EMAIL typo would become a principal that a sender could match
        exactly — a configuration error silently turning into an approval path.
        This pins the structural check; equality alone does not cover it,
        because here the two sides ARE equal.
        """
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "not-an-address")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return self._triage("not-an-address")
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json):
            assert ec.read_replies(["TOK"]) == []


class TestReadRepliesWithStatus:
    """ateles#1178: statusful read distinguishes empty-ok from transport failure."""

    def test_read_replies_with_status_ok_empty_texts(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
        with _mock_inbox({"complete": True, "messages": []}):
            outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "ok"
        assert outcome.texts == []

    def test_read_replies_with_status_transport_error_is_not_ok(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
        with _mock_inbox(None):
            outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "transport_error"
        assert outcome.texts == []
        assert "operator@example.com" not in (outcome.detail or "")

    def test_read_replies_with_status_gws_cli_missing(self, monkeypatch):
        # Effect: missing gws is transport_error with a specific detail, not
        # conflated with an empty inbox (the CI false-green ateles#1202 class).
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
        with patch.object(ec, "_gws", return_value=None):
            outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "transport_error"
        assert outcome.detail == "gws_cli_missing"
        assert outcome.texts == []

    def test_read_replies_with_status_disabled(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "0")
        outcome = ec.read_replies_with_status(["TOK"])
        assert outcome.kind == "disabled"

    def test_read_replies_wrapper_preserves_fail_open_list(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "operator@example.com")
        with _mock_inbox(None):
            assert ec.read_replies(["TOK"]) == []


# ── Sender domain authentication + complete-read (PR #1202 security review) ──

import json as _json

_GOOGLE_DMARC_PASS = (
    "mx.google.com;\r\n"
    "       dkim=pass header.i=@example.com header.s=sel header.b=abc;\r\n"
    "       spf=pass (google.com: domain of op@example.com designates 192.0.2.1 "
    "as permitted sender) smtp.mailfrom=op@example.com;\r\n"
    "       dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=example.com"
)


def _metadata(auth_values: list[str]) -> dict:
    headers = [{"name": "From", "value": "op@example.com"}]
    headers += [{"name": "Authentication-Results", "value": v} for v in auth_values]
    return {"id": "m1", "payload": {"headers": headers}}


def _is_metadata_get(args) -> bool:
    return list(args[:4]) == ["gmail", "users", "messages", "get"]


def _metadata_id(args) -> str:
    return _json.loads(args[args.index("--params") + 1])["id"]


class TestSenderDomainAuthentication:
    """A matching From address is necessary but NOT sufficient.

    A From header is caller-supplied text. The verdict counts only when Gmail's
    own receiving server (authserv-id ``mx.google.com``) recorded that the
    operator's domain authorized the message — DMARC pass for that domain, or a
    DKIM pass whose signing domain is that domain. Anything else — header
    missing, failing, unparseable, or stamped by another server — is
    ``unknown``, and unknown is not the operator.
    """

    def _run(self, monkeypatch, auth, *, reads=None):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [{"id": "m1",
                                      "subject": "RE: x [APPROVE-TOK]",
                                      "from": "Op <op@example.com>"}]}
            if _is_metadata_get(args):
                return auth if isinstance(auth, dict) or auth is None \
                    else _metadata(auth)
            if reads is not None:
                reads.append(args)
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json, auth=None):
            return ec.read_replies_with_status(["TOK"])

    # ── yields its verdict ──

    def test_dmarc_pass_for_operator_domain_yields_verdict(self, monkeypatch):
        out = self._run(monkeypatch, [_GOOGLE_DMARC_PASS])
        assert out.kind == "ok"
        assert len(out.texts) == 1 and "APPROVE" in out.texts[0]

    def test_aligned_dkim_pass_without_dmarc_yields_verdict(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=pass header.i=@example.com header.s=s header.b=x"])
        assert out.kind == "ok" and len(out.texts) == 1

    def test_dkim_header_d_aligned_yields_verdict(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=pass header.d=example.com header.s=s"])
        assert out.kind == "ok" and len(out.texts) == 1

    # ── held: no verdict ──

    def test_missing_authentication_results_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [])
        assert out.texts == []

    def test_failing_dmarc_and_dkim_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=fail header.i=@example.com; spf=softfail "
            "smtp.mailfrom=op@example.com; dmarc=fail (p=NONE) header.from=example.com"])
        assert out.texts == []

    def test_unparseable_authentication_results_is_held(self, monkeypatch):
        out = self._run(monkeypatch, ["%%% not an auth header"])
        assert out.texts == []

    def test_empty_authentication_results_value_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [""])
        assert out.texts == []

    def test_non_google_authserv_id_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.evil.example; dmarc=pass header.from=example.com; "
            "dkim=pass header.i=@example.com"])
        assert out.texts == []

    def test_google_lookalike_authserv_id_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com.evil.example; dmarc=pass header.from=example.com"])
        assert out.texts == []

    def test_pass_for_a_different_domain_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=pass header.i=@evil.example; "
            "dmarc=pass header.from=evil.example"])
        assert out.texts == []

    def test_dkim_pass_for_parent_or_sub_domain_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dkim=pass header.i=@mail.example.com"])
        assert out.texts == []

    def test_pass_only_inside_a_comment_is_held(self, monkeypatch):
        out = self._run(monkeypatch, [
            "mx.google.com; dmarc=fail (dmarc=pass header.from=example.com) "
            "header.from=example.com"])
        assert out.texts == []

    def test_only_the_topmost_google_result_counts(self, monkeypatch):
        """Gmail prepends its own result; a lower header carrying the same
        authserv-id did not come from this receipt and must not override it."""
        out = self._run(monkeypatch, [
            "mx.google.com; dmarc=fail header.from=example.com",
            "mx.google.com; dmarc=pass header.from=example.com"])
        assert out.texts == []

    def test_body_is_never_fetched_for_an_unauthenticated_reply(self, monkeypatch):
        reads: list = []
        self._run(monkeypatch, [], reads=reads)
        assert reads == []

    def test_unauthenticated_reply_fires_sender_rejected(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        rejected = []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            if _is_metadata_get(args):
                return _metadata([])
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json, auth=None):
            ec.read_replies_with_status(
                ["TOK"], on_sender_rejected=lambda: rejected.append(1))
        assert rejected == [1]

    def test_unauthenticated_operator_reply_fires_its_own_callback(self, monkeypatch):
        """Address matched, authentication missing: a distinct signal from a
        non-operator sender, and the reply is still ignored."""
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        rejected, unauth = [], []

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            if _is_metadata_get(args):
                return _metadata([])
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json, auth=None):
            out = ec.read_replies_with_status(
                ["TOK"],
                on_sender_rejected=lambda: rejected.append(1),
                on_unauthenticated_operator_reply=lambda: unauth.append(1),
            )
        assert unauth == [1]
        assert rejected == []
        assert out.kind == "ok" and out.texts == []

    def test_unreadable_metadata_response_is_not_ok(self, monkeypatch):
        """We could not read the authentication result at all: that is a read
        failure, never an 'ok, nothing authenticated' and never a pass."""
        for response in (None, {"unexpected": True}):
            outcome = self._run(monkeypatch, response)
            assert outcome.kind == "transport_error"
            assert [reply.message_id for reply in outcome.unreadable_replies] == ["m1"]
            assert outcome.rejected_replies == ()

    @pytest.mark.parametrize("auth", (
        {"id": "m1", "payload": {}},  # Partial Gmail metadata is a failed read.
        {"id": "other", "payload": {"headers": [
            {"name": "Authentication-Results", "value": _GOOGLE_DMARC_PASS}]}},
        {"id": "m1", "payload": {"headers": [
            {"name": "Other", "value": []},
            {"name": "Authentication-Results", "value": _GOOGLE_DMARC_PASS}]}},
    ))
    def test_mismatched_or_invalid_auth_metadata_is_not_ok(self, monkeypatch, auth):
        outcome = self._run(monkeypatch, auth)
        assert outcome.kind == "transport_error"
        assert outcome.texts == []

    def test_fail_open_wrapper_also_drops_unauthenticated(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                      "from": "op@example.com"}]}
            if _is_metadata_get(args):
                return _metadata([])
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json, auth=None):
            assert ec.read_replies(["TOK"]) == []


class TestPartialReadFailsClosed:
    """Any per-message or per-token fetch failure fails the WHOLE read.

    A verdict set assembled from the messages that happened to load is not the
    operator's verdict set: a SKIP in the message that failed would be silently
    replaced by an APPROVE in the one that loaded.
    """

    def _env(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

    def test_one_body_read_fails_other_succeeds_is_not_ok(self, monkeypatch):
        self._env(monkeypatch)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK]", "from": "op@example.com"},
                    {"id": "m2", "subject": "RE: x [APPROVE-TOK]", "from": "op@example.com"},
                ]}
            if "+read" in args and args[args.index("--id") + 1] == "m2":
                return None
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json):
            out = ec.read_replies_with_status(["TOK"])
        assert out.kind == "transport_error"
        assert out.texts == []
        assert [r.message_id for r in out.replies] == ["m1"]
        assert [(r.message_id, r.subject) for r in out.unreadable_replies] == [
            ("m2", "RE: x [APPROVE-TOK]")]

    def test_one_metadata_fetch_fails_other_succeeds_is_not_ok(self, monkeypatch):
        self._env(monkeypatch)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK]", "from": "op@example.com"},
                    {"id": "m2", "subject": "RE: x [APPROVE-TOK]", "from": "op@example.com"},
                ]}
            if _is_metadata_get(args):
                return None if _metadata_id(args) == "m2" else _metadata([_GOOGLE_DMARC_PASS])
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json, auth=None):
            out = ec.read_replies_with_status(["TOK"])
        assert out.kind == "transport_error"
        assert out.texts == []

    def test_one_token_triage_fails_other_succeeds_is_not_ok(self, monkeypatch):
        self._env(monkeypatch)

        def fake_gws_json(args, timeout=45):
            if "+triage" in args:
                if any("TOK2" in a for a in args):
                    return None
                return {"complete": True, "messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK1]", "from": "op@example.com"}]}
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake_gws_json):
            out = ec.read_replies_with_status(["TOK1", "TOK2"])
        assert out.kind == "transport_error"
        assert out.texts == []

    @pytest.mark.parametrize("position", ("first", "second", "only"))
    @pytest.mark.parametrize("bad", ({"unexpected": []}, {"messages": "bad", "complete": True},
                                     {"messages": [{"id": 5, "subject": "RE: x",
                                                    "from": "op@example.com"}], "complete": True}))
    def test_invalid_triage_response_poison_whole_sweep(self, monkeypatch, position, bad):
        self._env(monkeypatch)
        good = {"complete": True, "messages": [
            {"id": "m1", "subject": "RE: x [APPROVE-TOK]", "from": "op@example.com"}]}

        def fake(args, timeout=45):
            if "+triage" in args:
                query = args[args.index("--query") + 1]
                return bad if position == "only" or (("TOK1" in query) == (position == "first")) else good
            return {"body_text": "APPROVE"}

        tokens = ["TOK1"] if position == "only" else ["TOK1", "TOK2"]
        with _mock_inbox(side_effect=fake):
            out = ec.read_replies_with_status(tokens)
        assert out.kind == "transport_error"
        assert out.texts == []
        assert out.detail == "gws_triage_invalid_response"

    @pytest.mark.parametrize("collection,expected", (
        ({"complete": True, "messages": []}, "ok"),
        ({"messages": []}, "ok"),
        ({"complete": True, "messages": [], "nextPageToken": "next"}, "transport_error"),
        ({"complete": True, "messages": [], "resultSizeEstimate": 2}, "transport_error"),
        ({"complete": True, "messages": [], "has_more": True}, "transport_error"),
    ))
    def test_collection_completeness(self, monkeypatch, collection, expected):
        self._env(monkeypatch)
        with _mock_inbox(collection):
            out = ec.read_replies_with_status(["TOK"], max_msgs=1)
        assert out.kind == expected
        if expected != "ok":
            assert out.texts == []
            assert "truncated" in out.detail

    def test_full_page_requires_exhaustion_and_does_not_fire_callback(self, monkeypatch):
        self._env(monkeypatch)
        seen = []

        def fake(args, timeout=45):
            if "+triage" in args:
                query = args[args.index("--query") + 1]
                if "TOK2" in query:
                    return {"messages": [], "nextPageToken": "more"}
                return {"complete": True, "messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK1]",
                     "from": "op@example.com"}]}
            return {"body_text": "APPROVE"}

        with _mock_inbox(side_effect=fake):
            out = ec.read_replies_with_status(
                ["TOK1", "TOK2"], max_msgs=1,
                on_reply_message=lambda token, mid: seen.append((token, mid)))
        assert out.kind == "transport_error"
        assert out.texts == []
        assert seen == []

    def test_oversized_body_cannot_approve(self, monkeypatch):
        self._env(monkeypatch)

        def fake(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                     "from": "op@example.com"}]}
            return {"body_text": "APPROVE" + " " * 262144}

        with _mock_inbox(side_effect=fake):
            out = ec.read_replies_with_status(["TOK"])
        assert out.kind == "transport_error"
        assert out.texts == []
        assert out.detail == "gws_read_oversized"

    @pytest.mark.parametrize("body", (
        {"body_text": "APPROVE", "body": "HOLD"},
        {"body_text": "APPROVE", "plain": []},
        {"body_text": "APPROVE", "extra": ["x" * 262144]},
    ))
    def test_invalid_or_oversized_body_fields_cannot_approve(self, monkeypatch, body):
        self._env(monkeypatch)

        def fake(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                     "from": "op@example.com"}]}
            return body

        with _mock_inbox(side_effect=fake):
            out = ec.read_replies_with_status(["TOK"])
        assert out.kind == "transport_error"
        assert out.texts == []

    def test_callback_failure_cannot_return_approval(self, monkeypatch):
        self._env(monkeypatch)

        def fake(args, timeout=45):
            if "+triage" in args:
                return {"complete": True, "messages": [
                    {"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                     "from": "op@example.com"}]}
            return {"body_text": "APPROVE"}

        def failed_callback(_token, _mid):
            raise RuntimeError("synthetic callback failure")

        with _mock_inbox(side_effect=fake):
            out = ec.read_replies_with_status(
                ["TOK"], on_reply_message=failed_callback)
        assert out.kind == "transport_error"
        assert out.texts == []


class TestSwarmMailboxConfigured:
    """Email consent needs a mailbox separate from the operator's (ateles#1221)."""

    def _env(self, monkeypatch, *, swarm, cfg):
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        if swarm is None:
            monkeypatch.delenv("ATELES_SWARM_EMAIL", raising=False)
        else:
            monkeypatch.setenv("ATELES_SWARM_EMAIL", swarm)
        if cfg is None:
            monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR", raising=False)
        else:
            monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", cfg)

    def test_distinct_address_and_existing_config_dir(self, monkeypatch, tmp_path):
        self._env(monkeypatch, swarm="swarm@example.net", cfg=str(tmp_path))
        assert ec.swarm_mailbox_configured() is True

    def test_unset_swarm_address(self, monkeypatch, tmp_path):
        self._env(monkeypatch, swarm=None, cfg=str(tmp_path))
        assert ec.swarm_mailbox_configured() is False

    def test_swarm_address_same_as_operator(self, monkeypatch, tmp_path):
        self._env(monkeypatch, swarm="Op <OP@example.com>", cfg=str(tmp_path))
        assert ec.swarm_mailbox_configured() is False

    def test_missing_config_dir(self, monkeypatch, tmp_path):
        self._env(monkeypatch, swarm="swarm@example.net", cfg=None)
        assert ec.swarm_mailbox_configured() is False
        self._env(monkeypatch, swarm="swarm@example.net", cfg=str(tmp_path / "nope"))
        assert ec.swarm_mailbox_configured() is False

    def test_unparseable_swarm_address(self, monkeypatch, tmp_path):
        self._env(monkeypatch, swarm="not-an-address", cfg=str(tmp_path))
        assert ec.swarm_mailbox_configured() is False


# ── Swarm mailbox routing (ateles#1221 consent half, folded into PR #1202) ───


def _subprocess_mailbox(calls: list, *, auth_value: str | None, body: str = "ATTENDED",
                        sender: str = "Op <op@example.com>"):
    """Fake gws at the subprocess boundary: records (argv, env) for every call
    and answers as a mailbox holding one reply from ``sender``."""
    import json as _j

    def run(cmd, **kw):
        calls.append((list(cmd), kw.get("env")))
        args = list(cmd[1:])
        if "+triage" in args:
            out = {"complete": True, "messages": [{"id": "m1", "subject": "RE: x [APPROVE-TOK]",
                                 "from": sender}]}
        elif args[:4] == ["gmail", "users", "messages", "get"]:
            headers = [] if auth_value is None else [
                {"name": "Authentication-Results", "value": auth_value}]
            out = {"id": "m1", "payload": {"headers": headers}}
        elif "+read" in args:
            out = {"body_text": body}
        else:
            out = {}
        return subprocess.CompletedProcess(cmd, 0, stdout=_j.dumps(out), stderr="")

    return run


_GMAIL_PASS = ("mx.google.com; dkim=pass header.i=@example.com header.s=s; "
               "dmarc=pass (p=NONE) header.from=example.com")


class TestSwarmMailboxRouting:
    def _arm(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        import os as _os
        return _os.environ["ATELES_SWARM_GWS_CONFIG_DIR"]

    def test_request_is_sent_from_swarm_to_operator_as_the_swarm_mailbox(self, monkeypatch):
        import os as _os
        swarm_dir = self._arm(monkeypatch)
        calls: list = []
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run",
                          side_effect=_subprocess_mailbox(calls, auth_value=None)):
            assert ec.send_request("Subj [APPROVE-TOK]", "body") is True
        (argv, env), = calls
        assert argv[argv.index("--to") + 1] == "op@example.com"
        assert argv[argv.index("--from") + 1] == "swarm@example.net"
        assert env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == swarm_dir
        # Scoped to the subprocess: the process env is not mutated.
        assert "GOOGLE_WORKSPACE_CLI_CONFIG_DIR" not in _os.environ

    def test_every_reply_read_call_runs_as_the_swarm_mailbox(self, monkeypatch):
        swarm_dir = self._arm(monkeypatch)
        calls: list = []
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run",
                          side_effect=_subprocess_mailbox(calls, auth_value=_GMAIL_PASS)):
            out = ec.read_replies_with_status(["TOK"])
        # An authenticated inbound operator reply in the swarm mailbox counts.
        assert out.kind == "ok" and len(out.texts) == 1
        kinds = {("+triage" in a and "triage") or ("+read" in a and "read")
                 or ("get" in a and "metadata") for a, _ in calls}
        assert kinds == {"triage", "metadata", "read"}
        assert all(env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == swarm_dir
                   for _, env in calls)

    def test_in_thread_reply_runs_as_the_swarm_mailbox(self, monkeypatch, tmp_path):
        swarm_dir = self._arm(monkeypatch)
        calls: list = []
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run",
                          side_effect=_subprocess_mailbox(calls, auth_value=None)):
            assert ec.reply_in_thread("m1", "done", cwd=str(tmp_path)) is True
        (argv, env), = calls
        assert argv[argv.index("--from") + 1] == "swarm@example.net"
        assert argv[argv.index("--to") + 1] == "op@example.com"
        assert env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == swarm_dir

    def test_unconfigured_swarm_mailbox_makes_no_gws_call_at_all(self, monkeypatch):
        """Never fall back to the operator's mailbox, even when the process env
        already points gws somewhere."""
        self._arm(monkeypatch)
        monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR")
        monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "/operator/default")
        with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
             patch.object(ec.subprocess, "run") as run:
            assert ec.send_request("s", "b") is False
            out = ec.read_replies_with_status(["TOK"])
            assert ec.reply_in_thread("m1", "b") is False
            assert ec.gws_json(["gmail", "+triage"]) is None
        run.assert_not_called()
        assert out.kind == "transport_error"
        assert out.detail == "swarm_mailbox_unconfigured"


class TestAuthservIdConfig:
    _ORG = "mx.example.org; dmarc=pass header.from=example.com"

    def test_default_is_gmail(self, monkeypatch):
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        assert ec.trusted_authserv_id() == "mx.google.com"
        assert ec.sender_domain_authenticated([_GMAIL_PASS]) is True
        assert ec.sender_domain_authenticated([self._ORG]) is False

    def test_override_trusts_only_the_configured_server(self, monkeypatch):
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        monkeypatch.setenv("ATELES_MAIL_AUTHSERV_ID", "MX.Example.org")
        assert ec.trusted_authserv_id() == "mx.example.org"
        assert ec.sender_domain_authenticated([self._ORG]) is True
        assert ec.sender_domain_authenticated([_GMAIL_PASS]) is False

    def test_override_applies_end_to_end_on_the_read_path(self, monkeypatch):
        monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        monkeypatch.setenv("ATELES_MAIL_AUTHSERV_ID", "mx.example.org")
        for auth, expected in ((self._ORG, 1), (_GMAIL_PASS, 0)):
            calls: list = []
            with patch.object(ec.shutil, "which", return_value="/bin/gws"), \
                 patch.object(ec.subprocess, "run",
                              side_effect=_subprocess_mailbox(calls, auth_value=auth)):
                out = ec.read_replies_with_status(["TOK"])
            assert out.kind == "ok" and len(out.texts) == expected


class TestSwarmMailboxProblems:
    def test_each_missing_variable_is_named(self, monkeypatch):
        monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
        monkeypatch.delenv("ATELES_SWARM_EMAIL")
        only_email = " ".join(ec.swarm_mailbox_problems())
        assert "ATELES_SWARM_EMAIL" in only_email
        assert "ATELES_SWARM_GWS_CONFIG_DIR" not in only_email
        monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR")
        both = " ".join(ec.swarm_mailbox_problems())
        assert "ATELES_SWARM_EMAIL" in both and "ATELES_SWARM_GWS_CONFIG_DIR" in both
        monkeypatch.setenv("ATELES_SWARM_EMAIL", "swarm@example.net")
        only_dir = " ".join(ec.swarm_mailbox_problems())
        assert "ATELES_SWARM_GWS_CONFIG_DIR" in only_dir
        assert "ATELES_SWARM_EMAIL" not in only_dir


def test_gws_config_dir_env_precedence(monkeypatch):
    # Approval selection never falls back to an ambient Workspace account.
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "/synthetic/operator-gws")
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", "/synthetic/swarm-gws")
    assert ec.gws_config_dir_env() == {"GOOGLE_WORKSPACE_CLI_CONFIG_DIR": "/synthetic/swarm-gws"}
    monkeypatch.delenv("ATELES_SWARM_GWS_CONFIG_DIR")
    assert ec.gws_config_dir_env() == {}
    assert ec.gws_config_dir_env(allow_ambient=True) == {
        "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": "/synthetic/operator-gws"
    }
    monkeypatch.delenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR")
    assert ec.gws_config_dir_env() == {}


def test_gws_config_dir_env_requires_existing_swarm_directory(monkeypatch, tmp_path):
    swarm = tmp_path / "swarm"
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setenv("ATELES_SWARM_GWS_CONFIG_DIR", str(swarm))
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", str(ambient))
    assert ec.gws_config_dir_env(require_existing=True) == {}
    swarm.mkdir()
    assert ec.gws_config_dir_env(require_existing=True) == {
        "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": str(swarm)
    }


@pytest.mark.parametrize("html", (
    "<blockquote/>QUOTED approve",
    "<div class='gmail_quote'/>QUOTED approve",
    "<BLOCKQUOTE />QUOTED approve",
    "<p>ok</p><blockquote/>QUOTED approve</blockquote>",
))
def test_self_closing_syntax_does_not_close_a_quote_container(html):
    # HTML ignores the slash on a non-void element: the container stays open and
    # what follows is quoted, so no quoted text is ever emitted as authored text.
    text, _boundary = ec._extract_authored_html(html)
    assert "QUOTED" not in text and "approve" not in text.lower().replace("ok", "")


@pytest.mark.parametrize("color", (
    "white", "#fff", "#ffffff", "#fefefe", "#f8f8f8", "rgb(255,255,255)", "rgb(250, 250, 250)",
    "rgba(255,255,255,1)", "transparent", "rgba(0,0,0,0)", "rgb(0 0 0 / 0%)", "ghostwhite", "snow",
    "#ffffff00", "WHITE !important",
))
def test_near_white_or_transparent_text_is_not_authored_even_without_a_matching_background(color):
    # The background may come from an ancestor or a style block, which this reader
    # cannot evaluate, so such text makes the HTML not safely separable.
    html = f'<body style="background:#fff"><span style="color:{color}">HIDDEN approve</span>x</body>'
    text, _boundary = ec._extract_authored_html(html)
    assert "HIDDEN" not in text and "approve" not in text.lower()


@pytest.mark.parametrize("color", ("#333", "black", "rgb(20, 20, 20)", "rgba(0,0,0,1)", "darkslategray"))
def test_ordinary_text_colours_stay_readable(color):
    text, _boundary = ec._extract_authored_html(f'<body><p style="color:{color}">approve</p></body>')
    assert "approve" in text.lower()


@pytest.mark.parametrize("body", ("yes", "Can you clarify?", "approve v1.2.3"))
def test_subject_discovery_without_body_token_is_correction_only(monkeypatch, body):
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    seen = []

    def fake_gws(args, timeout=45):
        if "+triage" in args:
            query = args[args.index("--query") + 1]
            rows = ([{"id": "m1", "from": "op@example.com",
                      "subject": "RE: Release v1.2.3 ready to approve"}]
                    if "subject:" in query else [])
            return {"complete": True, "messages": rows}
        return {"body_text": body}

    with _mock_inbox(side_effect=fake_gws):
        outcome = ec.read_replies_with_status(
            ["release-approve:"], correction_queries=["ready to approve"],
            on_reply_message=lambda *_: seen.append(True))
    assert outcome.kind == "ok"
    assert outcome.texts == [] and seen == []
    assert [reply.message_id for reply in outcome.correction_only_replies] == ["m1"]


def test_subject_discovery_still_requires_sender_authentication(monkeypatch):
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

    def fake_gws(args, timeout=45):
        if "+triage" in args:
            return {"complete": True, "messages": [
                {"id": "m1", "from": "op@example.com",
                 "subject": "RE: Release v1.2.3 ready to approve"}]}
        return {"body_text": "yes"}

    with _mock_inbox(side_effect=fake_gws), \
         patch.object(ec, "_fetch_auth_results", return_value=[]):
        outcome = ec.read_replies_with_status(
            ["release-approve:"], correction_queries=["ready to approve"])
    assert outcome.rejected_replies
    assert outcome.correction_only_replies == () and outcome.texts == []


@pytest.mark.parametrize("container", (
    "<blockquote>{}</blockquote>",
    '<div class="gmail_quote">{}</div>',
    "<blockquote><blockquote>{}</blockquote></blockquote>",
))
def test_unproven_html_quote_holds_the_whole_reply(monkeypatch, container):
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")
    quoted = "<p>release-approve: v1.2.3</p><p>I withdraw</p>"
    html = "<p>approve v1.2.3</p>" + container.format(quoted)

    def fake_gws(args, timeout=45):
        if "+triage" in args:
            return {"complete": True, "messages": [
                {"id": "m1", "from": "op@example.com",
                 "subject": "RE: Release v1.2.3 ready to approve"}]}
        return {"body_html": html}

    with _mock_inbox(side_effect=fake_gws):
        outcome = ec.read_replies_with_status(["release-approve:"])
    assert outcome.kind == "transport_error"
    assert outcome.texts == []
    assert [reply.message_id for reply in outcome.unreadable_replies] == ["m1"]


def test_known_quoted_request_remains_readable(monkeypatch):
    monkeypatch.setenv("ATELES_NOTIFY_EMAIL", "1")
    monkeypatch.setenv("OPERATOR_EMAIL", "op@example.com")

    def fake_gws(args, timeout=45):
        if "+triage" in args:
            return {"complete": True, "messages": [
                {"id": "m1", "from": "op@example.com",
                 "subject": "RE: Release v1.2.3 ready to approve"}]}
        return {"body_html": ("<p>approve v1.2.3</p><blockquote>"
                              "<p>release-approve: v1.2.3</p></blockquote>")}

    with _mock_inbox(side_effect=fake_gws):
        outcome = ec.read_replies_with_status(["release-approve:"])
    assert outcome.kind == "ok" and len(outcome.texts) == 1
