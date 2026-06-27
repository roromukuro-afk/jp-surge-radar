"""
Initial price fetch for all 3000-yen-or-less target stocks.
Runs as a GitHub Actions bootstrap job (timeout: 6h).

Usage: python scripts/bootstrap_prices.py [batch_start] [batch_size] [price_range]
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

batch_start = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip().isdigit() else 0
batch_size  = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].strip().isdigit() else 500
price_range = sys.argv[3].strip() if len(sys.argv) > 3 and sys.argv[3].strip() else "2y"

from surge_radar import db, ingest, universe

db.init_db()

all_codes = universe.get_target_codes()
batch = all_codes[batch_start : batch_start + batch_size]

print(f"bootstrap: universe={len(all_codes)} batch=[{batch_start}:{batch_start+batch_size}] "
      f"actual={len(batch)} range={price_range}", flush=True)

stale = ingest.stale_codes(batch, stale_days=1)
print(f"stale (need fetch): {len(stale)} / {len(batch)}", flush=True)

if not stale:
    print("All stocks in batch already have recent prices. Done.", flush=True)
    sys.exit(0)

t0 = time.monotonic()
ok = 0; fail = 0; rows = 0
failed_codes = []

for i, code in enumerate(stale, 1):
    try:
        n = ingest.fetch_one(code, range_=price_range)
        if n > 0:
            ok += 1; rows += n
        else:
            fail += 1; failed_codes.append(code)
    except Exception as e:
        fail += 1; failed_codes.append(code)
        print(f"    FAIL {code}: {e}", flush=True)

    if i % 20 == 0:
        elapsed = time.monotonic() - t0
        rate = i / elapsed if elapsed > 0 else 0
        eta = (len(stale) - i) / rate if rate > 0 else 0
        print(f"  [{i}/{len(stale)}] ok={ok} fail={fail} rows={rows} "
              f"elapsed={elapsed:.0f}s eta={eta:.0f}s", flush=True)

    time.sleep(0.25)

elapsed = time.monotonic() - t0
print(f"bootstrap done: ok={ok} fail={fail} rows={rows} elapsed={elapsed:.0f}s", flush=True)
if failed_codes:
    print(f"failed codes ({len(failed_codes)}): {failed_codes[:50]}", flush=True)
