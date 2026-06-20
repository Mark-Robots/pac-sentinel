# PAC Sentinel — Static / Cloud

PAC Sentinel è uno strumento di analisi quantitativa per piani di accumulo (PAC) su azioni US.
Questa repo contiene la versione "cloud" del progetto: i dati di mercato vengono sincronizzati
automaticamente ogni notte tramite GitHub Actions e il dashboard è ospitato su GitHub Pages.

## Struttura

```
pac-sentinel/
├── index.html              # dashboard (verrà aggiunto nella Fase 2)
├── sync_daily.py           # script che aggiorna archivio.json da Twelve Data
├── build_snapshot.py       # script che genera snapshot.json pre-calcolato
├── archivio.json           # archivio non compresso (per script)
├── archivio.json.gz        # archivio compresso (per dashboard web)
├── snapshot.json           # screener + Q-Score pre-calcolati (caricamento veloce)
└── .github/workflows/
    └── sync.yml            # automazione giornaliera
```

## Setup iniziale (una sola volta)

### 1. Creare repository GitHub

Crea una nuova repo (può essere pubblica o privata) e clona localmente.
Copia in essa i 4 file generati: `sync_daily.py`, `build_snapshot.py`, `README.md`, `.github/workflows/sync.yml`.

### 2. Aggiungere il secret TWELVE_DATA_KEY

- Vai su Settings → Secrets and variables → Actions → "New repository secret"
- Nome: `TWELVE_DATA_KEY`
- Valore: la tua API key Twelve Data
- Salva

### 3. Primo run manuale

- Vai su Actions → "Sync archive daily" → "Run workflow"
- Aspetta ~50 minuti (primo sync completo, 322 ticker × 8 sec)
- Al termine vedrai un nuovo commit con `archivio.json`, `archivio.json.gz`, `snapshot.json`

### 4. Lasciar fare

Da questo momento ogni giorno alle 07:00 UTC parte automaticamente il sync incrementale
(~3-5 minuti, solo le barre nuove dal giorno precedente).

## Esecuzione manuale (locale)

Se vuoi testare gli script sul tuo PC prima di committare:

```bash
# Imposta la API key come variabile d'ambiente
export TWELVE_DATA_KEY="89b98d4c..."

# Esegui sync
python sync_daily.py

# Genera snapshot
python build_snapshot.py
```

## Limiti Twelve Data Free

- 8 chiamate/minuto (gestito automaticamente)
- 800 chiamate/giorno (cap rispettato)
- Sync incrementale giornaliero usa ~150-200 chiamate (ampio margine)

## Note

- L'universo dei 322 ticker è hardcoded in `sync_daily.py`. Se vuoi modificarlo, edita la sezione `SECTOR_TICKERS`.
- Il benchmark per il Q-Score è QQQ (Nasdaq). Modificabile in `build_snapshot.py` (`QSCORE_BENCHMARK`).
- I file `archivio.json` (~70 MB) e `archivio.json.gz` (~12 MB) vengono committati in Git. È un design semplice ma il repo diventerà grandino. In futuro si può migrare a Git LFS o GitHub Releases.

## Fase 2

In una sessione successiva verrà aggiunto `index.html` (dashboard statico) che caricherà
`snapshot.json` e (su richiesta) `archivio.json.gz` per fare backtest predittivi nel browser.
