from e2ekg.explain.refuted import Refuted, parse_refuted


def test_simple_line():
    r = parse_refuted(["https://kg.local/chg/deployment/hero/01J239A03CBAD72F638D493134 refuted: no dependency path ... landed at 22:18:00Z"])
    assert r == [Refuted(("https://kg.local/chg/deployment/hero/01J239A03CBAD72F638D493134",), "no dependency path ... landed at 22:18:00Z", False)]


def test_as_root_and_sibling_ids_are_split():
    line = ("https://kg.local/chg/scaling/hero/b9653116-19a1-ef6f-1046-404aa6e78aab (and sibling scale-downs 0edf7fa4, 04094b3a, 6a487e8e) "
            "refuted as root: the autoscaler could not take the fleet below the old floor")
    known = {"https://kg.local/chg/scaling/hero/b9653116-19a1-ef6f-1046-404aa6e78aab",
             "https://kg.local/chg/scaling/hero/0edf7fa4-aaaa", "https://kg.local/chg/scaling/hero/04094b3a-bbbb",
             "https://kg.local/chg/scaling/hero/6a487e8e-cccc"}
    [r] = parse_refuted([line], known)
    assert r.as_root and len(r.uris) == 4 and r.reason.startswith("the autoscaler")


def test_unresolved_prefix_is_dropped():
    line = "https://kg.local/chg/scaling/hero/aaaa1111 (and sibling deadbeef) refuted as root: nope"
    [r] = parse_refuted([line], {"https://kg.local/chg/scaling/hero/aaaa1111"})
    assert r.uris == ("https://kg.local/chg/scaling/hero/aaaa1111",)


def test_unparseable_line_is_kept():
    [r] = parse_refuted(["the gateway was never plausible"])
    assert r.uris == () and r.reason == "the gateway was never plausible" and not r.as_root
