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
