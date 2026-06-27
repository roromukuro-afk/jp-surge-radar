"""Audit material data quality by source. No secrets printed."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _envload import load_env
load_env()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from surge_radar import db

with db.cursor() as conn:
    def rows(q, *a):
        return conn.execute(q, a).fetchall()
    def one(q, *a):
        return conn.execute(q, a).fetchone()

    print("=== by source ===")
    for r in rows("SELECT source, COUNT(*) n FROM materials GROUP BY source ORDER BY n DESC"):
        print(f"  {r['source']:12} {r['n']}")

    tot = one("SELECT COUNT(*) n FROM materials")["n"]
    print(f"total: {tot}")

    print("=== linkage / url validity ===")
    linked = one("SELECT COUNT(*) n FROM materials WHERE code IS NOT NULL AND code<>''")["n"]
    url_ok = one("SELECT COUNT(*) n FROM materials WHERE url LIKE %s", "http%")["n"]
    print(f"  code linked: {linked}/{tot}")
    print(f"  http url:    {url_ok}/{tot}")

    print("=== category (material_type proxy) top 15 ===")
    for r in rows("SELECT category, COUNT(*) n FROM materials GROUP BY category ORDER BY n DESC LIMIT 15"):
        print(f"  {str(r['category'])[:40]:40} {r['n']}")

    print("=== scoring field coverage (non-null & non-zero) ===")
    for col in ["sentiment", "impact", "persistence", "unpriced", "connect"]:
        nn = one(f"SELECT COUNT(*) n FROM materials WHERE {col} IS NOT NULL")["n"]
        nz = one(f"SELECT COUNT(*) n FROM materials WHERE {col} IS NOT NULL AND {col}<>0")["n"]
        print(f"  {col:12} non-null={nn:5} non-zero={nz:5}")

    print("=== body present ===")
    body = one("SELECT COUNT(*) n FROM materials WHERE body IS NOT NULL AND body<>''")["n"]
    print(f"  body: {body}/{tot}")

    print("=== date range ===")
    dr = one("SELECT MIN(date) lo, MAX(date) hi FROM materials")
    print(f"  {dr['lo']} -> {dr['hi']}")

    print("=== sample per source (title + url head) ===")
    for s in [r["source"] for r in rows("SELECT DISTINCT source FROM materials")]:
        samp = one("SELECT code,title,url,category,sentiment,impact,persistence FROM materials WHERE source=%s ORDER BY date DESC LIMIT 1", s)
        if samp:
            print(f"  [{s}] code={samp['code']} cat={samp['category']} sent={samp['sentiment']} imp={samp['impact']} pers={samp['persistence']}")
            print(f"        title: {str(samp['title'])[:70]}")
            print(f"        url:   {str(samp['url'])[:70]}")

    print("=== columns present in schema ===")
    cols = rows("SELECT column_name FROM information_schema.columns WHERE table_name='materials' ORDER BY ordinal_position")
    print("  " + ", ".join(c["column_name"] for c in cols))
