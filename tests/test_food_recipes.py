"""test_food_recipes.py — 媒合（完全/缺一味/canonical/全缺）+ dislikes 排除 + disclaimer + 不點名。"""
import food_recipes as fr


def _dishes(matches):
    return {m["dish"] for m in matches}


def test_match_full_single():
    m = fr.match_recipes(["虱目魚肚"])
    assert "煎虱目魚肚" in _dishes(m)
    full = [x for x in m if x["dish"] == "煎虱目魚肚"][0]
    assert full["missing"] == []


def test_match_missing_one():
    m = fr.match_recipes(["蛋"])
    assert "蛋花湯" in _dishes(m)          # need[蛋] 完全可煮
    assert "蘿蔔糕煎蛋" in _dishes(m)       # 缺蘿蔔糕
    luo = [x for x in m if x["dish"] == "蘿蔔糕煎蛋"][0]
    assert luo["missing"] == ["蘿蔔糕"]


def test_canonical_match():
    # GP1 C5e：recipe need「豬肉」要對上 inventory canonical「豬」
    assert "豬肉炒高麗菜" in _dishes(fr.match_recipes(["豬", "高麗菜"]))


def test_all_missing_excluded():
    assert fr.match_recipes(["蘋果"]) == []   # 沒 recipe need 蘋果 → 全缺不列


def test_dislikes_excludes():
    m = fr.match_recipes(["豆腐"], dislikes=["豆腐"])
    assert "豆腐湯" not in _dishes(m)


def test_empty_inventory():
    assert fr.match_recipes([]) == []
    assert fr.format_suggestions([]) == ""   # graceful-empty


def test_disclaimer_present():
    out = fr.format_suggestions(["虱目魚肚"])
    assert fr.DISCLAIMER in out
    assert "煎虱目魚肚" in out


def test_no_personal_naming():
    # GP2 A：輸出不得點名任何家庭成員
    out = fr.format_suggestions(["蛋", "虱目魚肚", "高麗菜"])
    for name in ("媽媽", "爸爸", "妹妹", "弟弟"):
        assert name not in out


def test_oa_jian_bug_fixed():
    oa = [r for r in fr.RECIPES if r["dish"] == "蚵仔煎"][0]
    assert "牡蠣" in oa["need"] and "蝦" not in oa["need"]
