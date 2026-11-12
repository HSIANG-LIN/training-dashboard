#!/usr/bin/env python3
"""regenerate — 讀取 Obsidian 訓練紀錄/菜單，產生 data.json + index.html。

用法： python3 regenerate.py
輸出： data.json（資料）、index.html（靜態 dashboard，資料內嵌，可直接雙擊開啟）
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = Path("/mnt/d/ObsidianVault/health/訓練紀錄.md")
MENU = Path("/mnt/d/ObsidianVault/health/訓練菜單.md")
TEMPLATE = BASE / "template.html"
TODAY = dt.date.today()

SECTION_ORDER = ["胸部", "背部", "腿部", "肩部", "手臂", "核心"]
SECTION_ALIAS = {"大腿": "腿部"}
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FFFF☀-➿️‍]+")
WARN: list[str] = []


# ---------------------------------------------------------------- table parse
def parse_tables(text: str):
    """回傳 (body_rows, cardio_rows)。body_rows: section→[row]。"""
    lines = text.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":  # frontmatter
        for j in range(1, len(lines)):
            if lines[j].strip() == "---":
                i = j + 1
                break

    body: dict[str, list[dict]] = {}
    cardio: list[dict] = []
    section, kind = None, None

    for line in lines[i:]:
        if line.startswith("## "):
            title = EMOJI_RE.sub("", line[3:]).strip()
            title = title.split("（")[0].strip()
            section = SECTION_ALIAS.get(title, title)
            kind = None
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and all(c and set(c) <= set("-: ") for c in cells):
            continue  # separator
        if cells[0] == "日期":
            kind = "cardio" if len(cells) > 1 and cells[1] == "方式" else "body"
            continue
        if not re.match(r"\d{4}-\d{2}-\d{2}$", cells[0]):
            continue
        date = dt.date.fromisoformat(cells[0])
        if kind == "body" and len(cells) >= 7 and section:
            cells += [""] * (8 - len(cells))
            body.setdefault(section, []).append(
                {"date": date, "name": cells[1].strip(), "sets": cells[2:7],
                 "note": cells[7], "section": section}
            )
        elif kind == "cardio" and len(cells) >= 5:
            cells += [""] * (6 - len(cells))
            cardio.append({"date": date, "mode": cells[1], "incline": cells[2],
                           "speed": cells[3], "time": cells[4], "note": cells[5]})
    return body, cardio


# ---------------------------------------------------------------- set parsing
def parse_set_cell(cell: str) -> list[dict]:
    """組欄 → segments。涵蓋 65*20 / 65*4+55*4 / 自重×10 / 40秒 / 30下 / 空值。"""
    cell = (cell or "").strip()
    if cell in ("", "—", "-", "–"):
        return []
    segs = []
    for p in re.split(r"\s*\+\s*", cell):
        p = p.strip()
        if not p:
            continue
        m = re.fullmatch(r"自重\s*(?:[×x*]\s*(\d+))?", p)
        if m:
            segs.append({"t": "bw", "r": int(m.group(1)) if m.group(1) else 0})
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(kg|lbs)\s*[×x*]\s*(\d+)", p, re.I)
        if m:
            segs.append({"t": "wl", "w": float(m.group(1)),
                         "u": m.group(2).lower(), "r": int(m.group(3))})
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[×x*]\s*(\d+)", p)  # 65*20 無單位
        if m:
            segs.append({"t": "wl", "w": float(m.group(1)), "u": None,
                         "r": int(m.group(3))})
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*秒", p)
        if m:
            segs.append({"t": "tm", "s": float(m.group(1))})
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:下|次)?", p)
        if m:
            segs.append({"t": "rp", "r": float(m.group(1))})
            continue
        segs.append({"t": "raw", "x": p})
        WARN.append(f"無法解析組欄: {cell!r}")
    return segs


def fix_units(segs: list[dict], group_units: set[str]) -> list[dict]:
    """無單位重量：若該動作只用過一種單位就補上，否則維持 None（不進總量）。"""
    for s in segs:
        if s["t"] == "wl" and s["u"] is None and len(group_units) == 1:
            s["u"] = next(iter(group_units))
    return segs


# ---------------------------------------------------------------- aggregations
def build_equipment(rows: list[dict]) -> list[dict]:
    """同一動作全名 = 同一台設備（資料已用括號區分台別，不混比）。"""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["name"].strip(), []).append(r)

    # 先收集各 group 出現過的單位，供無單位重量補碼
    known: dict[str, set[str]] = {}
    for name, rs in groups.items():
        us = {s["u"] for r in rs for c in r["sets"]
              for s in parse_set_cell(c) if s["t"] == "wl" and s["u"]}
        known[name] = us

    out = []
    for name, rs in groups.items():
        rs.sort(key=lambda r: r["date"])
        for r in rs:
            r["segs"] = [fix_units(parse_set_cell(c), known[name])
                         for c in r["sets"]]

        all_segs = [s for r in rs for c in r["segs"] for s in c]
        if any(s["t"] == "wl" for s in all_segs):
            kind = "weight"
        elif any(s["t"] == "tm" for s in all_segs):
            kind = "time"
        else:
            kind = "reps"

        # 每次訓練（日期）一個代表值
        by_date: dict[dt.date, dict] = {}
        for r in rs:
            d = r["date"]
            e = by_date.setdefault(d, {"w": None, "u": None, "r": 0, "s": 0.0,
                                       "reps_at_max": None})
            for c in r["segs"]:
                for seg in c:
                    if seg["t"] == "wl" and seg["u"]:
                        if e["w"] is None or seg["w"] > e["w"]:
                            e["w"], e["u"], e["reps_at_max"] = seg["w"], seg["u"], seg["r"]
                    elif seg["t"] == "bw":
                        e["r"] = max(e["r"], seg["r"])
                    elif seg["t"] == "tm":
                        e["s"] = max(e["s"], seg["s"])
                    elif seg["t"] == "rp":
                        e["r"] = max(e["r"], seg["r"])

        points = []
        for d in sorted(by_date):
            e = by_date[d]
            if kind == "weight" and e["w"] is not None:
                points.append({"date": d.isoformat(), "value": e["w"],
                               "unit": e["u"], "reps": e["reps_at_max"]})
            elif kind == "time" and e["s"]:
                points.append({"date": d.isoformat(), "value": e["s"], "unit": "秒"})
            elif kind == "reps" and e["r"]:
                points.append({"date": d.isoformat(), "value": e["r"], "unit": "下"})
        if not points:
            continue

        cur, prev = points[-1], (points[-2] if len(points) > 1 else None)
        delta = round(cur["value"] - prev["value"], 1) if prev else None
        base = name.split("（")[0].strip()
        sub = name[len(base):].strip("（）")
        out.append({
            "id": re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", name).strip("-"),
            "name": name, "base": base, "sub": sub,
            "section": rs[-1]["section"], "kind": kind,
            "unit": cur["unit"], "current": cur["value"],
            "reps": cur.get("reps_at_max"),
            "prev": prev, "delta": delta,
            "date": cur["date"], "last_date": rs[-1]["date"].isoformat(),
            "points": points, "sessions": len(points),
        })
    out.sort(key=lambda e: (e["last_date"], e["name"]), reverse=True)
    return out


def build_sessions(rows: list[dict]) -> list[dict]:
    by_date: dict[dt.date, dict] = {}
    for r in rows:
        e = by_date.setdefault(r["date"], {"date": r["date"], "parts": [],
                                           "sets": 0, "vol": {}})
        if r["section"] not in e["parts"]:
            e["parts"].append(r["section"])
        for c in r["sets"]:
            segs = parse_set_cell(c)
            if not segs:
                continue
            e["sets"] += 1  # 一格 = 一組（drop set 同組）
            for s in segs:
                if s["t"] == "wl" and s["u"]:
                    e["vol"][s["u"]] = e["vol"].get(s["u"], 0) + s["w"] * s["r"]
    out = []
    for d in sorted(by_date, reverse=True)[:10]:
        e = by_date[d]
        out.append({"date": d.isoformat(),
                    "parts": [p for p in SECTION_ORDER if p in e["parts"]]
                            + [p for p in e["parts"] if p not in SECTION_ORDER],
                    "sets": e["sets"],
                    "vol": {u: round(v, 1) for u, v in e["vol"].items()}})
    return out


def build_section_compare(rows: list[dict], today: dt.date) -> list[dict]:
    windows = {"last": (today - dt.timedelta(days=6), today),
               "prev": (today - dt.timedelta(days=13), today - dt.timedelta(days=7))}
    out = []
    for sec in SECTION_ORDER:
        rs = [r for r in rows if r["section"] == sec]
        counts = {"last": 0, "prev": 0}
        for r in rs:
            for k, (a, b) in windows.items():
                if a <= r["date"] <= b:
                    counts[k] += sum(1 for c in r["sets"] if parse_set_cell(c))
        out.append({"section": sec, "last": counts["last"], "prev": counts["prev"]})
    return out


def parse_cardio(cardio: list[dict]) -> list[dict]:
    rec = {"time": 0.0, "speed": 0.0, "km": 0.0, "kcal": 0.0}
    out = []
    for r in sorted(cardio, key=lambda x: x["date"]):  # 由舊到新追蹤新高
        def num(pat, s):
            m = re.search(pat, s or "")
            return float(m.group(1)) if m else None
        minutes = num(r"^(\d+(?:\.\d+)?)\s*分", r["time"])
        speed = num(r"^(\d+(?:\.\d+))?$", r["speed"].strip())
        incline = num(r"^(\d+(?:\.\d+)?)$", r["incline"].strip())
        km = num(r"(\d+(?:\.\d+)?)\s*km", r["note"])
        kcal = num(r"(\d+(?:\.\d+)?)\s*大卡", r["note"])
        pr = {}
        for key, val in (("time", minutes), ("speed", speed),
                         ("km", km), ("kcal", kcal)):
            if val is not None and val > rec[key]:
                rec[key] = val
                pr[key] = True
        out.append({"date": r["date"].isoformat(), "mode": r["mode"],
                    "incline": incline, "speed": speed, "time": minutes,
                    "km": km, "kcal": kcal, "pr": pr, "note": r["note"]})
    out.reverse()  # 新日在上
    return out


def build_schedule(sessions: list[dict], today: dt.date) -> list[dict]:
    """本週排程（週計畫表）vs 實際。"""
    themes: dict[str, str] = {}
    if MENU.exists():
        txt = MENU.read_text(encoding="utf-8")
        m = re.search(r"## 📅 週計畫\n(.*?)(?=\n## )", txt, re.S)
        if m:
            for line in m.group(1).splitlines():
                if line.startswith("|"):
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    if cells and cells[0] in "一二三四五六日" and len(cells) > 1 \
                            and not set(cells[0]) <= set("-"):
                        themes[cells[0]] = cells[1]
    day_chars = "一二三四五六日"
    expect = (("腿", ["腿部"]), ("推", ["胸部", "肩部", "手臂"]),
              ("拉", ["背部", "手臂"]), ("上肢", ["背部", "胸部", "肩部", "手臂"]))
    monday = today - dt.timedelta(days=today.weekday())
    trained = {s["date"]: s for s in sessions}
    out = []
    for i in range(7):
        d = monday + dt.timedelta(days=i)
        ch = day_chars[i]
        theme = themes.get(ch, "休息" if ch in "三五" else "")
        is_rest = theme in ("休息", "—", "")
        s = trained.get(d.isoformat())
        if s:
            status = "extra" if is_rest else "done"
        elif d == today:
            status = "today" if not is_rest else "rest"
        elif d > today:
            status = "upcoming" if not is_rest else "rest"
        else:
            status = "missed" if not is_rest else "rest"
        mismatch = False
        if status == "done":
            for kw, parts in expect:
                if kw in theme:
                    mismatch = not any(p in (s["parts"] if s else []) for p in parts)
                    break
        out.append({"date": d.isoformat(), "day": ch, "theme": theme,
                    "status": status, "mismatch": mismatch,
                    "parts": s["parts"] if s else [],
                    "sets": s["sets"] if s else 0})
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    if not LOG.exists():
        print(f"ERROR: 找不到 {LOG}", file=sys.stderr)
        return 1
    body, cardio = parse_tables(LOG.read_text(encoding="utf-8"))
    rows = [r for rs in body.values() for r in rs]

    equipment = build_equipment(rows)
    sessions_all = build_sessions(rows)  # 10 堂
    # 全部訓練日期（供排程比對）
    all_dates = {r["date"].isoformat() for r in rows}
    full_sessions = []
    by_date: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x["date"], reverse=True):
        e = by_date.setdefault(r["date"].isoformat(),
                               {"date": r["date"].isoformat(), "parts": [], "sets": 0})
        if r["section"] not in e["parts"]:
            e["parts"].append(r["section"])
        e["sets"] += sum(1 for c in r["sets"] if parse_set_cell(c))
    full_sessions = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)

    schedule = build_schedule(full_sessions, TODAY)

    cardio_out = parse_cardio(cardio)
    total_sets = sum(e["sets"] for e in full_sessions)
    week_sets = sum(sc["sets"] for sc in schedule)

    data = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "today": TODAY.isoformat(),
        "kpi": {
            "sessions": len(full_sessions),
            "sets": total_sets,
            "week_sets": week_sets,
            "last": full_sessions[0]["date"] if full_sessions else None,
            "equipment": len(equipment),
        },
        "equipment": equipment,
        "section_compare": build_section_compare(rows, TODAY),
        "sessions": sessions_all,
        "cardio": cardio_out,
        "schedule": schedule,
        "sections": [s for s in SECTION_ORDER if s in body],
    }

    (BASE / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    tpl = TEMPLATE.read_text(encoding="utf-8")
    html = tpl.replace("__DASHBOARD_DATA__",
                       json.dumps(data, ensure_ascii=False))
    (BASE / "index.html").write_text(html, encoding="utf-8")

    # ---- 驗證輸出
    print(f"OK  rows={len(rows)} equipment={len(equipment)} "
          f"sessions={len(full_sessions)} cardio={len(cardio_out)} "
          f"schedule={len(schedule)}")
    wl = sum(1 for e in equipment if e["kind"] == "weight")
    print(f"    weight={wl} reps={sum(1 for e in equipment if e['kind']=='reps')} "
          f"time={sum(1 for e in equipment if e['kind']=='time')}")
    for w in WARN[:10]:
        print("WARN", w)
    # 總量分單位 sanity
    vol = {}
    for r in rows:
        for c in r["sets"]:
            for s in parse_set_cell(c):
                if s["t"] == "wl" and s["u"]:
                    vol[s["u"]] = vol.get(s["u"], 0) + s["w"] * s["r"]
    print("    volume:", {u: round(v) for u, v in vol.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
