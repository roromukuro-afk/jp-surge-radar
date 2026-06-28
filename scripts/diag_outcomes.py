"""
live予測の成否レビュー (5/10/20営業日判定の蓄積を分析)。

ユーザー指定の観点を毎回同じ基準で出す標準レビューツール:
  - classify_path別 / material_quality別 / 材料あり・なしB/C / AI類似のみのdanger_fail率
  - B vs C / material_quality>0.3勝率 / 材料+チャート+出来高勝率
  - near_missの特徴 / danger_failの共通点

成功 = S/A/B (+20%到達)。hit_rate = 成功/判定数。danger_rate = danger_fail/判定数。
判定が少ない区分は (N小) と注記。live予測 (origin='live') のみ対象。

Usage: python scripts/diag_outcomes.py
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _envload import load_env
load_env()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from surge_radar import db


def lj(s, f):
    try:
        return json.loads(s) if s else f
    except Exception:
        return f


SUCCESS = {"S", "A", "B"}


def rate(rows, pred):
    n = len(rows)
    if not n:
        return "—"
    k = sum(1 for r in rows if pred(r))
    return f"{k}/{n} ({k/n*100:.0f}%)"


def seg(rows):
    """1セグメントの成績文字列。"""
    n = len(rows)
    if not n:
        return "判定0"
    hit = sum(1 for r in rows if r["result_class"] in SUCCESS)
    near = sum(1 for r in rows if r["result_class"] == "near")
    dfail = sum(1 for r in rows if r["result_class"] == "danger_fail")
    tag = " (N小)" if n < 10 else ""
    return (f"判定{n}{tag} | 勝率 {hit}/{n} ({hit/n*100:.0f}%) | "
            f"near {near} | danger_fail {dfail} ({dfail/n*100:.0f}%)")


with db.cursor() as c:
    rows = c.execute(
        """SELECT p.code,p.category,p.material_score,p.similarity_score,p.chart_score,
                  p.volume_score,p.flags, o.result_class,o.max_up_20d,o.max_drawdown,
                  o.failure_tags,o.material_continued,o.volume_continued
           FROM predictions p JOIN prediction_outcomes o ON o.prediction_id=p.id
           WHERE p.status='judged' AND COALESCE(p.origin,'live')='live'"""
    ).fetchall()

for r in rows:
    r["_f"] = lj(r["flags"], {})
    r["_mq"] = r["_f"].get("material_quality", 0) or 0
    r["_path"] = r["_f"].get("classify_path", "(none)")

n = len(rows)
print(f"=== live判定済み: {n}件 ===")
if n == 0:
    open_n = c.execute("SELECT COUNT(*) n FROM predictions WHERE status='open' AND COALESCE(origin,'live')='live'").fetchone()["n"]
    print(f"まだlive判定データがありません (open {open_n}件が5/10/20営業日経過待ち)。")
    print("最初の5営業日判定は2026-06-25分が ~2026-07-02頃 から出始めます。")
    sys.exit(0)

hit = sum(1 for r in rows if r["result_class"] in SUCCESS)
near = sum(1 for r in rows if r["result_class"] == "near")
dfail = sum(1 for r in rows if r["result_class"] == "danger_fail")
print(f"全体: 勝率 {hit}/{n} ({hit/n*100:.0f}%) | near {near} | danger_fail {dfail} ({dfail/n*100:.0f}%)")

bc = [r for r in rows if r["category"] in ("B", "C")]
abc = [r for r in rows if r["category"] in ("A", "B", "C")]

print("\n[1] classify_path別")
paths = {}
for r in rows:
    paths.setdefault(r["_path"], []).append(r)
for p, rs in sorted(paths.items(), key=lambda kv: -len(kv[1])):
    print(f"  {p:20} {seg(rs)}")

print("\n[2] material_quality別")
buckets = [("0 (材料なし)", lambda r: r["_mq"] <= 0),
           ("0〜0.3", lambda r: 0 < r["_mq"] <= 0.3),
           (">0.3", lambda r: r["_mq"] > 0.3)]
for label, f in buckets:
    print(f"  mq {label:12} {seg([r for r in rows if f(r)])}")

print("\n[3] 材料ありB/C (material_score>0.05)")
print(f"  {seg([r for r in bc if (r['material_score'] or 0) > 0.05])}")
print("[4] 材料なしB/C")
print(f"  {seg([r for r in bc if (r['material_score'] or 0) <= 0.05])}")

print("\n[5] AI類似度のみ候補 (material_score<=0.05 & similarity>=0.9) のdanger_fail率")
ai_only = [r for r in abc if (r["material_score"] or 0) <= 0.05 and (r["similarity_score"] or 0) >= 0.9]
print(f"  {seg(ai_only)}")

print("\n[6] B候補 vs C候補")
print(f"  B: {seg([r for r in rows if r['category']=='B'])}")
print(f"  C: {seg([r for r in rows if r['category']=='C'])}")

print("\n[7] material_quality>0.3候補の勝率")
print(f"  {seg([r for r in rows if r['_mq'] > 0.3])}")

print("\n[8] 材料+チャート+出来高が揃った候補 (mat>0.05 & chart>0.3 & vol>0.3)")
tri = [r for r in rows if (r["material_score"] or 0) > 0.05 and (r["chart_score"] or 0) > 0.3 and (r["volume_score"] or 0) > 0.3]
print(f"  {seg(tri)}")

print("\n[9] near_missの特徴")
nm = [r for r in rows if r["result_class"] == "near"]
if nm:
    avg_up = sum(r["max_up_20d"] or 0 for r in nm) / len(nm)
    mat_cont = sum(1 for r in nm if r["material_continued"])
    vol_cont = sum(1 for r in nm if r["volume_continued"])
    print(f"  {len(nm)}件 | 平均max_up_20d {avg_up*100:.1f}% | 材料継続 {mat_cont}/{len(nm)} | 出来高継続 {vol_cont}/{len(nm)}")
else:
    print("  near_miss なし")

print("\n[10] danger_failの共通点")
df = [r for r in rows if r["result_class"] == "danger_fail"]
if df:
    avg_dd = sum(r["max_drawdown"] or 0 for r in df) / len(df)
    tagc = {}
    for r in df:
        for t in (lj(r["failure_tags"], []) or []):
            tagc[t] = tagc.get(t, 0) + 1
    no_mat = sum(1 for r in df if (r["material_score"] or 0) <= 0.05)
    ai_hi = sum(1 for r in df if (r["similarity_score"] or 0) >= 0.9)
    print(f"  {len(df)}件 | 平均max_drawdown {avg_dd*100:.1f}% | 材料なし {no_mat}/{len(df)} | AI類似>=0.9 {ai_hi}/{len(df)}")
    print(f"  共通failure_tags: {dict(sorted(tagc.items(), key=lambda kv: -kv[1]))}")
else:
    print("  danger_fail なし")

print("\n[11] D/E→成功(拾い漏れ)分析  ※B/C条件が厳しすぎないかの判断材料 (n小のうちは変更しない)")
de = [r for r in rows if r["category"] in ("D", "E")]
de_succ = [r for r in de if r["result_class"] in SUCCESS]
print(f"  D/E判定 {len(de)}件中 成功(S/A/B) {len(de_succ)}件"
      + (f" = 拾い漏れ率 {len(de_succ)/len(de)*100:.0f}%" if de else ""))
for r in de_succ:
    sigs = []
    if (r["chart_score"] or 0) > 0.4:
        sigs.append("chart")
    if (r["volume_score"] or 0) > 0.4:
        sigs.append("volume")
    if (r["similarity_score"] or 0) >= 0.9:
        sigs.append("AI類似")
    if (r["material_score"] or 0) > 0.05:
        sigs.append("材料")
    print(f"   {r['code']} [{r['category']}→{r['result_class']}] path={r['_path']} "
          f"mq={r['_mq']:.2f} mat={(r['material_score'] or 0):.2f} chart={(r['chart_score'] or 0):.2f} "
          f"vol={(r['volume_score'] or 0):.2f} sim={(r['similarity_score'] or 0):.2f} "
          f"up20={(r['max_up_20d'] or 0)*100:.0f}%")
    print(f"      効いた信号: {sigs or ['弱シグナルのみ']} | 材料なし={(r['material_score'] or 0) <= 0.05} | "
          f"なぜB/C外: chart/vol/材料が分類閾値未満だった可能性")
# 共通点サマリ
if len(de_succ) >= 2:
    nomat = sum(1 for r in de_succ if (r["material_score"] or 0) <= 0.05)
    ai = sum(1 for r in de_succ if (r["similarity_score"] or 0) >= 0.9)
    avgmq = sum(r["_mq"] for r in de_succ) / len(de_succ)
    print(f"  共通点: 材料なし {nomat}/{len(de_succ)} | AI類似>=0.9 {ai}/{len(de_succ)} | 平均mq {avgmq:.2f}")

print("\n[12] 低AI類似×出来高+材料 主導の成功割合  ※B分類がAI類似に寄りすぎていないかの判断材料")
succ = [r for r in rows if r["result_class"] in SUCCESS]
if succ:
    # AI類似 < 0.7 かつ (出来高 or 材料) が効いて上がった成功
    low_ai_vm = [r for r in succ
                 if (r["similarity_score"] or 0) < 0.7
                 and ((r["volume_score"] or 0) > 0.4 or (r["material_score"] or 0) > 0.05)]
    high_ai = [r for r in succ if (r["similarity_score"] or 0) >= 0.9]
    print(f"  全成功 {len(succ)}件中:")
    print(f"   低AI類似(<0.7)×出来高/材料主導の成功: {len(low_ai_vm)} ({len(low_ai_vm)/len(succ)*100:.0f}%)"
          " ← 拾い漏れ/AI偏重の兆候")
    print(f"   高AI類似(>=0.9)主導の成功: {len(high_ai)} ({len(high_ai)/len(succ)*100:.0f}%)")
    print("  ※ 低AI類似×出来高/材料の比率が高いほど、B分類のAI類似偏重で急騰を拾い漏れている可能性。")
    print("    ただし十分な件数(目安: 各区分20件以上)が揃うまで条件は変更しない。")
else:
    print("  成功判定なし")
