"""
PAC Sentinel — Sync giornaliero standalone
==========================================

Script da eseguire da GitHub Actions ogni notte.
Scarica le barre nuove da Twelve Data per i 322 ticker dell'universo
e aggiorna archivio.json sul disco.

Variabili d'ambiente richieste:
    TWELVE_DATA_KEY    API key di Twelve Data (da GitHub Secrets)

Usage:
    python sync_daily.py

Output:
    archivio.json     aggiornato in place

Rate limit gestito: 8s tra chiamate (~7.5/min, sotto il limite di 8/min)
Cap giornaliero: 800 chiamate (free tier). Lo script si ferma se raggiunto.
"""

import os
import sys
import json
import time
import gzip
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# === CONFIG ============================================================
ARCHIVE_FILE = 'archivio.json'
ARCHIVE_GZ_FILE = 'archivio.json.gz'
TD_RATE_LIMIT_SEC = 8       # 8s = 7.5/min, sotto il limite di 8/min
TD_DAILY_CAP = 800           # free tier hard cap
HISTORY_YEARS = 10           # quanto guardare indietro al primo sync

# Universe (deve corrispondere a quello dell'HTML)
# Per ora hardcoded qui — in futuro estraibile da un file condiviso
UNIVERSE = [
    # Si veda l'HTML pac_sentinel.html — qui un subset di esempio
    # Generato programmaticamente sotto da SECTOR_TICKERS
]

# Per evitare di duplicare l'universo, lo leggiamo qui sotto da una struttura
# che replica quella dell'HTML. Se cambia l'universo nell'HTML, va aggiornato
# anche qui (è documentato nel README).

SECTOR_TICKERS = {
    'Technology':    ['AAPL','MSFT','NVDA','AVGO','ORCL','ADBE','CRM','ACN','CSCO','AMD','IBM','INTC','TXN','QCOM','NOW','INTU','KLAC','LRCX','AMAT','MU','ADI','MRVL','SNPS','CDNS','ROP','MSI','FICO','MCHP','DELL','GLW','HPQ','HPE','NTAP','JBL','CDW','ON','ANSS','JNPR','AKAM','TER','GRMN','TYL','FFIV','PTC','TRMB','KEYS','NXPI','SWKS','STX','WDC','CTSH'],
    'Healthcare':    ['LLY','JNJ','UNH','ABBV','MRK','PFE','TMO','ABT','DHR','ISRG','BMY','AMGN','GILD','CVS','MDT','CI','ELV','SYK','REGN','VRTX','BSX','MCK','BDX','CNC','HCA','HUM','ZTS','BIIB','IDXX','IQV','EW','DXCM','RMD','COR','LH','DGX','ALGN','INCY','WST','BAX'],
    'Financials':    ['BRK-B','JPM','V','MA','BAC','WFC','GS','MS','C','BLK','SCHW','AXP','USB','PNC','TFC','COF','BK','FITB','MTB','KEY','NTRS','RJF','CME','ICE','NDAQ','CBOE','MCO','SPGI','MSCI','AON','MMC','AIG','MET','PRU','AFL','PGR','TRV','CB','HIG','ALL'],
    'Cons. Disc.':   ['AMZN','TSLA','HD','MCD','NKE','LOW','SBUX','BKNG','TJX','MAR','GM','F','ORLY','AZO','CMG','YUM','ROST','RCL','CCL','EBAY','LULU','ULTA','BBY','TSCO','DPZ','GPC','KMX','EXPE','HLT','TPR'],
    'Communication': ['GOOGL','GOOG','META','NFLX','DIS','T','VZ','CMCSA','TMUS','EA','TTWO','CHTR','OMC','IPG','LYV'],
    'Industrials':   ['GE','RTX','HON','UPS','CAT','BA','DE','ETN','LMT','NOC','GD','MMM','EMR','ITW','PH','CSX','NSC','UNP','FDX','WM','RSG','GWW','FAST','JCI','PCAR','IR','SWK','CMI','DAL','UAL','AAL','LUV','EXPD','ODFL','ADP','PAYX','DOV','XYL','SNA','CPRT'],
    'Cons. Staples': ['PG','KO','PEP','WMT','COST','MDLZ','PM','MO','KHC','GIS','K','KR','KMB','CHD','CL','EL','CLX','HSY','HRL','CAG','MNST','KDP','ADM','TSN','STZ'],
    'Energy':        ['XOM','CVX','COP','EOG','SLB','MPC','PSX','VLO','OXY','HES','DVN','BKR','HAL','KMI','OKE'],
    'Materials':     ['LIN','APD','SHW','ECL','FCX','NEM','NUE','MLM','VMC','DOW','DD','PPG','IFF','BALL','IP'],
    'Real Estate':   ['PLD','AMT','EQIX','PSA','WELL','DLR','O','SPG','CCI','AVB','EQR','VTR'],
    'Utilities':     ['NEE','SO','DUK','AEP','SRE','EXC','XEL','PCG','EIX','WEC','DTE','PPL','ED','AWK','ETR'],
    'ETF':           ['SPY','VOO','IVV','VTI','QQQ','IWM','IJR','IJH','VTV','VUG','VYM','VIG','SCHD','EFA','VEA','VWO','EEM','GLD','SLV','BND','AGG','VNQ','XLK','XLF','XLV','XLE','XLI','XLP','XLY','XLU'],
}

# Build universe with simple names
UNIVERSE = []
TICKER_NAMES = {
    # Aggiunti i nomi principali — i mancanti useranno il ticker come fallback
    'AAPL':'Apple','MSFT':'Microsoft','NVDA':'Nvidia','AVGO':'Broadcom','ORCL':'Oracle','ADBE':'Adobe',
    'CRM':'Salesforce','ACN':'Accenture','CSCO':'Cisco','AMD':'AMD','IBM':'IBM','INTC':'Intel',
    'TXN':'Texas Instruments','QCOM':'Qualcomm','NOW':'ServiceNow','INTU':'Intuit','KLAC':'KLA',
    'LRCX':'Lam Research','AMAT':'Applied Materials','MU':'Micron','ADI':'Analog Devices',
    'MRVL':'Marvell','SNPS':'Synopsys','CDNS':'Cadence','ROP':'Roper','MSI':'Motorola Solutions',
    'FICO':'Fair Isaac','MCHP':'Microchip','DELL':'Dell Technologies','GLW':'Corning','HPQ':'HP',
    'HPE':'HPE','NTAP':'NetApp','JBL':'Jabil','CDW':'CDW','ON':'ON Semiconductor','ANSS':'Ansys',
    'JNPR':'Juniper','AKAM':'Akamai','TER':'Teradyne','GRMN':'Garmin','TYL':'Tyler Tech',
    'FFIV':'F5','PTC':'PTC','TRMB':'Trimble','KEYS':'Keysight','NXPI':'NXP','SWKS':'Skyworks',
    'STX':'Seagate','WDC':'Western Digital','CTSH':'Cognizant',
    'LLY':'Eli Lilly','JNJ':'Johnson & Johnson','UNH':'UnitedHealth','ABBV':'AbbVie','MRK':'Merck',
    'PFE':'Pfizer','TMO':'Thermo Fisher','ABT':'Abbott','DHR':'Danaher','ISRG':'Intuitive Surgical',
    'BMY':'Bristol Myers','AMGN':'Amgen','GILD':'Gilead','CVS':'CVS','MDT':'Medtronic',
    'CI':'Cigna','ELV':'Elevance','SYK':'Stryker','REGN':'Regeneron','VRTX':'Vertex',
    'BSX':'Boston Scientific','MCK':'McKesson','BDX':'BD','CNC':'Centene','HCA':'HCA',
    'HUM':'Humana','ZTS':'Zoetis','BIIB':'Biogen','IDXX':'IDEXX Laboratories','IQV':'IQVIA',
    'EW':'Edwards Lifesciences','DXCM':'Dexcom','RMD':'ResMed','COR':'Cencora','LH':'LabCorp',
    'DGX':'Quest Diagnostics','ALGN':'Align','INCY':'Incyte','WST':'West Pharmaceutical',
    'BAX':'Baxter',
    'BRK-B':'Berkshire B','JPM':'JPMorgan','V':'Visa','MA':'Mastercard','BAC':'Bank of America',
    'WFC':'Wells Fargo','GS':'Goldman Sachs','MS':'Morgan Stanley','C':'Citigroup','BLK':'BlackRock',
    'SCHW':'Schwab','AXP':'American Express','USB':'US Bank','PNC':'PNC','TFC':'Truist',
    'COF':'Capital One','BK':'BNY Mellon','FITB':'Fifth Third','MTB':'M&T Bank','KEY':'KeyCorp',
    'NTRS':'Northern Trust','RJF':'Raymond James','CME':'CME Group','ICE':'ICE','NDAQ':'Nasdaq',
    'CBOE':'Cboe','MCO':'Moody\'s','SPGI':'S&P Global','MSCI':'MSCI Inc','AON':'Aon plc',
    'MMC':'Marsh McLennan','AIG':'AIG','MET':'MetLife','PRU':'Prudential','AFL':'Aflac',
    'PGR':'Progressive','TRV':'Travelers','CB':'Chubb','HIG':'Hartford','ALL':'Allstate',
    'AMZN':'Amazon','TSLA':'Tesla','HD':'Home Depot','MCD':'McDonald\'s','NKE':'Nike',
    'LOW':'Lowe\'s','SBUX':'Starbucks','BKNG':'Booking','TJX':'TJX Companies','MAR':'Marriott',
    'GM':'General Motors','F':'Ford','ORLY':'O\'Reilly Automotive','AZO':'AutoZone',
    'CMG':'Chipotle','YUM':'Yum! Brands','ROST':'Ross Stores','RCL':'Royal Caribbean',
    'CCL':'Carnival','EBAY':'eBay','LULU':'Lululemon','ULTA':'Ulta Beauty','BBY':'Best Buy',
    'TSCO':'Tractor Supply','DPZ':'Domino\'s','GPC':'Genuine Parts','KMX':'CarMax',
    'EXPE':'Expedia','HLT':'Hilton Worldwide','TPR':'Tapestry',
    'GOOGL':'Alphabet Class A','GOOG':'Alphabet Class C','META':'Meta Platforms','NFLX':'Netflix',
    'DIS':'Disney','T':'AT&T','VZ':'Verizon','CMCSA':'Comcast','TMUS':'T-Mobile US',
    'EA':'EA','TTWO':'Take-Two','CHTR':'Charter','OMC':'Omnicom','IPG':'Interpublic',
    'LYV':'Live Nation',
    'GE':'General Electric','RTX':'RTX','HON':'Honeywell','UPS':'UPS','CAT':'Caterpillar',
    'BA':'Boeing','DE':'Deere','ETN':'Eaton','LMT':'Lockheed Martin','NOC':'Northrop',
    'GD':'General Dynamics','MMM':'3M','EMR':'Emerson','ITW':'Illinois Tool Works',
    'PH':'Parker-Hannifin','CSX':'CSX','NSC':'Norfolk Southern','UNP':'Union Pacific',
    'FDX':'FedEx','WM':'Waste Management','RSG':'Republic Services','GWW':'Grainger',
    'FAST':'Fastenal','JCI':'Johnson Controls','PCAR':'Paccar','IR':'Ingersoll Rand',
    'SWK':'Stanley Black','CMI':'Cummins','DAL':'Delta','UAL':'United Airlines','AAL':'American Airlines',
    'LUV':'Southwest','EXPD':'Expeditors','ODFL':'Old Dominion Freight','ADP':'ADP',
    'PAYX':'Paychex','DOV':'Dover','XYL':'Xylem','SNA':'Snap-on','CPRT':'Copart',
    'PG':'Procter & Gamble','KO':'Coca-Cola','PEP':'Pepsi','WMT':'Walmart','COST':'Costco',
    'MDLZ':'Mondelez','PM':'Philip Morris','MO':'Altria','KHC':'Kraft Heinz','GIS':'General Mills',
    'K':'Kellogg','KR':'Kroger','KMB':'Kimberly-Clark','CHD':'Church & Dwight','CL':'Colgate',
    'EL':'Estée Lauder','CLX':'Clorox','HSY':'Hershey','HRL':'Hormel','CAG':'Conagra',
    'MNST':'Monster Beverage','KDP':'Keurig Dr Pepper','ADM':'ADM','TSN':'Tyson','STZ':'Constellation',
    'XOM':'Exxon','CVX':'Chevron','COP':'ConocoPhillips','EOG':'EOG Resources','SLB':'Schlumberger',
    'MPC':'Marathon Petroleum','PSX':'Phillips 66','VLO':'Valero','OXY':'Occidental',
    'HES':'Hess','DVN':'Devon','BKR':'Baker Hughes','HAL':'Halliburton','KMI':'Kinder Morgan',
    'OKE':'ONEOK',
    'LIN':'Linde','APD':'Air Products','SHW':'Sherwin-Williams','ECL':'Ecolab','FCX':'Freeport',
    'NEM':'Newmont','NUE':'Nucor','MLM':'Martin Marietta','VMC':'Vulcan Materials','DOW':'Dow',
    'DD':'DuPont','PPG':'PPG','IFF':'IFF','BALL':'Ball Corp','IP':'International Paper',
    'PLD':'Prologis','AMT':'American Tower','EQIX':'Equinix','PSA':'Public Storage','WELL':'Welltower',
    'DLR':'Digital Realty','O':'Realty Income','SPG':'Simon Property','CCI':'Crown Castle',
    'AVB':'AvalonBay','EQR':'Equity Residential','VTR':'Ventas',
    'NEE':'NextEra','SO':'Southern','DUK':'Duke Energy','AEP':'AEP','SRE':'Sempra',
    'EXC':'Exelon','XEL':'Xcel','PCG':'PG&E','EIX':'Edison Intl','WEC':'WEC Energy',
    'DTE':'DTE Energy','PPL':'PPL','ED':'Con Edison','AWK':'American Water','ETR':'Entergy',
    'SPY':'SPDR S&P 500','VOO':'Vanguard S&P 500','IVV':'iShares S&P 500','VTI':'Vanguard Total Market',
    'QQQ':'Invesco QQQ','IWM':'iShares Russell 2000','IJR':'iShares S&P Small','IJH':'iShares S&P Mid',
    'VTV':'Vanguard Value','VUG':'Vanguard Growth','VYM':'Vanguard High Div','VIG':'Vanguard Div Appr',
    'SCHD':'Schwab Div','EFA':'iShares EAFE','VEA':'Vanguard Developed','VWO':'Vanguard EM',
    'EEM':'iShares EM','GLD':'SPDR Gold','SLV':'iShares Silver','BND':'Vanguard Bond',
    'AGG':'iShares Bond','VNQ':'Vanguard REIT','XLK':'Tech Select SPDR','XLF':'Fin Select SPDR',
    'XLV':'Health Select SPDR','XLE':'Energy Select SPDR','XLI':'Indl Select SPDR',
    'XLP':'Staples Select SPDR','XLY':'Disc Select SPDR','XLU':'Util Select SPDR',
}

for sector, tickers in SECTOR_TICKERS.items():
    for tk in tickers:
        UNIVERSE.append({
            'ticker': tk,
            'name': TICKER_NAMES.get(tk, tk),
            'sector': sector,
        })

# === HELPERS ============================================================

def log(msg, level='INFO'):
    """Stampa con timestamp UTC, flush immediato per CI logs."""
    ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'[{ts}] [{level}] {msg}', flush=True)


def today_str():
    return datetime.utcnow().strftime('%Y-%m-%d')


def history_start_str():
    d = datetime.utcnow() - timedelta(days=HISTORY_YEARS * 365 + 30)
    return d.strftime('%Y-%m-%d')


def fetch_twelve_data(symbol, start_date, api_key):
    """
    Chiama Twelve Data /time_series e ritorna lista di bars {date, o, h, l, c, v}.

    Returns:
        list[dict]: barre giornaliere (anche [] se range vuoto, weekend, festività)

    Raises:
        RuntimeError: solo per errori veri (rate limit, simbolo non trovato, network).
        Gli HTTP 400 con "no data" sono trattati come empty result (non errore).
    """
    today = today_str()
    # Twelve Data uses dots not dashes for share classes (BRK.B not BRK-B)
    td_symbol = symbol.replace('-', '.')
    url = (
        'https://api.twelvedata.com/time_series'
        f'?symbol={td_symbol}'
        f'&interval=1day'
        f'&start_date={start_date}'
        f'&end_date={today}'
        f'&format=JSON'
        f'&outputsize=5000'
        f'&apikey={api_key}'
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        # HTTP 400 often means "no data in range" (weekend, future date) — not a real error
        if e.code == 400:
            try:
                err_body = e.read().decode('utf-8')
                err_data = json.loads(err_body)
                err_msg = (err_data.get('message') or '').lower()
                if any(s in err_msg for s in ['no data', 'no trading days', 'invalid date', 'start_date', 'end_date']):
                    return []  # empty range, not an error
                # Other 400: re-raise with context
                raise RuntimeError(f'TD HTTP 400: {err_msg[:120]}')
            except (json.JSONDecodeError, AttributeError):
                # 400 without parsable body → assume no data
                return []
        elif e.code == 404:
            raise RuntimeError(f'Symbol {td_symbol} not found (HTTP 404)')
        elif e.code == 429:
            raise RuntimeError('TD rate limit raggiunto (HTTP 429)')
        else:
            raise RuntimeError(f'HTTP error {e.code}: {e}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'Network error: {e}')

    data = json.loads(raw)
    if data.get('status') == 'error' or data.get('code'):
        msg = data.get('message') or str(data.get('code', 'unknown error'))
        msg_low = msg.lower()
        if any(s in msg_low for s in ['rate limit', 'exceeded', 'limit reached', 'run out']):
            raise RuntimeError('TD rate limit raggiunto')
        if any(s in msg_low for s in ['no data', 'no trading days', 'invalid date', 'out of range']):
            return []  # empty range, not an error
        raise RuntimeError(f'TD error: {msg[:120]}')

    values = data.get('values', [])
    if not isinstance(values, list):
        return []  # malformed response, treat as no data

    # TD returns DESC dates → flip to ASC
    bars = []
    for b in values:
        try:
            bars.append({
                'date': (b['datetime'] or '')[:10],
                'o': float(b['open']),
                'h': float(b['high']),
                'l': float(b['low']),
                'c': float(b['close']),
                'v': int(b.get('volume', 0)),
            })
        except (KeyError, TypeError, ValueError):
            continue
    bars = [b for b in bars if b['date'] and not (b['c'] != b['c'])]  # filter NaN
    bars.sort(key=lambda x: x['date'])
    return bars


def add_days(date_str, n):
    d = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=n)
    return d.strftime('%Y-%m-%d')


def load_archive():
    """Carica archivio.json (o .gz) se esiste, altrimenti restituisce struttura vuota."""
    if Path(ARCHIVE_GZ_FILE).exists():
        log(f'Loading {ARCHIVE_GZ_FILE} (compressed)')
        with gzip.open(ARCHIVE_GZ_FILE, 'rt', encoding='utf-8') as f:
            return json.load(f)
    if Path(ARCHIVE_FILE).exists():
        log(f'Loading {ARCHIVE_FILE}')
        with open(ARCHIVE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    log('No archive found, starting fresh', 'WARN')
    return {'series': [], 'meta': {}}


def save_archive(data):
    """Scrive archivio.json (uncompressed, per script consume) e archivio.json.gz (per web)."""
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    with open(ARCHIVE_FILE, 'w', encoding='utf-8') as f:
        f.write(payload)
    raw_size = len(payload) / 1024 / 1024
    log(f'Wrote {ARCHIVE_FILE} ({raw_size:.1f} MB)')

    with gzip.open(ARCHIVE_GZ_FILE, 'wt', encoding='utf-8', compresslevel=9) as f:
        f.write(payload)
    gz_size = Path(ARCHIVE_GZ_FILE).stat().st_size / 1024 / 1024
    log(f'Wrote {ARCHIVE_GZ_FILE} ({gz_size:.1f} MB compressed)')


# === MAIN ============================================================

def main():
    api_key = os.environ.get('TWELVE_DATA_KEY', '').strip()
    if not api_key:
        log('TWELVE_DATA_KEY environment variable not set', 'ERROR')
        sys.exit(1)

    log(f'Sync starting: universe = {len(UNIVERSE)} tickers')

    data = load_archive()
    series_by_ticker = {r['ticker']: r for r in data.get('series', [])}

    calls_made = 0
    updated = 0
    skipped = 0
    errors = 0
    cap_reached = False

    for i, item in enumerate(UNIVERSE):
        if calls_made >= TD_DAILY_CAP:
            log(f'Cap raggiunto ({TD_DAILY_CAP}/day) — stop', 'WARN')
            cap_reached = True
            break

        ticker = item['ticker']
        existing = series_by_ticker.get(ticker)

        # Calcola da quale data partire
        if existing and existing.get('lastDate'):
            start_date = add_days(existing['lastDate'], 1)
            # Skip se start_date è oggi o nel futuro (weekend, festività, già aggiornato)
            if start_date >= today_str():
                skipped += 1
                if i % 50 == 0:
                    log(f'[{i+1}/{len(UNIVERSE)}] {ticker}: già aggiornato (lastDate={existing["lastDate"]})')
                continue
        else:
            start_date = history_start_str()

        try:
            log(f'[{i+1}/{len(UNIVERSE)}] {ticker}: fetch from {start_date}')
            new_bars = fetch_twelve_data(ticker, start_date, api_key)
            calls_made += 1

            if not new_bars:
                log(f'  → 0 nuove barre')
            else:
                if existing:
                    # Append (de-dup by date)
                    existing_dates = {b['date'] for b in existing.get('bars', [])}
                    appended = [b for b in new_bars if b['date'] not in existing_dates]
                    existing['bars'].extend(appended)
                    existing['bars'].sort(key=lambda x: x['date'])
                    existing['lastDate'] = existing['bars'][-1]['date'] if existing['bars'] else None
                    existing['updatedAt'] = datetime.utcnow().isoformat() + 'Z'
                    log(f'  → +{len(appended)} barre (totale {len(existing["bars"])})')
                else:
                    # First sync for this ticker
                    new_rec = {
                        'ticker': ticker,
                        'name': item['name'],
                        'sector': item['sector'],
                        'bars': new_bars,
                        'lastDate': new_bars[-1]['date'],
                        'firstDate': new_bars[0]['date'],
                        'updatedAt': datetime.utcnow().isoformat() + 'Z',
                    }
                    series_by_ticker[ticker] = new_rec
                    log(f'  → initial sync: {len(new_bars)} barre')

                updated += 1
        except Exception as e:
            log(f'  → ERROR: {e}', 'ERROR')
            errors += 1

        # Rate limit
        if calls_made < len(UNIVERSE):
            time.sleep(TD_RATE_LIMIT_SEC)

    # Save back
    data['series'] = list(series_by_ticker.values())
    data['meta'] = data.get('meta', {})
    data['meta']['lastSync'] = datetime.utcnow().isoformat() + 'Z'
    data['meta']['lastSyncCalls'] = calls_made
    save_archive(data)

    log(f'=== SUMMARY ===')
    log(f'Calls made:  {calls_made}')
    log(f'Updated:     {updated}')
    log(f'Skipped:     {skipped}')
    log(f'Errors:      {errors}')
    log(f'Total ticker:{len(data["series"])}/{len(UNIVERSE)}')
    log(f'Cap reached: {cap_reached}')

    # Non fallire mai se ci sono errori parziali (qualche ticker fallisce ma altri ok)
    # Fallisci solo se TUTTI hanno fallito o nessuna call è stata fatta
    if calls_made == 0 and len(UNIVERSE) > 0 and not all(series_by_ticker.get(item['ticker']) for item in UNIVERSE):
        log('Zero calls made and archive incomplete — failing', 'ERROR')
        sys.exit(2)


if __name__ == '__main__':
    main()
