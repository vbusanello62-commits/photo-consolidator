# photo-consolidator

Consolidamento di un archivio fotografico multi-sorgente, con resilienza ai blackout.

Riunisce foto e video da più fonti quasi duplicate (dischi, NAS, backup) — inclusi quelli nascosti dentro archivi zip/7z/rar/tar di backup — in un'unica struttura `AAAA/MM/GG`, deduplicando per hash esatto del contenuto e scartando le immagini troppo piccole (thumbnail/icone). Ogni passo è tracciato in un database SQLite: un'interruzione improvvisa (blackout, crash, `kill`) non fa perdere il lavoro già svolto — basta rilanciare lo stesso comando per riprendere esattamente da dove si era fermato.

Nato per consolidare ~1,3 milioni di file reali sparsi su tre dischi USB quasi identici accumulati in anni di backup incrociati, risultato: 36.543 foto/video unici recuperati su oltre 11 milioni di file totali esaminati.

## Caratteristiche

- **Resiliente ai blackout**: stato persistito in SQLite (WAL + `synchronous=FULL`), ripresa automatica da dove interrotto
- **Deduplica per contenuto** (hash SHA-256), non per nome file
- **Estrae ricorsivamente** gli archivi di backup (zip, 7z, rar inclusi i volumi multi-parte, tar/tgz/tbz2) fino a un livello di annidamento configurabile
- **Filtra le immagini troppo piccole** (icone, thumbnail) tramite soglia dimensionale configurabile
- **Multi-disco di destinazione** con fallback automatico quando lo spazio finisce, scegliendo sempre il disco più vuoto
- **Estrazione parallela** con limiti di concorrenza per disco fisico (evita il seek thrashing sugli HDD rotanti)
- **Modalità dry-run**: simula l'intero processo (inclusa l'estrazione e la deduplica) senza scrivere nulla sulla destinazione finale, per revisionare il risultato prima di impegnarsi

## Requisiti

- Python 3.9+
- Strumenti da riga di comando: `unzip`, `7z` (p7zip), `unrar`, `tar`, `exiftool`

Su Debian/Ubuntu/Mint:
```bash
sudo apt install p7zip-full unrar unzip exiftool
```

## Avvio rapido

```bash
# 1. Genera un file di configurazione di esempio
python3 consolidate.py --init-config config.json

# 2. Modifica config.json con i tuoi dischi reali (vedi sotto)

# 3. Simula senza scrivere nulla (fortemente raccomandato come primo passo)
python3 consolidate.py --config config.json --dry-run

# 4. Controlla il report, poi esegui per davvero
python3 consolidate.py --config config.json

# In qualsiasi momento, anche a processo in corso:
python3 consolidate.py --config config.json --status
```

## Configurazione

Esempio di `config.json`:

```json
{
  "sources": [
    {"name": "DiscoA", "path": "/media/utente/DiscoA", "network": false},
    {"name": "DiscoB", "path": "/media/utente/DiscoB", "network": false}
  ],
  "destination": {
    "primary": "/media/utente/FOTOGRAFIE",
    "fallback": [
      {"path": "/media/utente/DiscoDiRiserva", "subdir": "FOTOGRAFIE"}
    ]
  },
  "scratch_roots": ["~/.photo-consolidator/_scratch"],
  "min_width": 640,
  "min_height": 480,
  "rotational_source_concurrency": {"DiscoA": 1, "DiscoB": 1}
}
```

- `sources`: le fonti da consolidare. `network: true` limita la concorrenza di lettura per fonti su NAS/condivisioni di rete.
- `destination.primary`: il disco/cartella di destinazione preferito.
- `destination.fallback`: dischi di riserva usati automaticamente se il primario si riempie (possono includere anche i dischi sorgente).
- `scratch_roots`: cartelle temporanee per estrarre gli archivi. **Il primo dovrebbe essere su un filesystem nativo Linux** (ext4/btrfs/xfs) — vedi Avvertenze.
- `rotational_source_concurrency`: quante estrazioni parallele per disco fisico sorgente (1 per HDD rotanti, valori più alti per SSD/NVMe).

Tutte le chiavi hanno un default ragionevole tranne `sources` e `destination.primary`, obbligatorie.

## Avvertenze — leggere prima dell'uso

Alcuni bug reali, scoperti durante lo sviluppo su un caso reale multi-terabyte, sono già corretti nel codice, ma è importante capire **perché**, per evitare di reintrodurli modificando lo script:

1. **Lo scratch DEVE tollerare qualsiasi byte nei nomi file.** Vecchi backup Windows salvano interi percorsi assoluti (`C:\Users\...\file.ext`) come singola voce in uno zip; l'estrazione li separa in sottocartelle, creando letteralmente una cartella `C:` — impossibile su exFAT/NTFS (`:` è riservato). Usare sempre un filesystem nativo Linux come primo scratch.
2. **I file "da copiare" trovati dentro un archivio vanno messi in cache subito**, non lasciati nella cartella di estrazione: quella cartella viene ripulita a fine elaborazione dell'archivio, **anche in dry-run**. Senza la cache (già implementata in `cache_ephemeral_source`), il file risulterebbe irraggiungibile al momento della copia reale.
3. **Gli archivi annidati scoperti dentro un altro archivio vanno processati SUBITO**, prima di ripulire la cartella del genitore — altrimenti la loro unica fonte sparisce per sempre. Vedi `process_nested_archives_inline`.
4. **Un file archivio (zip/rar/7z) non deve mai finire nella pipeline di hash/copia dei media**: va sempre estratto e il suo contenuto deduplicato individualmente, mai copiato come blob unico (altrimenti si perde la deduplica di tutto ciò che contiene, e si copiano gigabyte di container invece delle singole foto).
5. **Testare sempre con `--dry-run` prima** e leggere il report finale (`--status`) prima di lanciare la copia reale.
6. **Nomi file con encoding legacy** (CP850, tipico di vecchi zip DOS/Windows con lettere accentate) vengono gestiti con un tentativo di ri-decodifica automatica; se l'estrazione fallisce ripetutamente su un archivio specifico, controllare i log per il messaggio esatto prima di ignorarlo.

## Come funziona (architettura in breve)

Il programma opera per fasi, tutte riprendibili indipendentemente grazie allo stato in SQLite:

- **Fase 0**: verifica spazio disco e raggiungibilità delle fonti
- **Fase 1**: scansione parallela delle fonti (un thread per fonte, thread-safe: solo il thread principale scrive su SQLite)
- **Fase 2**: estrazione parallela degli archivi (limitata per disco fisico), con gestione ricorsiva degli annidati
- **Fasi 3-6**: per ogni file media, calcolo dimensioni (via `exiftool`), hash SHA-256, deduplica, copia con verifica
- **Fase 7**: report finale
- **Fase 8**: pulizia scratch residuo

Per l'analisi tecnica completa (inclusi tutti i bug scoperti e le relative correzioni) vedi la relazione tecnica allegata al progetto.

## Licenza

MIT — vedi [LICENSE](LICENSE). Uso, modifica e ridistribuzione libera, incluso uso commerciale, senza garanzia.
