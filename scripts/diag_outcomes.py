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

# ===== 非バイアス早期パフォーマンス (open含む全追跡・5営業日) =====
# judged は「+20%到達 or 20営業日到達」だけなので生存者バイアスがある。
# ここは status に関わらず bars_tracked>=5 の全追跡予測の「5営業日の実際の値動き」を見る。
print("\n===== [13] 早期パフォーマンス (非バイアス: judged/open問わず bars_tracked>=5, 5営業日) =====")
print("  ※ +20%到達で確定した分だけでなく追跡中(open)も含む。生存者バイアスを除いた早期指標。")
with db.cursor() as c2:
    trk = c2.execute(
        """SELECT p.category, p.material_score, p.similarity_score, p.chart_score,
                  p.volume_score, p.flags, o.bars_tracked, o.max_up_5d, o.max_up_10d
           FROM predictions p JOIN prediction_outcomes o ON o.prediction_id=p.id
           WHERE COALESCE(p.origin,'live')='live' AND o.bars_tracked>=5"""
    ).fetchall()
for r in trk:
    r["_f"] = lj(r["flags"], {})
    r["_mq"] = r["_f"].get("material_quality", 0) or 0
    r["_path"] = r["_f"].get("classify_path", "(none)")


def early(rs):
    m = len(rs)
    if not m:
        return "追跡0"
    up20 = sum(1 for r in rs if (r["max_up_5d"] or 0) >= 0.20)
    up10 = sum(1 for r in rs if (r["max_up_5d"] or 0) >= 0.10)
    avg = sum((r["max_up_5d"] or 0) for r in rs) / m
    tag = " (N小)" if m < 20 else ""
    return (f"追跡{m}{tag} | 5日+20% {up20}({up20/m*100:.0f}%) | "
            f"+10% {up10}({up10/m*100:.0f}%) | 平均max5d {avg*100:+.1f}%")


print(f"  追跡総数(bars>=5): {len(trk)}")
if trk:
    print("  --- カテゴリ別 ---")
    for cat in ["A", "B", "C", "D", "E"]:
        rs = [r for r in trk if r["category"] == cat]
        if rs:
            print(f"   [{cat}] {early(rs)}")
    print(f"   B/C計: {early([r for r in trk if r['category'] in ('B', 'C')])}")
    print(f"   D/E計: {early([r for r in trk if r['category'] in ('D', 'E')])}")
    print("  --- 材料/品質別 ---")
    print(f"   材料あり(mat>0.05): {early([r for r in trk if (r['material_score'] or 0) > 0.05])}")
    print(f"   材料なし:           {early([r for r in trk if (r['material_score'] or 0) <= 0.05])}")
    print(f"   mq>0.3:             {early([r for r in trk if r['_mq'] > 0.3])}")
    print(f"   材料+chart+vol:     {early([r for r in trk if (r['material_score'] or 0) > 0.05 and (r['chart_score'] or 0) > 0.3 and (r['volume_score'] or 0) > 0.3])}")
    print(f"   低AI類似(<0.7)×出来高/材料: {early([r for r in trk if (r['similarity_score'] or 0) < 0.7 and ((r['volume_score'] or 0) > 0.4 or (r['material_score'] or 0) > 0.05)])}")
    print("  --- classify_path別(上位6) ---")
    pth: dict = {}
    for r in trk:
        pth.setdefault(r["_path"], []).append(r)
    for p, rs in sorted(pth.items(), key=lambda kv: -len(kv[1]))[:6]:
        print(f"   {p:18} {early(rs)}")
    print("  ※ B/CがD/Eより『5日+20%到達率』『平均max5d』で優位なら分類は機能。")
    print("    逆に差が無い/D/Eが優位なら拾い漏れの兆候。ただし20営業日確定まで条件は変更しない。")

print("\n===== [14] 自律学習フィードバック 現在の状態 (learning.py, 完全自律・毎日自動更新) =====")
print("  ※ ここに出るtrust_multiplierが現在ライブでスコアリングに適用されている値。")
print("    maturity_ramp: 運用歴が浅いうちは振れ幅を自動抑制(生存者バイアス対策)。")
with db.cursor() as c3:
    pp = c3.execute(
        "SELECT classify_path,n_judged,n_success,n_danger_fail,raw_hit_rate,"
        "shrunk_hit_rate,trust_multiplier,updated_at FROM path_performance "
        "ORDER BY n_judged DESC"
    ).fetchall()
if pp:
    last_updated = pp[0]["updated_at"]
    print(f"  最終更新: {last_updated}")
    for r in pp:
        flag = " ← 実績により減点中" if r["trust_multiplier"] < 0.97 else (
               " ← 実績により加点中" if r["trust_multiplier"] > 1.03 else "")
        print(f"   {r['classify_path']:20} n={r['n_judged']:4} 生勝率={r['raw_hit_rate']:.2f} "
              f"補正後={r['shrunk_hit_rate']:.2f} trust={r['trust_multiplier']:.3f}{flag}")
else:
    print("  未計算 (次回dailyのlearningステップで生成される)")

print("\n===== [15] 早期成功 vs 満期到達 (bars>=18) の実力差 =====")
print("  ※ 全体のhit_rateは「+20%に早く到達した分だけ」を多く含む生存者バイアスがかかる。")
print("    ここは早期に決着しなかった(20日近くまで持ち越した)ケースだけに絞った「本当の実力」。")
with db.cursor() as c4:
    mature = c4.execute(
        """SELECT p.category,p.flags,o.result_class
           FROM predictions p JOIN prediction_outcomes o ON o.prediction_id=p.id
           WHERE p.status='judged' AND o.bars_tracked>=18"""
    ).fetchall()
for r in mature:
    r["_f"] = lj(r["flags"], {})
    r["_mq"] = r["_f"].get("material_quality", 0) or 0
    r["_path"] = r["_f"].get("classify_path", "(none)")


def mature_stats(rs):
    n = len(rs)
    if not n:
        return "0件"
    hit = sum(1 for r in rs if r["result_class"] in SUCCESS)
    dang = sum(1 for r in rs if r["result_class"] == "danger_fail")
    tag = " (N小)" if n < 20 else ""
    return f"n={n}{tag} 勝率={hit}/{n}({hit/n*100:.0f}%) danger_fail={dang}({dang/n*100:.0f}%)"


if mature:
    print("  --- カテゴリ別 ---")
    for cat in ["A", "B", "C", "D", "E"]:
        rs = [r for r in mature if r["category"] == cat]
        if rs:
            print(f"   [{cat}] {mature_stats(rs)}")
    print("  --- classify_path別(上位6) ---")
    mpaths: dict = {}
    for r in mature:
        mpaths.setdefault(r["_path"], []).append(r)
    for p, rs in sorted(mpaths.items(), key=lambda kv: -len(kv[1]))[:6]:
        print(f"   {p:20} {mature_stats(rs)}")
    print("  --- material_quality別 ---")
    print(f"   mq>0.3:  {mature_stats([r for r in mature if r['_mq'] > 0.3])}")
    print(f"   mq0-0.3: {mature_stats([r for r in mature if 0 < r['_mq'] <= 0.3])}")
    print(f"   mq=0:    {mature_stats([r for r in mature if r['_mq'] <= 0])}")
    print("  ※ 全体hit_rateと比べて大きく下がる/danger_fail率が上がるpath・mq帯は、")
    print("    「早期に決まらないと危険」な性質を持つ。この時点では条件変更しないが、")
    print("    将来「N日以内に反応がなければ手仕舞い」等の戦略検討材料として記録する。")
else:
    print("  bars>=18到達データがまだありません")
