"""잠정실적 표 파싱: 음수 부호·빈칸 '-'·흑자/적자 전환 칸 (2026-10-28 큐리옥스 버그 회귀)."""
from summarize import _earnings_metric

# 영업이익·법인세차감전 줄은 큐리옥스바이오시스템즈 2026-10-27 공정공시 원문, 매출액 줄은 예시 숫자
CURIOX = ("매출액 당해실적 3,512 3,020 16.29 - 2,951 19.01 - 누계실적 9,800 - - - 8,100 20.99 - "
          "영업이익 당해실적 -3,960 -4,457 11.15 - -2,680 -47.76 - 누계실적 -11,669 - - - -9,662 -20.77 - "
          "법인세비용차감전계속사업이익 당해실적 -5,566 -4,869 -14.32 - -3,046 -82.73 - ")


def test_operating_loss_keeps_minus_sign():
    cur, yoy, qoq = _earnings_metric(CURIOX, "영업이익")
    assert cur == -3960
    assert yoy == "적자지속"
    assert qoq == "적자지속"


def test_revenue_columns_not_shifted_by_blank_cells():
    cur, yoy, qoq = _earnings_metric(CURIOX, "매출액")
    assert cur == 3512
    assert qoq == 16.29
    assert yoy == 19.01


def test_turnaround_and_unicode_minus():
    text = "영업이익 당해실적 1,200 −300 - 흑자전환 500 140.0 - 누계실적"
    cur, yoy, qoq = _earnings_metric(text, "영업이익")
    assert cur == 1200
    assert qoq == "흑자전환"
    assert yoy == 140.0


def test_triangle_negative_and_loss_turn():
    text = "당기순이익 당해실적 △250 100 - - 300 - - 누계실적"
    cur, yoy, qoq = _earnings_metric(text, "당기순이익")
    assert cur == -250
    assert qoq == "적자전환"
    assert yoy == "적자전환"


def test_old_five_column_format():
    text = "매출액 당해실적 1,000 800 25.0 900 11.1 누계실적"
    assert _earnings_metric(text, "매출액") == (1000, 11.1, 25.0)


def test_blank_then_positive_number_not_merged():
    # '-' 빈칸 뒤 양수가 '-2,680'처럼 붙어 읽히면 안 된다
    text = "매출액 당해실적 3,000 2,500 20.0 - 2,680 11.9 - 누계실적"
    cur, yoy, qoq = _earnings_metric(text, "매출액")
    assert (cur, qoq, yoy) == (3000, 20.0, 11.9)
