"""Pure economy-health maths (logic/econstats.py)."""

from logic import econstats as ES
from logic import shop as S


def test_summarize_uses_net_per_reason():
    rows = [("daily", 1000), ("voice", 300), ("give", 0), ("coinflip", -50), ("shop", -2000),
            ("raffle", -200), ("daily", 100), ("predict", 0)]
    s = ES.summarize(rows)
    assert s.minted == 1400 and s.burned == 2250 and s.net == -850
    assert [(f.reason, f.net) for f in s.sources] == [("daily", 1100), ("voice", 300)]
    assert [(f.reason, f.net) for f in s.sinks] == [("shop", -2000), ("raffle", -200), ("coinflip", -50)]


def test_summarize_top_n_and_ties_by_name():
    s = ES.summarize([(r, 10) for r in "edcba"] + [("z", 5)], top=3)
    assert [f.reason for f in s.sources] == ["a", "b", "c"] and s.minted == 55
    assert ES.summarize([]) == ES.Summary(0, 0, [], [])
    assert ES.summarize([("x", None)]).minted == 0


def test_supply_total_median_and_concentration():
    sup = ES.supply([0, 100, 300, 200, -5])
    assert (sup.total, sup.holders, sup.median) == (600, 3, 200)
    assert sup.top_share == 1.0
    sup = ES.supply([10] * 20 + [0])
    assert sup.median == 10 and sup.top_share == 0.5
    assert ES.supply([1, 3]).median == 2
    assert ES.supply([]) == ES.Supply(0, 0, 0, 0.0)


def test_labels_cover_every_known_reason():
    for reason in ("daily", "shop", S.RAFFLE_REASON, S.SEASON_REASON, "word", "challenge", "tournament"):
        assert ES.label(reason) != reason
    assert ES.label("mystery") == "mystery"


def test_flow_lines_and_pct():
    assert ES.flow_lines([]) == "none"
    assert ES.flow_lines([ES.Flow("daily", 1200), ES.Flow("shop", -300)]) == "Daily: +1,200\nShop: -300"
    assert ES.pct(1, 4) == "25.0%" and ES.pct(1, 0) == "n/a"


def test_verdict():
    assert "No coins" in ES.verdict(ES.Summary(0, 0, [], []), 1000)
    assert "keeping up" in ES.verdict(ES.Summary(100, 300, [], []), 1000)
    assert "Consider" in ES.verdict(ES.Summary(500, 100, [], []), 1000)  # +40%
    assert "Mild" in ES.verdict(ES.Summary(150, 100, [], []), 1000) and "5.0%" in ES.verdict(
        ES.Summary(150, 100, [], []), 1000)
    assert "50 coins" in ES.verdict(ES.Summary(50, 0, [], []), 0)
