"""
PAC Sentinel — Build snapshot pre-calcolato
============================================

Legge archivio.json e genera snapshot.json contenente:
- Screener completo (fitness score + Q-Score per ogni ticker)
- Top 14 per Fitness e Top 14 per Q-Score
- Statistiche universe

Lo snapshot viene caricato dal sito web all'apertura — istantaneo.

Usage:
    python build_snapshot.py
"""

import os
import json
import math
import gzip
from datetime import datetime
from pathlib import Path

ARCHIVE_FILE = 'archivio.json'
ARCHIVE_GZ_FILE = 'archivio.json.gz'
SNAPSHOT_FILE = 'snapshot.json'

QSCORE_BENCHMARK = 'QQQ'
TRADING_DAYS_PER_YEAR = 252

# Filtri hard
MIN_YEARS = 5
MAX_ALLOWED_DD = -0.70
MAX_DAYS_SINCE_ATH = 365 * 3


def log(msg):
    ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'[{ts}] {msg}', flush=True)


# === ENGINE PORTATA DA JS ============================================

def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def lin_reg_log_prices(bars):
    """Linear regression of ln(close) vs index. Returns (slope, r2)."""
    n = len(bars)
    if n < 30:
        return None
    xs = list(range(n))
    ys = [math.log(b['c']) if b['c'] > 0 else None for b in bars]
    valid = [(x, y) for x, y in zip(xs, ys) if y is not None]
    if len(valid) < 30:
        return None
    n = len(valid)
    sx = sum(x for x, _ in valid)
    sy = sum(y for _, y in valid)
    sxx = sum(x * x for x, _ in valid)
    sxy = sum(x * y for x, y in valid)
    syy = sum(y * y for _, y in valid)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    # R²
    mean_y = sy / n
    ss_tot = sum((y - mean_y) ** 2 for _, y in valid)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in valid)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return slope, max(0, r2)


def compute_drawdowns(bars):
    """Return list of {peakIdx, dd} and overall maxDD."""
    n = len(bars)
    peak = bars[0]['c']
    peak_idx = 0
    dds = []
    max_dd = 0
    for i, b in enumerate(bars):
        if b['c'] > peak:
            peak = b['c']
            peak_idx = i
        dd = (b['c'] - peak) / peak if peak > 0 else 0
        dds.append({'peakIdx': peak_idx, 'dd': dd})
        if dd < max_dd:
            max_dd = dd
    return dds, max_dd


def recovery_hit_rate(bars, dds, threshold=-0.20, recovery_days=756):
    """% of drawdown episodes ≥ threshold that recovered within recovery_days."""
    n = len(bars)
    episodes = []
    in_dd = False
    start_idx = 0
    for i, d in enumerate(dds):
        if not in_dd and d['dd'] <= threshold:
            in_dd = True
            start_idx = d['peakIdx']
        elif in_dd and d['dd'] >= -0.001:  # recovered (≈0)
            recovered_in = i - start_idx
            episodes.append({'recovered': recovered_in <= recovery_days})
            in_dd = False
    if in_dd:
        # Open episode: not yet recovered
        episodes.append({'recovered': False})
    total = len(episodes)
    if total == 0:
        return {'totalEpisodes': 0, 'hitRate': 1.0}  # nessun crash è considerato "perfetto"
    hit = sum(1 for e in episodes if e['recovered'])
    return {'totalEpisodes': total, 'hitRate': hit / total}


def screen_ticker(bars):
    """Calculate metrics + fitness score 0-100."""
    if not bars or len(bars) < 30:
        return None
    n = len(bars)
    first, last = bars[0], bars[-1]
    years_span = (
        datetime.strptime(last['date'], '%Y-%m-%d')
        - datetime.strptime(first['date'], '%Y-%m-%d')
    ).days / 365.25

    reg = lin_reg_log_prices(bars)
    if not reg:
        return None
    slope, r2 = reg
    cagr = math.exp(slope * 252) - 1
    dds, max_dd = compute_drawdowns(bars)
    rec = recovery_hit_rate(bars, dds, -0.20, 756)
    last_peak_idx = dds[-1]['peakIdx']
    days_since_ath = (
        datetime.strptime(last['date'], '%Y-%m-%d')
        - datetime.strptime(bars[last_peak_idx]['date'], '%Y-%m-%d')
    ).days

    s_cagr = 25 * min(max(cagr, 0) / 0.15, 1)
    s_rec = 25 * rec['hitRate']
    s_r2 = 20 * r2
    s_yrs = 15 * min(years_span / 20, 1)
    s_ath = 10 * max(0, 1 - days_since_ath / 1095)
    s_dd = 5 * max(0, 1 - abs(max_dd) / 0.7)
    fitness = s_cagr + s_rec + s_r2 + s_yrs + s_ath + s_dd

    return {
        'yearsSpan': years_span,
        'cagr': cagr,
        'r2': r2,
        'maxDD': max_dd,
        'daysSinceATH': days_since_ath,
        'recoveryHitRate': rec['hitRate'],
        'recoveryEpisodes': rec['totalEpisodes'],
        'firstDate': first['date'],
        'lastDate': last['date'],
        'barsCount': n,
        'fitness': fitness,
    }


def q_score_lt(bars, bench_bars):
    """Compute Q-Score LT (5 components, 0-100). Needs benchmark bars."""
    if not bars or len(bars) < TRADING_DAYS_PER_YEAR * 2:
        return None
    n = len(bars)
    closes = [b['c'] for b in bars]
    b1y = TRADING_DAYS_PER_YEAR
    b2y = 2 * b1y
    b3y = 3 * b1y

    if n <= b1y:
        return None

    # 1Y return
    p1y = (closes[-1] - closes[-1 - b1y]) / closes[-1 - b1y] * 100 if closes[-1 - b1y] > 0 else None

    # Daily log returns
    dret = []
    for i in range(1, n):
        if closes[i] > 0 and closes[i - 1] > 0:
            dret.append(math.log(closes[i] / closes[i - 1]))
        else:
            dret.append(0)

    # 1Y Sharpe annualized
    dret1y = dret[-b1y:]
    mean1y = sum(dret1y) / len(dret1y) if dret1y else 0
    var = sum((x - mean1y) ** 2 for x in dret1y) / len(dret1y) if dret1y else 0
    std1y = math.sqrt(var)
    sharpe1y = mean1y / std1y * math.sqrt(TRADING_DAYS_PER_YEAR) if std1y > 0 else None

    # CAGR fallback 3Y → 2Y → 1Y
    cagr = None
    if n > b3y and closes[-1 - b3y] > 0:
        cagr = (math.pow(closes[-1] / closes[-1 - b3y], 1 / 3) - 1) * 100
    elif n > b2y and closes[-1 - b2y] > 0:
        cagr = (math.pow(closes[-1] / closes[-1 - b2y], 1 / 2) - 1) * 100
    elif n > b1y and closes[-1 - b1y] > 0:
        cagr = (closes[-1] / closes[-1 - b1y] - 1) * 100

    # Max DD + current DD
    peak = -math.inf
    max_dd = 0
    for c in closes:
        if c > peak:
            peak = c
        dd = (c - peak) / peak * 100 if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd
    ath = peak
    dd_now = (closes[-1] - ath) / ath * 100 if ath > 0 else None

    # Calmar
    calmar = None
    if cagr is not None and abs(max_dd) >= 0.01:
        calmar = cagr / abs(max_dd)

    # Trend persistence
    trend_pct = None
    if n >= b2y + 200:
        above_count = 0
        total = 0
        for i in range(n - b2y, n):
            sma = sum(closes[i - 199:i + 1]) / 200
            if closes[i] > sma:
                above_count += 1
            total += 1
        trend_pct = above_count / total * 100 if total > 0 else None

    # RS vs benchmark
    rs_avg = None
    if bench_bars and len(bench_bars) > b1y:
        bn_close = [b['c'] for b in bench_bars]
        bn = len(bn_close)
        rs_1y = rs_2y = None
        if n - 1 >= b1y and bn - 1 >= b1y:
            t_then = closes[-1 - b1y]
            b_then = bn_close[-1 - b1y]
            if t_then > 0 and b_then > 0:
                rs_1y = (closes[-1] / t_then) / (bn_close[-1] / b_then)
        if n - 1 >= b2y and bn - 1 >= b2y:
            t_then = closes[-1 - b2y]
            b_then = bn_close[-1 - b2y]
            if t_then > 0 and b_then > 0:
                rs_2y = (closes[-1] / t_then) / (bn_close[-1] / b_then)
        if rs_1y is not None and rs_2y is not None:
            rs_avg = (rs_1y + rs_2y) / 2
        elif rs_1y is not None:
            rs_avg = rs_1y
        elif rs_2y is not None:
            rs_avg = rs_2y

    # Score components
    s_qm = 0 if (p1y is None or p1y <= 0 or sharpe1y is None or sharpe1y <= 0) else 25 * clamp((p1y / 100) * sharpe1y / 1.5, 0, 1)
    s_tp = 0 if trend_pct is None else 20 * clamp(trend_pct / 100, 0, 1)
    s_cal = 0 if (calmar is None or calmar <= 0) else 20 * clamp(calmar / 2, 0, 1)
    s_rs = 0 if rs_avg is None else 25 * clamp((rs_avg - 0.8) / 1.7, 0, 1)
    s_dd = 0 if dd_now is None else 10 * clamp(1 + dd_now / 40, 0, 1)
    q_total = s_qm + s_tp + s_cal + s_rs + s_dd
    return q_total


# === MAIN ============================================================

def load_archive():
    if Path(ARCHIVE_GZ_FILE).exists():
        log(f'Loading {ARCHIVE_GZ_FILE}')
        with gzip.open(ARCHIVE_GZ_FILE, 'rt', encoding='utf-8') as f:
            return json.load(f)
    if Path(ARCHIVE_FILE).exists():
        log(f'Loading {ARCHIVE_FILE}')
        with open(ARCHIVE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    raise FileNotFoundError('archivio.json[.gz] non trovato')


def main():
    data = load_archive()
    series = data.get('series', [])
    log(f'Universe: {len(series)} ticker')

    # Find benchmark
    bench_rec = next((r for r in series if r.get('ticker') == QSCORE_BENCHMARK), None)
    bench_bars = bench_rec.get('bars') if bench_rec else None
    if not bench_bars:
        log(f'WARN: benchmark {QSCORE_BENCHMARK} non in archive — Q-Score sarà null')

    results = []
    excluded = {'invalid': 0, 'years': 0, 'maxDD': 0, 'ath': 0}

    for rec in series:
        bars = rec.get('bars')
        if not bars:
            excluded['invalid'] += 1
            continue
        m = screen_ticker(bars)
        if not m:
            excluded['invalid'] += 1
            continue
        if m['yearsSpan'] < MIN_YEARS:
            excluded['years'] += 1
            continue
        if m['maxDD'] < MAX_ALLOWED_DD:
            excluded['maxDD'] += 1
            continue
        if m['daysSinceATH'] > MAX_DAYS_SINCE_ATH:
            excluded['ath'] += 1
            continue

        q = q_score_lt(bars, bench_bars)

        results.append({
            'ticker': rec['ticker'],
            'name': rec.get('name', rec['ticker']),
            'sector': rec.get('sector', '—'),
            **m,
            'qScore': q,
        })

    results.sort(key=lambda x: x['fitness'], reverse=True)

    # Top 14 by fitness and by Q-Score
    top_fitness = sorted(results, key=lambda x: x['fitness'], reverse=True)[:14]
    top_qscore = sorted([r for r in results if r['qScore'] is not None], key=lambda x: x['qScore'], reverse=True)[:14]

    snapshot = {
        'generatedAt': datetime.utcnow().isoformat() + 'Z',
        'universe': len(series),
        'qualifying': len(results),
        'excluded': excluded,
        'totalExcluded': sum(excluded.values()),
        'screener': results,
        'topFitness': top_fitness,
        'topQscore': top_qscore,
        'meta': data.get('meta', {}),
    }

    # Save snapshot
    with open(SNAPSHOT_FILE, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False, separators=(',', ':'))

    size_kb = Path(SNAPSHOT_FILE).stat().st_size / 1024
    log(f'Wrote {SNAPSHOT_FILE} ({size_kb:.1f} KB)')
    log(f'Qualifying: {len(results)} · Excluded: {sum(excluded.values())} ({excluded})')
    log(f'Top fitness: {", ".join(r["ticker"] for r in top_fitness[:5])}')
    log(f'Top Q-Score: {", ".join(r["ticker"] for r in top_qscore[:5])}')


if __name__ == '__main__':
    main()
