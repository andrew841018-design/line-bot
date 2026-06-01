"""food_recipes.py — 食材媒合（家裡有什麼 → 能煮什麼）。

⚠️ SOURCE: RECIPES 複製自 recipes.json（@2026-05-31）。依 user directive 不讀原檔（死碼線）。
   複製時修正原 recipes.json 資料 bug：蚵仔煎 need ["蝦","蛋"] → ["牡蠣","蛋"]（蚵仔=牡蠣，非蝦）。
   tags 限 food_db.NUTRITION_TAGS、allergens 限 food_db.ALLERGENS（GP2 D 白名單，防醫療標籤滑坡）。

媒合：need ⊆ inventory（完全可煮）或缺一味；need / inventory 都先 canonical（GP1 C5e）。
用家庭層級 dislikes 排除（不點名個人，GP2 A）。輸出固定附營養 disclaimer（GP2 D）。
"""
from __future__ import annotations

import food_db
import food_signals

# 一般營養參考，非醫療建議（GP2 D：防中性標籤被當醫囑）
DISCLAIMER = "🩺 以上為一般營養參考，個人飲食請依醫囑"

# need 可寫 surface 或 canonical，match 時統一 canonical（GP1 C5e）
RECIPES: list[dict] = [
    {"dish": "蘿蔔糕煎蛋", "need": ["蘿蔔糕", "蛋"], "steps": "蘿蔔糕切片煎金黃，淋蛋液再煎 1 分鐘", "tags": ["高澱粉", "中鈉"], "allergens": ["蛋"]},
    {"dish": "煎虱目魚肚", "need": ["虱目魚肚"], "steps": "魚肚抹鹽，鍋熱下油煎到皮酥 3 分鐘", "tags": ["高蛋白", "中油"], "allergens": ["魚"]},
    {"dish": "虱目魚丸湯", "need": ["虱目魚丸"], "steps": "水滾下薑片，魚丸浮起即熟，撒鹽蔥花", "tags": ["高蛋白", "中鈉"], "allergens": ["魚"]},
    {"dish": "麻婆豆腐", "need": ["豆腐", "絞肉"], "steps": "蒜薑爆香絞肉，加豆瓣與水，下豆腐燴 3 分鐘", "tags": ["高鈉", "中油"], "allergens": ["豆"]},
    {"dish": "三杯雞", "need": ["雞", "薑", "蒜"], "steps": "麻油薑爆香，雞肉燜 8 分鐘拌九層塔", "tags": ["中鈉", "高油"], "allergens": []},
    {"dish": "白斬雞", "need": ["雞"], "steps": "整雞滾水煮 15 分鐘燜 10 分鐘，切片", "tags": ["高蛋白", "低鈉"], "allergens": []},
    {"dish": "蛋花湯", "need": ["蛋"], "steps": "水滾下鹽，蛋液畫圈淋入，灑蔥花", "tags": ["低油", "中鈉"], "allergens": ["蛋"]},
    {"dish": "蒸魚", "need": ["魚"], "steps": "魚鋪薑蔥，大火蒸 8 分鐘淋醬油", "tags": ["高蛋白", "低油"], "allergens": ["魚"]},
    {"dish": "蘿蔔燉排骨", "need": ["蘿蔔", "排骨"], "steps": "排骨汆燙，與蘿蔔薑燉 40 分鐘", "tags": ["高蛋白", "低鈉"], "allergens": []},
    {"dish": "番茄炒蛋", "need": ["蕃茄", "蛋"], "steps": "蕃茄炒出汁，倒蛋液炒到剛凝固", "tags": ["中油"], "allergens": ["蛋"]},
    {"dish": "炒青菜", "need": ["青菜"], "steps": "蒜爆香，青菜大火快炒 1-2 分鐘加鹽", "tags": ["低油", "低鈉"], "allergens": []},
    {"dish": "紅蘿蔔炒蛋", "need": ["紅蘿蔔", "蛋"], "steps": "紅蘿蔔絲炒軟，淋蛋液炒散加鹽", "tags": ["中油"], "allergens": ["蛋"]},
    {"dish": "豬肉炒高麗菜", "need": ["豬肉", "高麗菜"], "steps": "豬肉絲炒變色，下高麗菜大火炒 2 分鐘", "tags": ["中油", "中鈉"], "allergens": []},
    {"dish": "雞湯", "need": ["雞"], "steps": "雞肉汆燙，加薑與水燉 30 分鐘加鹽", "tags": ["低油"], "allergens": []},
    {"dish": "蚵仔煎", "need": ["牡蠣", "蛋"], "steps": "鍋熱下油，鋪蛋液與蔬菜，下牡蠣煎到金黃", "tags": ["中油"], "allergens": ["蛋", "貝類"]},
    {"dish": "煎秋刀魚", "need": ["秋刀魚"], "steps": "魚抹鹽，鍋熱少油煎兩面焦香", "tags": ["高油", "高蛋白"], "allergens": ["魚"]},
    {"dish": "豆腐湯", "need": ["豆腐"], "steps": "水滾下豆腐塊與薑，煮 3 分鐘加鹽撒蔥", "tags": ["低油"], "allergens": ["豆"]},
    {"dish": "炒高麗菜", "need": ["高麗菜"], "steps": "蒜爆香，高麗菜大火快炒加鹽", "tags": ["低油", "低鈉"], "allergens": []},
    {"dish": "粽子加熱", "need": ["粽子"], "steps": "粽子放電鍋外鍋一杯水蒸 15 分鐘", "tags": ["高澱粉", "高鈉"], "allergens": []},
    {"dish": "肉燥飯", "need": ["絞肉", "飯"], "steps": "絞肉炒變色，加醬油與水燉 10 分鐘淋熱飯", "tags": ["高鈉", "高澱粉"], "allergens": []},
    {"dish": "鮭魚煎", "need": ["鮭魚"], "steps": "魚抹鹽靜置，皮面朝下煎 3 分鐘翻面 2 分鐘", "tags": ["高蛋白", "中油"], "allergens": ["魚"]},
    {"dish": "蕃茄牛肉湯", "need": ["蕃茄", "牛肉"], "steps": "牛肉汆燙，與蕃茄薑燉 40 分鐘加鹽", "tags": ["低油"], "allergens": []},
]


def _validate_recipes() -> None:
    """啟動時驗 tags / allergens 都在白名單（GP2 D：防自由字串滑坡到醫療標籤）。"""
    for r in RECIPES:
        for t in r["tags"]:
            assert t in food_db.NUTRITION_TAGS, f"bad tag {t!r} in {r['dish']}"
        for a in r["allergens"]:
            assert a in food_db.ALLERGENS, f"bad allergen {a!r} in {r['dish']}"


_validate_recipes()


def match_recipes(
    inventory: list[str],
    dislikes: list[str] | None = None,
    max_results: int = 5,
) -> list[dict]:
    """回可煮的菜：need ⊆ inventory（完全可煮）或缺一味，且至少有一樣食材在手。

    need / inventory 都先 canonical（GP1 C5e：need「豬肉」對得上 inventory「豬」）。
    含 disliked food 的菜排除（家庭層級，不點名）。
    回 [{dish, have, missing, steps, tags, allergens}]，完全可煮優先。
    """
    inv = {food_signals.canonical(f) for f in (inventory or [])}
    dis = {food_signals.canonical(f) for f in (dislikes or [])}
    out: list[dict] = []
    for r in RECIPES:
        need = {food_signals.canonical(f) for f in r["need"]}
        if need & dis:
            continue
        missing = need - inv
        # 最多缺一味，且至少有一樣在手（missing < need 排除「全缺」）
        if len(missing) <= 1 and len(missing) < len(need):
            out.append({
                "dish": r["dish"],
                "have": sorted(need & inv),
                "missing": sorted(missing),
                "steps": r["steps"],
                "tags": list(r["tags"]),
                "allergens": list(r["allergens"]),
            })
    out.sort(key=lambda x: (len(x["missing"]), x["dish"]))
    return out[:max_results]


def format_suggestions(inventory: list[str], dislikes: list[str] | None = None) -> str:
    """組推播 / 指令用的純文字。無可煮菜回 ""（caller 做 graceful-empty）。

    只講菜色與營養標籤，不點名任何人（GP2 A）；結尾固定 disclaimer（GP2 D）。
    """
    matches = match_recipes(inventory, dislikes)
    if not matches:
        return ""
    lines = ["🍳 用家裡現有食材，可以煮："]
    for m in matches:
        tag = "/".join(m["tags"]) if m["tags"] else ""
        line = f"・{m['dish']}"
        if tag:
            line += f"（{tag}）"
        if m["missing"]:
            line += f"，還差：{'、'.join(m['missing'])}"
        if m["allergens"]:
            line += f"〔過敏原：{'、'.join(m['allergens'])}〕"
        lines.append(line)
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
