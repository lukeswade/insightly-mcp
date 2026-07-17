#!/usr/bin/env python3
"""Lightweight unit tests for the pure helpers in insightly_mcp.

Run (deps come from uv, like the server):
    uv run --with mcp --with httpx --with pydantic python tests/test_helpers.py

Exits non-zero on the first failed assertion. No pytest required.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import insightly_mcp as m  # noqa: E402


def test_obj_normalisation():
    assert m._obj("Contacts") == "Contacts"
    assert m._obj("organizations") == "Organisations"   # US → British
    assert m._obj("organization") == "Organisations"
    assert m._obj("  /Leads/ ") == "Leads"              # trims slashes/space
    assert m._obj("KnowledgeArticle") == "KnowledgeArticle"
    # the API's singular objects: plural/case aliases resolve to the real endpoint
    assert m._obj("Tickets") == "Ticket"
    assert m._obj("products") == "Product"
    assert m._obj("Quotations") == "Quotation"
    assert m._obj("pricebooks") == "Pricebook"
    # and singular aliases resolve to canonical-plural endpoints
    assert m._obj("contact") == "Contacts"
    assert m._obj("Lead") == "Leads"
    # unknown endpoints pass through untouched (raw escape hatch)
    assert m._obj("Prospect") == "Prospect"


def test_pk_for_singular_objects():
    assert m.PK["Ticket"] == "TICKET_ID"
    assert m.PK["Product"] == "PRODUCT_ID"
    assert m.PK["Quotation"] == "QUOTE_ID"      # NOT QUOTATION_ID — verified live
    assert m.PK["Pricebook"] == "PRICEBOOK_ID"


def test_brief_strip_drops_body():
    rows = [{"ARTICLE_ID": 1, "Body": "<p>huge html</p>", "Title": "x"},
            {"ARTICLE_ID": 2, "Title": "y"}]
    m._brief_strip(rows)
    assert "Body" not in rows[0] and rows[0]["Title"] == "x"
    assert rows[1] == {"ARTICLE_ID": 2, "Title": "y"}
    # non-list shapes pass through untouched
    assert m._brief_strip({"Body": "keep"}) == {"Body": "keep"}


def test_mask():
    assert m._mask("52b85ad7-f00e-48d5-bbb9-9cf1a23ed126") == "…d126"
    assert m._mask(None) == ""
    assert m._mask("ab") == "set"


def test_apply_sort_desc_and_nulls_last():
    rows = [{"n": 2}, {"n": None}, {"n": 5}, {"missing": True}, {"n": 1}]
    out = m._apply_sort(rows, "n desc")
    vals = [r.get("n") for r in out]
    assert vals[:3] == [5, 2, 1]           # descending
    assert vals[3] is None and "missing" in out[4]  # None / missing sort last
    asc = [r.get("n") for r in m._apply_sort(rows, "n asc")]
    assert asc[:3] == [1, 2, 5]
    # no order_by → unchanged
    assert m._apply_sort(rows, None) is rows


def test_apply_sort_does_not_mutate_input():
    rows = [{"n": 2}, {"n": "mixed"}, {"n": 1}]      # mixed types → str-key fallback
    snapshot = [dict(r) for r in rows]
    out = m._apply_sort(rows, "n asc")
    assert rows == snapshot                            # input untouched
    assert [r["n"] for r in out] == [1, 2, "mixed"]    # str-key order


def test_record_contains_single_and_any_field():
    rec = {"FIRST_NAME": "Tyler", "LAST_NAME": "Shedron", "AGE": 41, "CUSTOMFIELDS": []}
    assert m._record_contains(rec, "shed", "LAST_NAME")
    assert not m._record_contains(rec, "tyler", "LAST_NAME")
    assert m._record_contains(rec, "tyler")            # any-field
    assert m._record_contains(rec, "41")               # numeric scalar, any-field
    assert not m._record_contains(rec, "zzz")
    assert not m._record_contains("not a dict", "x")


def test_cf_compact():
    f = {"FIELD_NAME": "Intake_Status__c", "FIELD_LABEL": "Intake Status",
         "FIELD_TYPE": "DROPDOWN", "EDITABLE": True, "FIELD_HELP_TEXT": None,
         "CUSTOM_FIELD_OPTIONS": [{"OPTION_ID": 1, "OPTION_VALUE": "Admitted"},
                                  {"OPTION_ID": 2, "OPTION_VALUE": ""}],
         "JOIN_OBJECT": None, "DEPENDENCY": None}
    c = m._cf_compact(f)
    assert c == {"name": "Intake_Status__c", "label": "Intake Status",
                 "type": "DROPDOWN", "editable": True, "options": ["Admitted"]}
    lookup = m._cf_compact({"FIELD_NAME": "Clinician__c", "FIELD_LABEL": "Clinician",
                            "FIELD_TYPE": "LOOKUPRELATIONSHIP", "EDITABLE": True,
                            "CUSTOM_FIELD_OPTIONS": [], "JOIN_OBJECT": "Contact"})
    assert lookup["links_to"] == "Contact" and "options" not in lookup


def test_write_hint_only_on_4xx():
    hinted = m._write_hint({"error": "HTTP 400", "body": "bad"}, "Contacts")
    assert "describe_object" in hinted["hint"]
    ok = m._write_hint({"CONTACT_ID": 1}, "Contacts")
    assert "hint" not in ok
    server_err = m._write_hint({"error": "HTTP 500"}, "Contacts")
    assert "hint" not in server_err


def test_page_envelope_has_more():
    # a full page implies there may be more
    full = m._page_envelope([{}, {}, {}], skip=0, top=3)
    assert full["has_more"] is True and full["next_skip"] == 3 and full["returned"] == 3
    # a short page is the last page
    short = m._page_envelope([{}, {}], skip=10, top=3)
    assert short["has_more"] is False and short["next_skip"] == 12


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run()
