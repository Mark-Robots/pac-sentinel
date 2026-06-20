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
from datetime import datetime, timedelta
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


# =========================================================================
# PAC ENGINE — port semplificato del PAC SMART vs CLASSICO
# (versione Python per Monte Carlo rolling, fedele alla logica JS)
# =========================================================================

# Parametri PAC fissi per Monte Carlo (validati dal backtest predittivo)
PAC_PARAMS = {
    'tf': 'week',          # TF medie (W per SMA50/200)
    'tf_acc': 'month',     # cadenza acquisti (mensile)
    'len_ma1': 50,
    'len_ma2': 200,
    'imp_ma1': 100,        # 100€ sotto MA1
    'imp_ma2': 200,        # 200€ sotto MA2 (extra accumulo)
    'rsi_on': False,       # RSI exit disabilitata (validato che penalizza Q-Score picks)
}

# Monte Carlo rolling settings
WINDOW_YEARS = 5           # finestra di 5 anni
STEP_MONTHS = 3            # avanzo ogni 3 mesi (~32 finestre per 10y)
MIN_WINDOWS = 5            # ticker con meno di 5 finestre vengono saltati


def aggregate_weekly(daily_bars):
    """Aggrega barre giornaliere in barre settimanali (W-FRI close)."""
    if not daily_bars:
        return []
    weekly = []
    cur_week = None
    cur_o = cur_h = cur_l = cur_c = None
    cur_date = None
    for b in daily_bars:
        d = datetime.strptime(b['date'], '%Y-%m-%d')
        # ISO week: lunedì=1, domenica=7 → week starts Monday
        week_key = (d.isocalendar()[0], d.isocalendar()[1])
        if cur_week is None or week_key != cur_week:
            if cur_week is not None:
                weekly.append({'date': cur_date, 'o': cur_o, 'h': cur_h, 'l': cur_l, 'c': cur_c})
            cur_week = week_key
            cur_o = b['o']
            cur_h = b['h']
            cur_l = b['l']
            cur_c = b['c']
            cur_date = b['date']
        else:
            cur_h = max(cur_h, b['h'])
            cur_l = min(cur_l, b['l'])
            cur_c = b['c']
            cur_date = b['date']
    if cur_week is not None:
        weekly.append({'date': cur_date, 'o': cur_o, 'h': cur_h, 'l': cur_l, 'c': cur_c})
    return weekly


def sma(values, length):
    """Simple Moving Average. Returns list aligned with input (NaN before length-1)."""
    if length <= 0 or len(values) < length:
        return [None] * len(values)
    out = [None] * (length - 1)
    s = sum(values[:length])
    out.append(s / length)
    for i in range(length, len(values)):
        s += values[i] - values[i - length]
        out.append(s / length)
    return out


def find_bar_at_or_before(bars, target_date_str):
    """Find index of last bar with date <= target_date_str. Returns -1 if none."""
    lo, hi = 0, len(bars) - 1
    if not bars or bars[0]['date'] > target_date_str:
        return -1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if bars[mid]['date'] <= target_date_str:
            lo = mid
        else:
            hi = mid - 1
    return lo


def run_pac_backtest(daily_bars, params=PAC_PARAMS):
    """
    Simula PAC SMART e PAC CLASSICO sui bars giornalieri forniti.
    Ritorna {'smart_ret', 'trad_ret', 'smart_winner'} con rendimenti in percentuale.

    Logica fedele al motore JS:
    - SMART: ogni mese controlla SMA50 e SMA200 (calcolate su barre weekly).
      Se prezzo < SMA1 → compra imp_ma1.
      Se prezzo < SMA2 → compra imp_ma1 + imp_ma2 (totale extra).
      Altrimenti niente.
    - CLASSICO: ogni mese investe la media (= capitale_smart / n_mesi).
      Stesso capitale totale, distribuito uniformemente.
    """
    if not daily_bars or len(daily_bars) < 60:
        return None

    # Aggrega in settimanali per il calcolo MA (come PAC Sentinel)
    weekly = aggregate_weekly(daily_bars)
    if len(weekly) < params['len_ma2']:
        return None

    weekly_closes = [b['c'] for b in weekly]
    ma1 = sma(weekly_closes, params['len_ma1'])
    ma2 = sma(weekly_closes, params['len_ma2'])

    # Crea indice per lookup veloce date→index
    weekly_date_to_idx = {b['date']: i for i, b in enumerate(weekly)}

    # Itera mese per mese: trova l'ultima seduta di ogni mese
    months = {}
    for i, b in enumerate(daily_bars):
        ym = b['date'][:7]  # 'YYYY-MM'
        months[ym] = i  # sovrascrive → ultimo bar del mese
    month_indices = sorted(months.values())
    if len(month_indices) < 2:
        return None

    # Simula SMART
    smart_invested = 0.0
    smart_shares = 0.0
    for didx in month_indices:
        bar = daily_bars[didx]
        price = bar['c']
        # Trova SMA più vicine alla data
        wi = find_bar_at_or_before(weekly, bar['date'])
        if wi < 0:
            continue
        cur_ma1 = ma1[wi] if wi < len(ma1) else None
        cur_ma2 = ma2[wi] if wi < len(ma2) else None
        if cur_ma1 is None or cur_ma2 is None:
            continue

        amount = 0.0
        if price < cur_ma2:
            amount = params['imp_ma1'] + params['imp_ma2']
        elif price < cur_ma1:
            amount = params['imp_ma1']
        # else: niente acquisto

        if amount > 0:
            smart_invested += amount
            smart_shares += amount / price

    # Valore finale SMART
    last_price = daily_bars[-1]['c']
    smart_final_value = smart_shares * last_price
    smart_ret_pct = (smart_final_value - smart_invested) / smart_invested * 100 if smart_invested > 0 else None

    # Simula CLASSICO: stesso capitale totale (smart_invested), distribuito uniformemente
    if not smart_invested or len(month_indices) == 0:
        trad_ret_pct = None
    else:
        trad_per_month = smart_invested / len(month_indices)
        trad_shares = 0.0
        for didx in month_indices:
            price = daily_bars[didx]['c']
            if price > 0:
                trad_shares += trad_per_month / price
        trad_final_value = trad_shares * last_price
        trad_ret_pct = (trad_final_value - smart_invested) / smart_invested * 100 if smart_invested > 0 else None

    if smart_ret_pct is None or trad_ret_pct is None:
        return None

    return {
        'smart_ret': smart_ret_pct,
        'trad_ret': trad_ret_pct,
        'smart_won': smart_ret_pct > trad_ret_pct,
        'invested': smart_invested,
    }


# =========================================================================
# MONTE CARLO ROLLING — simula PAC su finestre rolling di 5 anni
# =========================================================================

def slice_bars_by_date(bars, start_date, end_date):
    """Subset di bars con start_date <= date <= end_date."""
    out = []
    for b in bars:
        if start_date <= b['date'] <= end_date:
            out.append(b)
    return out


def percentile(sorted_values, p):
    """Percentile p (0-100) di una lista già ordinata."""
    if not sorted_values:
        return None
    if p <= 0:
        return sorted_values[0]
    if p >= 100:
        return sorted_values[-1]
    k = (len(sorted_values) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def rolling_monte_carlo(bars, window_years=WINDOW_YEARS, step_months=STEP_MONTHS):
    """
    Per un ticker, genera finestre rolling di N anni avanzando di M mesi
    e simula PAC SMART + CLASSICO su ognuna.
    Ritorna dict con statistiche aggregate.
    """
    if not bars or len(bars) < 252 * window_years:
        return None  # storia insufficiente

    first_date = datetime.strptime(bars[0]['date'], '%Y-%m-%d')
    last_date = datetime.strptime(bars[-1]['date'], '%Y-%m-%d')

    # Inizio finestra: dal primo bar
    window_days = int(window_years * 365.25)
    step_days = int(step_months * 30.4)

    smart_returns = []
    trad_returns = []
    smart_wins = 0

    cursor_start = first_date
    while True:
        cursor_end = cursor_start + timedelta(days=window_days)
        if cursor_end > last_date:
            break  # finestra incompleta
        start_str = cursor_start.strftime('%Y-%m-%d')
        end_str = cursor_end.strftime('%Y-%m-%d')
        window_bars = slice_bars_by_date(bars, start_str, end_str)
        if len(window_bars) >= 252 * (window_years - 1):  # almeno 4 anni di dati nella finestra
            res = run_pac_backtest(window_bars)
            if res:
                smart_returns.append(res['smart_ret'])
                trad_returns.append(res['trad_ret'])
                if res['smart_won']:
                    smart_wins += 1
        cursor_start = cursor_start + timedelta(days=step_days)

    n = len(smart_returns)
    if n < MIN_WINDOWS:
        return None

    # Statistiche SMART
    smart_sorted = sorted(smart_returns)
    smart_median = percentile(smart_sorted, 50)
    smart_mean = sum(smart_returns) / n
    smart_worst5 = percentile(smart_sorted, 5)
    smart_best5 = percentile(smart_sorted, 95)
    # Std dev manuale
    smart_var = sum((x - smart_mean) ** 2 for x in smart_returns) / n
    smart_std = math.sqrt(smart_var)

    # Win rate vs CLASSICO
    win_rate = smart_wins / n * 100

    # Consistency score: mediana / (std / |mediana|) — più alto = più consistente
    # Versione semplice: mediana penalizzata dalla volatilità
    if smart_std > 0 and smart_median > 0:
        consistency = smart_median / (1 + smart_std / abs(smart_median))
    elif smart_median is not None:
        consistency = smart_median  # niente penalità se std=0 (improbabile ma safe)
    else:
        consistency = 0

    # % finestre positive (SMART > 0)
    positive_pct = sum(1 for x in smart_returns if x > 0) / n * 100

    return {
        'n_windows': n,
        'smart_median': smart_median,
        'smart_mean': smart_mean,
        'smart_std': smart_std,
        'smart_worst5': smart_worst5,
        'smart_best5': smart_best5,
        'trad_median': percentile(sorted(trad_returns), 50),
        'win_rate_vs_classico': win_rate,
        'positive_pct': positive_pct,
        'consistency': consistency,
    }


# =========================================================================
# MAIN
# =========================================================================

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

    log('=== STAGE 1: Screening (fitness + Q-Score) ===')
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
            'bars_ref': rec,  # tieni riferimento per stage 2
        })

    log(f'Stage 1 done: {len(results)} qualifying, {sum(excluded.values())} excluded')

    log('=== STAGE 2: Monte Carlo rolling (5y windows, step 3 months) ===')
    log(f'  Universe: {len(results)} ticker · window {WINDOW_YEARS}y · step {STEP_MONTHS}m')
    mc_completed = 0
    mc_skipped = 0
    t0 = datetime.utcnow()
    for i, r in enumerate(results):
        rec = r.pop('bars_ref')  # rimuovi riferimento bars (non serializzabile)
        bars = rec.get('bars', [])
        try:
            mc = rolling_monte_carlo(bars)
            if mc:
                r['mc'] = mc
                mc_completed += 1
            else:
                r['mc'] = None
                mc_skipped += 1
        except Exception as e:
            log(f'  MC failed for {r["ticker"]}: {e}', )
            r['mc'] = None
            mc_skipped += 1
        if (i + 1) % 50 == 0:
            elapsed = (datetime.utcnow() - t0).total_seconds()
            log(f'  Progress: {i+1}/{len(results)} · {elapsed:.0f}s elapsed')
    elapsed = (datetime.utcnow() - t0).total_seconds()
    log(f'Stage 2 done in {elapsed:.0f}s: {mc_completed} completed, {mc_skipped} skipped')

    # Ordina lo screener per fitness di default
    results.sort(key=lambda x: x['fitness'], reverse=True)

    # Top 14 by fitness and by Q-Score
    top_fitness = sorted(results, key=lambda x: x['fitness'], reverse=True)[:14]
    top_qscore = sorted([r for r in results if r['qScore'] is not None], key=lambda x: x['qScore'], reverse=True)[:14]

    # Top per Monte Carlo consistency (la sezione "PICK FINALE")
    mc_results = [r for r in results if r.get('mc') is not None]
    top_mc_consistency = sorted(
        mc_results,
        key=lambda x: x['mc']['consistency'],
        reverse=True
    )[:20]
    # Top per win-rate vs CLASSICO (utili per validare che SMART funziona)
    top_mc_winrate = sorted(
        mc_results,
        key=lambda x: x['mc']['win_rate_vs_classico'],
        reverse=True
    )[:20]

    snapshot = {
        'generatedAt': datetime.utcnow().isoformat() + 'Z',
        'universe': len(series),
        'qualifying': len(results),
        'excluded': excluded,
        'totalExcluded': sum(excluded.values()),
        'screener': results,
        'topFitness': top_fitness,
        'topQscore': top_qscore,
        'topMcConsistency': top_mc_consistency,
        'topMcWinrate': top_mc_winrate,
        'mcConfig': {
            'windowYears': WINDOW_YEARS,
            'stepMonths': STEP_MONTHS,
            'pacParams': PAC_PARAMS,
        },
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
    log(f'Top MC consistency: {", ".join(r["ticker"] for r in top_mc_consistency[:5])}')
    log(f'Top MC win-rate vs CLASSICO: {", ".join(r["ticker"] for r in top_mc_winrate[:5])}')


if __name__ == '__main__':
    main()
