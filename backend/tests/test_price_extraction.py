from app.core.review.sentence import _extract_price, _extract_price_match


def test_price_cues_before_and_after_amount():
    samples = {
        "价格1299元": 1299, "只要1999元": 1999, "999元价格": 999,
        "到手价1299元": 1299, "售价是1299": 1299, "¥1299": 1299,
        "￥1,299": 1299, "1299块": 1299, "1299块钱": 1299,
        "1299.00元": 1299, "优惠后只需999元": 999,
    }
    for text, expected in samples.items():
        assert _extract_price(text) == expected


def test_final_promotional_price_wins_and_raw_match_is_auditable():
    match = _extract_price_match("原价1999元，优惠后只需999元")
    assert match is not None
    assert match.value == 999
    assert match.raw == "优惠后只需999元"


def test_model_time_quantity_and_plain_numbers_are_not_prices():
    for text in ("God60键盘", "晚上8点开播", "支持三种模式", "房间号707840423057"):
        assert _extract_price(text) is None
