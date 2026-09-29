#!/usr/bin/env python3
"""
photo-consolidator - Consolidamento archivio fotografico multi-sorgente
con resilienza ai blackout.

Riunisce foto e video da piu' fonti quasi duplicate (inclusi quelli dentro
archivi di backup zip/7z/rar/tar) in un'unica struttura AAAA/MM/GG sul disco
di destinazione, scartando le immagini troppo piccole, deduplicando per hash
esatto del contenuto, e tracciando ogni passo in un database SQLite cosi'
che un'interruzione improvvisa (blackout, kill, crash) non faccia perdere il
lavoro gia' svolto: basta rilanciare lo stesso comando per riprendere
esattamente da dove si era fermato.

Configurazione:
    Tutti i percorsi (fonti, destinazione, scratch) sono definiti in un file
    di configurazione JSON, NON nel codice. Genera un file di esempio con:

        python3 consolidate.py --init-config config.json

    poi modificalo con i tuoi percorsi reali e lancialo con:

        python3 consolidate.py --config config.json --dry-run

Uso:
    python3 consolidate.py --init-config config.json   # crea una config di esempio
    python3 consolidate.py --config config.json --dry-run   # simula tutto tranne la copia finale
    python3 consolidate.py --config config.json             # esegue per davvero (riprende se interrotto)
    python3 consolidate.py --config config.json --status    # mostra solo il report dello stato attuale

Licenza: MIT (vedi LICENSE)
"""

import argparse
import concurrent.futures
import json
import hashlib
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configurazione (valori di default, sovrascritti da apply_config() con i
# contenuti del file --config fornito dall'utente: NESSUN percorso reale e'
# hardcoded per renderlo utilizzabile su qualsiasi macchina)
# ---------------------------------------------------------------------------

HOME = Path.home()
STATE_DIR = HOME / ".photo-consolidator"
STATE_DB = STATE_DIR / "state.db"
LOG_FILE = STATE_DIR / "run.log"

# Elenco (nome_simbolico, percorso) delle fonti da consolidare. Popolato da
# apply_config() leggendo la chiave "sources" del file di configurazione.
SOURCE_ROOTS = []

# Destinazione: la "primary" e' il disco dedicato e la priorita' naturale. Se
# si esaurisce lo spazio li', si prosegue automaticamente su qualsiasi disco
# elencato in "fallback" (puoi includere anche i dischi sorgente se vuoi),
# scegliendo ogni volta il PIU' VUOTO al momento (non un ordine fisso: cosi'
# la scelta si adatta man mano che i dischi si riempiono). Su ogni disco di
# riserva i file finiscono dentro la sottocartella indicata da "subdir" (o
# nella radice se "subdir" e' null), cosi' il risultato resta riconoscibile
# indipendentemente dal disco fisico su cui e' finito (vedi pick_dest_root).
DEST_PRIMARY = None
DEST_FALLBACK_POOL = []
MIN_DEST_FREE = 5 * 1024 ** 3  # 5 GB: sotto questa soglia si passa al prossimo disco
NO_DATE_DIRNAME = "senza-data"

# Il primo disco della lista scratch DOVREBBE essere una cartella su
# filesystem nativo Linux (ext4/btrfs/xfs...): a differenza di exFAT/NTFS,
# questi filesystem non impongono NESSUNA restrizione sui byte ammessi in un
# nome file a parte '/' e NUL. Molti vecchi backup Windows salvano intere
# voci come 'C:\Users\...\file.ext' in un unico nome: unzip le separa in
# sottocartelle (da qui l'avviso "appears to use backslashes as path
# separators"), il che significa creare una cartella letteralmente chiamata
# "C:" - IMPOSSIBILE su exFAT/NTFS (':' e' un carattere riservato), fallisce
# con "cannot create"/errno=22 anche ritentando con 7z. Vedi pick_scratch_disk.
SCRATCH_ROOTS = []
SCRATCH_SUBDIR = "_estrazioni_temp"
MIN_SCRATCH_FREE = 2 * 1024 ** 3  # 2 GB: sotto questa soglia si passa al prossimo disco di scratch in lista

# Cache persistente (su filesystem di sistema, non scratch effimero) per i
# file 'would_copy' provenienti da dentro un archivio: la loro cartella di
# estrazione viene cancellata a fine Fase2 anche in dry-run, quindi senza
# questa cache il file sorgente sparirebbe prima che la copia reale possa
# leggerlo (bug reale scoperto durante lo sviluppo: 5570 file persi cosi'
# prima che questo fix esistesse - vedi il capitolo "Avvertenze" nel README).
CACHE_DIR = STATE_DIR / "_would_copy_cache"

MIN_WIDTH = 640
MIN_HEIGHT = 480
MAX_ARCHIVE_DEPTH = 1  # un livello di ricorsione per archivi annidati

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
    ".bmp", ".gif", ".webp", ".raw", ".cr2", ".nef", ".arw",
    ".dng", ".orf", ".rw2",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mts",
    ".m2ts", ".wmv",
}
ARCHIVE_SIMPLE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tgz", ".tbz2", ".tbz"}

# Formati documento moderni che sono internamente contenitori ZIP (Office
# Open XML, OpenDocument, ebook EPUB): il rilevamento a magic byte li
# riconoscerebbe come 'archive' da espandere, ma non sono backup nel senso
# richiesto e trattarli come tali sprecherebbe tempo enorme (un'estrazione
# per ogni singolo documento/ebook) e rischierebbe di far entrare in
# FOTOGRAFIE immagini non pertinenti (es. copertine di ebook ad alta
# risoluzione che superano il filtro dimensionale minimo). Vengono esclusi
# esplicitamente PRIMA del controllo sui magic byte.
DOCUMENT_ZIP_EXTS = {
    ".docx", ".docm", ".dotx", ".dotm",
    ".xlsx", ".xlsm", ".xltx", ".xltm",
    ".pptx", ".pptm", ".potx", ".ppsx", ".ppsm",
    ".odt", ".ods", ".odp", ".odg", ".odf",
    ".epub",
}

# Volumi multi-parte WinRAR ("nome.part01.rar", "nome.part02.rar", ...): unrar,
# quando invocato su QUALSIASI parte, ricostruisce e ristrae l'intero contenuto
# se le altre parti sono presenti nella stessa cartella (verificato: stesso
# conteggio di file ottenuto ripetutamente su parti diverse dello stesso set).
# Registrare ed estrarre OGNI parte come archivio indipendente sprecherebbe
# tempo enorme (fino a N volte il lavoro per un set da N parti). Si registra
# per l'estrazione reale solo la parte con il numero piu' basso di ogni gruppo;
# le altre vengono marcate subito come 'archived' senza mai essere estratte.
RAR_VOLUME_RE = re.compile(r"^(.*)\.part0*(\d+)\.rar$", re.IGNORECASE)

# Cartelle note per contenere solo cache/dati applicativi, mai foto reali.
# Scoperte analizzando lo stallo su DATA4TBnero: un backup di profilo Windows
# sotto AppData conteneva centinaia di migliaia di piccoli file di cache
# (Edge Service Worker CacheStorage, WinGet cache, ecc.) che rendevano la
# scansione enormemente lenta senza alcun beneficio, dato che nessuno di quei
# file potra' mai superare il filtro immagine/video. Vengono saltate interamente
# (confronto case-insensitive sul nome della cartella).
# NB: 'fotografie' e' incluso perche' quando la destinazione finisce su un
# disco sorgente (es. MyBook14TBo pieno di spazio libero usato come riserva),
# l'output consolidato finisce dentro una sua sottocartella "FOTOGRAFIE": va
# esclusa dalla (ri)scansione delle fonti, altrimenti un run successivo la
# tratterebbe come materiale nuovo da consolidare, indicizzando e deduplicando
# senza fine il proprio stesso output gia' prodotto.
EXCLUDE_DIR_NAMES = {
    "appdata", "node_modules", ".git", "$recycle.bin",
    "system volume information", ".trash", ".trash-1000",
    "cachestorage", "__pycache__", ".cache", "fotografie",
    "temporary internet files",
}

MAGIC_SIGNATURES = [
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07\x00", "rar"),
    (b"Rar!\x1a\x07\x01\x00", "rar"),
    (b"\x1f\x8b", "gz"),
]

HASH_CHUNK = 4 * 1024 * 1024  # 4 MB
EXIFTOOL_BATCH = 200

# Parallelismo: di default pari al numero di CPU disponibili, ma il collo di
# bottiglia reale e' quasi sempre l'I/O su dischi/rete distinti, non la CPU.
# HASH_WORKERS thread eseguono lettura+SHA-256 in parallelo (safe: sola
# lettura sulle fonti, nessuna scrittura condivisa - i risultati vengono
# scritti su DB in modo sequenziale dal thread principale, unico scrittore).
HASH_WORKERS = os.cpu_count() or 4
EXIF_PARALLEL_CALLS = 4  # quante chiamate exiftool concorrenti per ogni lotto di file (processi separati, non limitati dal GIL)

# Fonti raggiunte via rete (non dischi USB/SATA locali): la banda e' spesso
# condivisa da un singolo link, quindi molti thread che leggono contempora-
# neamente file video di centinaia di MB non sommano velocita', si dividono
# solo la stessa banda con overhead aggiuntivo. Popolato da apply_config()
# con i nomi (dalla chiave "sources") marcati "network": true nella config.
NETWORK_SOURCE_NAMES = set()
NETWORK_READ_CONCURRENCY = 3

# Quante estrazioni possono avvenire IN PARALLELO dallo STESSO disco fisico
# sorgente: 1 e' consigliato per dischi ROTANTI (HDD), dato che leggerne piu'
# di uno alla volta dallo stesso disco non da' alcun beneficio - anzi peggiora
# le prestazioni per il seek thrashing delle testine. Un numero piu' alto va
# bene per SSD/NVMe (nessun problema di seek) e per contenuto gia' estratto
# su scratch locale veloce (source_root='archivio'). Popolato da apply_config
# con la chiave "rotational_source_concurrency" della config (default: 1 per
# ogni fonte elencata li', altrimenti DEFAULT_EXTRACT_CONCURRENCY).
SOURCE_EXTRACT_CONCURRENCY = {}
DEFAULT_EXTRACT_CONCURRENCY = 4
EXTRACTION_POOL_WORKERS = 10
EXTRACTION_QUEUE_MAXSIZE = 50

# Se una fonte non produce alcun file nuovo per piu' di questo tempo (secondi),
# viene considerata bloccata (es. richiesta di rete che non risponde) e lo
# script prosegue con le altre invece di restare in attesa indefinita. Il
# thread bloccato resta comunque attivo in background (e' un daemon thread):
# se si sblocca da solo, il lavoro fatto fino a quel punto e' comunque salvo.
STALL_TIMEOUT = 300

# ---------------------------------------------------------------------------
# Utilita'
# ---------------------------------------------------------------------------

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def human_size(n):
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_path TEXT UNIQUE NOT NULL,
    source_root TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'indexed',
    sha256 TEXT,
    width INTEGER,
    height INTEGER,
    exif_date TEXT,
    target_path TEXT,
    origin_archive_id INTEGER,
    error_msg TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_path TEXT UNIQUE NOT NULL,
    source_root TEXT NOT NULL,
    size INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    scratch_disk TEXT,
    extract_dir TEXT,
    depth INTEGER DEFAULT 0,
    parent_archive_id INTEGER,
    error_msg TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS hashes (
    sha256 TEXT PRIMARY KEY,
    target_path TEXT NOT NULL,
    first_file_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_complete (
    source_root TEXT PRIMARY KEY,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_origin ON files(origin_archive_id);
CREATE INDEX IF NOT EXISTS idx_archives_status ON archives(status);
"""


def get_conn():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STATE_DB), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=FULL;")  # invariato: e' la garanzia anti-blackout, non si tocca
    conn.execute("PRAGMA busy_timeout=30000;")  # con piu' connessioni concorrenti (thread di indicizzazione), attende invece di fallire subito
    conn.executescript(SCHEMA)
    # Migrazione: aggiunge 'cache_path' se il DB e' stato creato da una versione
    # precedente dello script (CREATE TABLE IF NOT EXISTS non aggiunge colonne
    # a una tabella gia' esistente).
    cols = [row[1] for row in conn.execute("PRAGMA table_info(files)")]
    if "cache_path" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN cache_path TEXT")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Classificazione file
# ---------------------------------------------------------------------------

def classify(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".tar.gz") or name.endswith(".tar.bz2"):
        return "archive"
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in ARCHIVE_SIMPLE_EXTS:
        return "archive"
    if ext in DOCUMENT_ZIP_EXTS:
        return "other"
    # fallback: controlla i magic byte per archivi con estensione sbagliata/mancante
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        for sig, _ in MAGIC_SIGNATURES:
            if head.startswith(sig):
                return "archive"
    except OSError:
        pass
    return "other"


# ---------------------------------------------------------------------------
# Fase 1 - Indicizzazione
# ---------------------------------------------------------------------------

def _walk_source(name, root_path, out_queue, stop_flag):
    """Cammina il filesystem di UNA fonte senza mai toccare il database:
    cosi' piu' fonti (dischi USB indipendenti + condivisione di rete) si
    possono leggere davvero in parallelo, senza contendersi il lock di
    scrittura di SQLite (che ammette un solo scrittore alla volta). I file
    trovati vengono semplicemente accodati; e' l'unico thread principale a
    scriverli nel database, in sequenza."""
    root = Path(root_path)
    if not root.exists():
        log(f"[Fase1] Fonte '{name}' non raggiungibile ({root_path}) - salto per ora")
        out_queue.put(("__DONE__", name, None, None, None))
        return
    log(f"[Fase1] Scansione '{name}' ({root_path})...")
    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            if stop_flag.is_set():
                return
            dirnames[:] = [d for d in dirnames if d.lower() not in EXCLUDE_DIR_NAMES]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                category = classify(fpath)
                out_queue.put((str(fpath), name, st.st_size, st.st_mtime, category))
                count += 1
                if count % 2000 == 0:
                    log(f"[Fase1] '{name}': {count} file scansionati finora (in coda per l'inserimento)...")
    except Exception as e:
        log(f"[Fase1] errore durante la scansione di '{name}': {e}")
    log(f"[Fase1] '{name}': scansione del filesystem completata, {count} file trovati in totale")
    out_queue.put(("__DONE__", name, None, None, None))


def phase1_index_all(conn):
    already_complete = {row[0] for row in conn.execute("SELECT source_root FROM scan_complete")}

    out_queue = queue.Queue(maxsize=50000)
    stop_flag = threading.Event()
    pending = set()
    for name, root_path in SOURCE_ROOTS:
        if name in already_complete:
            log(f"[Fase1] Fonte '{name}' gia' completamente scansionata in un run precedente, salto la ri-scansione")
            continue
        t = threading.Thread(target=_walk_source, args=(name, root_path, out_queue, stop_flag), daemon=True)
        t.start()
        pending.add(name)

    known_paths = set(row[0] for row in conn.execute("SELECT full_path FROM files"))
    inserted = 0
    last_activity = {name: time.time() for name in pending}
    stalled = set()

    def drain_available(block_timeout=None):
        nonlocal inserted
        got_any = False
        while True:
            try:
                item = out_queue.get(timeout=block_timeout) if block_timeout else out_queue.get_nowait()
            except queue.Empty:
                break
            block_timeout = None  # solo la prima get di ogni chiamata puo' bloccare
            got_any = True
            full_path, source_root, size, mtime, category = item
            last_activity[source_root] = time.time()
            if full_path == "__DONE__":
                pending.discard(source_root)
                conn.execute(
                    "INSERT OR REPLACE INTO scan_complete (source_root, completed_at) VALUES (?, ?)",
                    (source_root, time.time()),
                )
                conn.commit()
                continue
            if full_path in known_paths:
                continue
            known_paths.add(full_path)
            conn.execute(
                "INSERT INTO files (full_path, source_root, size, mtime, category, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'indexed', ?)",
                (full_path, source_root, size, mtime, category, time.time()),
            )
            inserted += 1
            if inserted % 1000 == 0:
                conn.commit()
                log(f"[Fase1] {inserted} nuovi file inseriti nel database finora (unico scrittore, nessun conflitto)...")
        return got_any

    try:
        while pending - stalled:
            drain_available(block_timeout=0.5)
            now = time.time()
            for name in list(pending - stalled):
                if now - last_activity[name] > STALL_TIMEOUT:
                    log(f"[Fase1] ATTENZIONE: la fonte '{name}' non fa progressi da oltre "
                        f"{STALL_TIMEOUT // 60} minuti (probabile file/cartella di rete che non risponde). "
                        f"Proseguo con le altre fonti; rilancia lo stesso comando piu' tardi per "
                        f"riprovare a completarla.")
                    stalled.add(name)
        # drena quel che resta arrivato nel frattempo
        while drain_available():
            pass
    except BaseException:
        stop_flag.set()
        raise
    finally:
        conn.commit()

    if stalled:
        log(f"[Fase1] {len(stalled)} fonte/i non completata/e in questo giro: {', '.join(sorted(stalled))}")
    log(f"[Fase1] Fase di indicizzazione conclusa per questo giro: {inserted} nuovi file inseriti in totale")
    register_pending_archives(conn)


def register_pending_archives(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, full_path, source_root, size FROM files WHERE category='archive'")
    all_archive_files = cur.fetchall()

    groups = {}
    standalone = []
    for fid, full_path, source_root, size in all_archive_files:
        m = RAR_VOLUME_RE.match(full_path)
        if m:
            base = m.group(1).lower()
            partnum = int(m.group(2))
            groups.setdefault(base, []).append((partnum, fid, full_path, source_root, size))
        else:
            standalone.append((fid, full_path, source_root, size))

    for fid, full_path, source_root, size in standalone:
        cur.execute("SELECT id FROM archives WHERE full_path = ?", (full_path,))
        if cur.fetchone():
            continue
        cur.execute(
            "INSERT INTO archives (full_path, source_root, size, status, depth, updated_at) "
            "VALUES (?, ?, ?, 'pending', 0, ?)",
            (full_path, source_root, size, time.time()),
        )

    for base, parts in groups.items():
        parts.sort(key=lambda p: p[0])
        designated_fid = parts[0][1]  # parte col numero piu' basso: l'unica da estrarre davvero
        for partnum, fid, full_path, source_root, size in parts:
            cur.execute("SELECT id FROM archives WHERE full_path = ?", (full_path,))
            existing = cur.fetchone()
            if existing:
                continue  # gia' registrato/gestito in un run precedente, non toccare
            if fid == designated_fid:
                cur.execute(
                    "INSERT INTO archives (full_path, source_root, size, status, depth, updated_at) "
                    "VALUES (?, ?, ?, 'pending', 0, ?)",
                    (full_path, source_root, size, time.time()),
                )
            else:
                cur.execute(
                    "UPDATE files SET status='archived', updated_at=? WHERE id=? AND status='indexed'",
                    (time.time(), fid),
                )
    conn.commit()


# ---------------------------------------------------------------------------
# Estrazione metadati (exiftool, a lotti per efficienza)
# ---------------------------------------------------------------------------

def probe_media_batch(paths):
    """Una sola chiamata exiftool per un gruppo di file: dimensioni + data.
    Ritorna dict path_str -> (width, height, exif_date_grezza)."""
    result = {}
    if not paths:
        return result
    try:
        proc = subprocess.run(
            ["exiftool", "-j", "-q", "-q",
             "-ImageWidth", "-ImageHeight",
             "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate"]
            + [str(p) for p in paths],
            capture_output=True, text=True, timeout=900,
        )
        data = json.loads(proc.stdout) if proc.stdout.strip() else []
    except Exception as e:
        log(f"[Fase3/4] errore chiamata exiftool a lotti: {e}")
        data = []
    by_source = {}
    for d in data:
        sf = d.get("SourceFile")
        if sf:
            by_source[sf] = d
    for p in paths:
        d = by_source.get(str(p))
        if d is None:
            result[str(p)] = (None, None, None)
            continue
        w = _scalar(d.get("ImageWidth"))
        h = _scalar(d.get("ImageHeight"))
        date = _scalar(d.get("DateTimeOriginal")) or _scalar(d.get("CreateDate")) or _scalar(d.get("MediaCreateDate"))
        result[str(p)] = (w, h, date)
    return result


def _scalar(value):
    """exiftool puo' restituire un elenco invece di un singolo valore quando un
    file ha piu' occorrenze dello stesso tag (video multi-traccia, immagini
    multi-frame/HEIC con piu' sotto-immagini, ecc.). SQLite rifiuta di
    salvare direttamente una lista Python: qui si prende semplicemente il
    primo valore utile."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def probe_media_batch_parallel(paths):
    """Divide il gruppo di file in sotto-lotti ed esegue piu' chiamate exiftool
    in processi separati e paralleli, invece di una sola chiamata seriale.
    Ogni chiamata a exiftool e' un processo a se': parallelismo vero, non
    limitato dal GIL di Python."""
    if not paths or len(paths) <= 50 or EXIF_PARALLEL_CALLS <= 1:
        return probe_media_batch(paths)
    chunk_size = max(1, -(-len(paths) // EXIF_PARALLEL_CALLS))  # ceil division
    chunks = [paths[i:i + chunk_size] for i in range(0, len(paths), chunk_size)]
    merged = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chunks)) as executor:
        for result in executor.map(probe_media_batch, chunks):
            merged.update(result)
    return merged


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


_network_roots_cache = None
_network_io_semaphore = threading.Semaphore(NETWORK_READ_CONCURRENCY)


def _network_roots():
    global _network_roots_cache
    if _network_roots_cache is None:
        _network_roots_cache = [
            Path(path) for name, path in SOURCE_ROOTS if name in NETWORK_SOURCE_NAMES
        ]
    return _network_roots_cache


def is_network_path(path: Path) -> bool:
    for root in _network_roots():
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def sha256_of_throttled(path: Path) -> str:
    """Come sha256_of, ma se il file proviene da una fonte di rete limita le
    letture concorrenti: oltre 2-3 flussi simultanei su un singolo link
    Gigabit condiviso non si guadagna velocita', si aggiunge solo overhead
    e contesa. I file locali (dischi USB/SATA indipendenti) non sono toccati
    da questo limite e mantengono il pieno parallelismo su HASH_WORKERS."""
    if is_network_path(path):
        with _network_io_semaphore:
            return sha256_of(path)
    return sha256_of(path)


def valid_date_parts(exif_date):
    if not exif_date:
        return None
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})", exif_date)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
        return m.group(1), m.group(2), m.group(3)
    return None


# ---------------------------------------------------------------------------
# Deduplica e copia (target path riservati in memoria per evitare scansioni ripetute)
# ---------------------------------------------------------------------------

_reserved_targets = None


def load_reserved_targets(conn):
    global _reserved_targets
    if _reserved_targets is None:
        _reserved_targets = set(
            row[0].lower() for row in conn.execute("SELECT target_path FROM hashes")
        )
    return _reserved_targets


def pick_dest_root():
    """Sceglie la cartella di destinazione da usare ORA: prima prova sempre
    DEST_PRIMARY (il disco dedicato FOTOGRAFIE); se non ha spazio sufficiente,
    passa a QUALSIASI disco in DEST_FALLBACK_POOL scegliendo ogni volta quello
    con PIU' spazio libero al momento della chiamata (non un ordine fisso),
    cosi' il lavoro non si ferma mai finche' ALMENO UN disco ne ha ancora, e
    la scelta si adatta automaticamente man mano che i dischi si riempiono."""
    if DEST_PRIMARY.exists():
        try:
            if shutil.disk_usage(DEST_PRIMARY).free >= MIN_DEST_FREE:
                return DEST_PRIMARY
        except OSError:
            pass

    candidates = []
    for base, subdir in DEST_FALLBACK_POOL:
        if not base.exists():
            continue
        try:
            free = shutil.disk_usage(base).free
        except OSError:
            continue
        candidates.append((free, base, subdir))
    candidates.sort(key=lambda c: c[0], reverse=True)  # il piu' vuoto prima

    for free, base, subdir in candidates:
        if free >= MIN_DEST_FREE:
            target = base / subdir
            target.mkdir(parents=True, exist_ok=True)
            return target

    # nessun disco (incluso il primario) ha la soglia minima: usa comunque il
    # piu' vuoto fra tutti i raggiungibili, primario incluso
    if DEST_PRIMARY.exists():
        try:
            candidates.append((shutil.disk_usage(DEST_PRIMARY).free, DEST_PRIMARY, None))
        except OSError:
            pass
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    free, base, subdir = candidates[0]
    target = (base / subdir) if subdir else base
    target.mkdir(parents=True, exist_ok=True)
    return target


def compute_target_path(conn, full_path, exif_date):
    dest_root = pick_dest_root()
    if dest_root is None:
        raise RuntimeError("nessun disco di destinazione raggiungibile con spazio libero")
    src_name = Path(full_path).name
    parts = valid_date_parts(exif_date)
    if parts:
        year, month, day = parts
        target_dir = dest_root / year / month / day
    else:
        target_dir = dest_root / NO_DATE_DIRNAME

    stem, suffix = Path(src_name).stem, Path(src_name).suffix
    reserved = load_reserved_targets(conn)
    candidate = target_dir / src_name
    n = 0
    while str(candidate).lower() in reserved or candidate.exists():
        n += 1
        candidate = target_dir / f"{stem}_{n:03d}{suffix}"
    reserved.add(str(candidate).lower())
    return candidate


def cache_ephemeral_source(fid, full_path, origin_archive_id):
    """Se il file vive dentro un archivio (origin_archive_id valorizzato), la
    sua cartella di estrazione e' effimera e sta per essere cancellata a fine
    Fase2: ne salva subito una copia in una cache persistente, cosi' la copia
    reale futura trovera' sempre qualcosa da leggere. Se invece il file vive
    direttamente su un disco sorgente (origin_archive_id NULL) resta li' per
    sempre: non serve nessuna cache, si ritorna semplicemente None."""
    if origin_archive_id is None:
        return None
    src = Path(full_path)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{fid}{src.suffix}"
    if not cache_path.exists():
        try:
            shutil.copy2(src, cache_path)
        except Exception as e:
            log(f"[Fase5/6] impossibile mettere in cache '{src.name}' (id={fid}): {e}")
            return None
    return str(cache_path)


def copy_and_verify(src: Path, target: Path, expected_hash: str):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / (target.name + ".part")
    if is_network_path(src):
        # Stesso limite delle letture di hashing: la copia legge dalla stessa
        # fonte di rete, quindi va throttlata allo stesso modo.
        with _network_io_semaphore:
            shutil.copy2(src, tmp)
    else:
        shutil.copy2(src, tmp)
    actual = sha256_of(tmp)  # legge dal file temporaneo gia' copiato in locale, non dalla rete: nessun limite necessario qui
    if actual != expected_hash:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("hash non corrispondente dopo la copia")
    os.replace(tmp, target)


# ---------------------------------------------------------------------------
# Fasi 3+4+5+6 combinate: filtro dimensioni, hash+data, deduplica, copia
# ---------------------------------------------------------------------------

def dedup_and_copy_batch(conn, dry_run, where_extra, extra_params):
    """Esegue deduplica e copia per tutti i file attualmente in stato 'hashed'
    che corrispondono al filtro dato. Viene chiamata sia all'inizio (per
    smaltire eventuali file hashati in un run precedente ma mai processati,
    tipicamente dopo un crash o un'interruzione), sia dopo ogni singolo lotto
    di hashing: cosi' i risultati diventano disponibili progressivamente
    invece che tutto-o-niente alla fine di un processo che puo' durare giorni.
    Ritorna il numero di file processati."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_path, sha256, exif_date, origin_archive_id FROM files WHERE status='hashed'" + where_extra,
        extra_params,
    )
    hashed_rows = cur.fetchall()
    if not hashed_rows:
        return 0
    log(f"[Fase5/6] deduplica e copia di {len(hashed_rows)} file...")

    for fid, full_path, digest, exif_date, origin_archive_id in hashed_rows:
        try:
            existing = conn.execute(
                "SELECT target_path FROM hashes WHERE sha256=?", (digest,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE files SET status='duplicate', target_path=?, updated_at=? WHERE id=?",
                    (existing[0], time.time(), fid),
                )
                continue

            target = compute_target_path(conn, full_path, exif_date)

            if dry_run:
                # Se il file vive dentro un archivio (origin_archive_id valorizzato),
                # la sua cartella di estrazione e' effimera: viene cancellata a
                # fine Fase2 SUBITO DOPO, anche in dry-run (vedi cache_ephemeral_
                # source). Senza questa cache, quando in seguito si lancia la
                # copia reale il file risulterebbe "non piu' trovato" e andrebbe
                # perso silenziosamente - scoperto su 5570 file gia' in questa
                # situazione prima del fix.
                cache_path = cache_ephemeral_source(fid, full_path, origin_archive_id)
                conn.execute(
                    "INSERT INTO hashes (sha256, target_path, first_file_id) VALUES (?, ?, ?)",
                    (digest, str(target), fid),
                )
                conn.execute(
                    "UPDATE files SET status='would_copy', target_path=?, cache_path=?, updated_at=? WHERE id=?",
                    (str(target), cache_path, time.time(), fid),
                )
                continue

            try:
                copy_and_verify(Path(full_path), target, digest)
            except Exception as e:
                conn.execute(
                    "UPDATE files SET status='error', error_msg=?, updated_at=? WHERE id=?",
                    (f"errore copia: {e}", time.time(), fid),
                )
                continue

            conn.execute(
                "INSERT INTO hashes (sha256, target_path, first_file_id) VALUES (?, ?, ?)",
                (digest, str(target), fid),
            )
            conn.execute(
                "UPDATE files SET status='copied', target_path=?, updated_at=? WHERE id=?",
                (str(target), time.time(), fid),
            )
        except Exception as e:
            log(f"[Fase5/6] valore inatteso per file id={fid}, segnato come errore: {e}")
            try:
                conn.execute(
                    "UPDATE files SET status='error', error_msg=?, updated_at=? WHERE id=?",
                    (f"valore inatteso: {e}", time.time(), fid),
                )
            except Exception:
                pass

    conn.commit()
    return len(hashed_rows)


def process_pending_files(conn, dry_run, only_origin_archive_id=None, direct_only=False):
    if only_origin_archive_id is not None:
        where_extra = " AND origin_archive_id = ?"
        extra_params = (only_origin_archive_id,)
    elif direct_only:
        where_extra = " AND origin_archive_id IS NULL"
        extra_params = ()
    else:
        where_extra = ""
        extra_params = ()

    # Ripulitura: eventuali file gia' hashati in un run precedente (es. prima
    # di un crash o interruzione) ma mai passati per deduplica/copia, vengono
    # gestiti subito invece di restare in sospeso.
    dedup_and_copy_batch(conn, dry_run, where_extra, extra_params)

    # I file non fotografici/video (category='other': PDF, .ini, .url, .bin,
    # immagini disco .vhdx di backup Windows, ecc.) non vanno mai ne' hashati
    # ne' copiati: non sono cio' che questo strumento deve consolidare, e
    # alcuni possono essere enormi (immagini disco da centinaia di GB),
    # sprecando tempo immenso se letti per intero. Vengono scartati subito,
    # senza leggerne il contenuto, con uno stato terminale dedicato.
    conn.execute(
        "UPDATE files SET status='not_media', updated_at=? "
        "WHERE status='indexed' AND category='other'" + where_extra,
        (time.time(),) + extra_params,
    )
    conn.commit()

    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_path, category FROM files WHERE status='indexed' "
        "AND category NOT IN ('archive', 'other')" + where_extra,
        extra_params,
    )
    rows = cur.fetchall()
    total = len(rows)
    if total:
        log(f"[Fase3/4] {total} file da analizzare (dimensioni, hash, data)...")
    processed = 0

    def _hash_one(item):
        """Lavoro per singolo file: solo lettura dalla fonte + calcolo hash,
        nessuna scrittura al database (quella resta nel thread principale,
        per sicurezza). Eseguito in parallelo su piu' thread: sia la lettura
        da disco/rete sia hashlib rilasciano il GIL, quindi piu' file da
        fonti diverse vengono letti davvero in concomitanza."""
        fid, full_path, category, media_info = item
        path = Path(full_path)
        if not path.exists():
            return (fid, "error", "file non trovato", None, None, None, None)

        width, height, exif_date = media_info.get(full_path, (None, None, None))

        if category == "image":
            if width is None or height is None:
                return (fid, "error", "dimensioni non leggibili", None, None, None, None)
            if width < MIN_WIDTH or height < MIN_HEIGHT:
                return (fid, "too_small", None, width, height, None, None)

        try:
            digest = sha256_of_throttled(path)
        except Exception as e:
            return (fid, "error", f"errore hashing: {e}", None, None, None, None)

        return (fid, "hashed", None, width, height, exif_date, digest)

    for i in range(0, len(rows), EXIFTOOL_BATCH):
        batch = rows[i:i + EXIFTOOL_BATCH]
        batch_paths = [Path(r[1]) for r in batch if Path(r[1]).exists()]
        media_info = probe_media_batch_parallel(batch_paths)
        work_items = [(fid, full_path, category, media_info) for fid, full_path, category in batch]

        with concurrent.futures.ThreadPoolExecutor(max_workers=HASH_WORKERS) as executor:
            results = list(executor.map(_hash_one, work_items))

        for fid, status, err, width, height, exif_date, digest in results:
            processed += 1
            try:
                if status == "error":
                    conn.execute(
                        "UPDATE files SET status='error', error_msg=?, updated_at=? WHERE id=?",
                        (err, time.time(), fid),
                    )
                elif status == "too_small":
                    conn.execute(
                        "UPDATE files SET status='too_small', width=?, height=?, updated_at=? WHERE id=?",
                        (width, height, time.time(), fid),
                    )
                else:
                    conn.execute(
                        "UPDATE files SET sha256=?, exif_date=?, width=?, height=?, status='hashed', updated_at=? WHERE id=?",
                        (digest, exif_date, width, height, time.time(), fid),
                    )
            except Exception as e:
                # Rete di sicurezza: un valore inatteso su un singolo file (es. un
                # tipo di dato imprevisto restituito da exiftool) non deve mai
                # far crashare un processo che gira per ore. Si registra l'errore
                # per quel file specifico e si prosegue con gli altri.
                log(f"[Fase3/4] valore inatteso per file id={fid}, segnato come errore: {e}")
                try:
                    conn.execute(
                        "UPDATE files SET status='error', error_msg=?, updated_at=? WHERE id=?",
                        (f"valore inatteso: {e}", time.time(), fid),
                    )
                except Exception:
                    pass

        conn.commit()
        if total:
            log(f"[Fase3/4] elaborati {min(processed, total)}/{total} file (con {HASH_WORKERS} thread paralleli)...")

        # Deduplica e copia PROGRESSIVA: il lotto appena hashato viene subito
        # deduplicato e copiato, invece di aspettare che tutti i file (potenzialmente
        # oltre un milione) siano stati hashati prima di salvare qualsiasi risultato.
        dedup_and_copy_batch(conn, dry_run, where_extra, extra_params)


# ---------------------------------------------------------------------------
# Fase 2 - Estrazione archivi
# ---------------------------------------------------------------------------

def pick_scratch_disk():
    """Preferisce il PRIMO disco della lista con spazio libero sufficiente,
    non quello con piu' spazio in assoluto: il primo e' una cartella ext4
    nativa senza restrizioni sui nomi file (vedi commento su SCRATCH_ROOTS),
    quindi va usata finche' possibile anche se gli altri dischi hanno piu'
    spazio fisico disponibile."""
    SCRATCH_ROOTS[0].mkdir(parents=True, exist_ok=True)
    for s in SCRATCH_ROOTS:
        if not s.exists():
            continue
        try:
            free = shutil.disk_usage(s).free
        except OSError:
            continue
        if free >= MIN_SCRATCH_FREE:
            return s
    # nessun disco ha la soglia minima: usa quello con piu' spazio libero fra i raggiungibili
    best = None
    best_free = -1
    for s in SCRATCH_ROOTS:
        if not s.exists():
            continue
        try:
            free = shutil.disk_usage(s).free
        except OSError:
            continue
        if free > best_free:
            best_free = free
            best = s
    return best


def _run_extract_cmd(cmd):
    # stdin=DEVNULL non basta da solo: unzip apre /dev/tty direttamente per
    # chiedere la password di un archivio protetto, ignorando lo stdin
    # rediretto. start_new_session=True stacca il sottoprocesso da
    # qualunque terminale di controllo, cosi' quel tentativo di apertura
    # fallisce subito (invece di bloccarsi fino un'ora, e nel frattempo
    # ricevere SIGTTIN che lo sospende come processo in background).
    # Scoperto su bollette TIM protette da password.
    return subprocess.run(cmd, timeout=3600,
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           start_new_session=True)


def extract_archive(archive_path: Path, dest_dir: Path) -> bool:
    name = archive_path.name.lower()
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        if name.endswith(".zip"):
            cmd = ["unzip", "-o", "-q", str(archive_path), "-d", str(dest_dir)]
        elif name.endswith(".7z"):
            cmd = ["7z", "x", "-y", f"-o{dest_dir}", str(archive_path)]
        elif name.endswith(".rar"):
            cmd = ["unrar", "x", "-y", str(archive_path), str(dest_dir) + os.sep]
        elif name.endswith(".tar.gz") or name.endswith(".tgz"):
            cmd = ["tar", "xzf", str(archive_path), "-C", str(dest_dir)]
        elif name.endswith(".tar.bz2") or name.endswith(".tbz2") or name.endswith(".tbz"):
            cmd = ["tar", "xjf", str(archive_path), "-C", str(dest_dir)]
        elif name.endswith(".tar"):
            cmd = ["tar", "xf", str(archive_path), "-C", str(dest_dir)]
        else:
            cmd = ["7z", "x", "-y", f"-o{dest_dir}", str(archive_path)]
        result = _run_extract_cmd(cmd)
        # unzip/7z/unrar/tar usano tutti la stessa convenzione: codice di uscita
        # 1 = avviso non fatale (es. "appears to use backslashes as path
        # separators" per zip creati su Windows), NON un fallimento reale -
        # il contenuto viene comunque estratto correttamente (verificato: un
        # archivio con questo avviso ha estratto tutti i 249 file previsti).
        # Trattarlo come errore fatale (come faceva check=True) scartava
        # migliaia di backup Windows senza mai indicizzarne il contenuto,
        # perdendo potenzialmente foto vere al loro interno. Solo un codice
        # di uscita >= 2 e' un fallimento reale.
        if result.returncode in (0, 1):
            if result.returncode == 1:
                err = result.stderr.decode(errors="replace") if result.stderr else ""
                log(f"[Fase2] {archive_path.name}: estratto con avvisi non fatali: {err[:200]}")
            return True
        err = result.stderr.decode(errors="replace") if result.stderr else f"codice di uscita {result.returncode}"

        # Molti vecchi zip DOS/Windows (backup aziendali, contabilita') salvano i
        # nomi file in una code page storica (es. CP850: lettere accentate
        # italiane/tedesche come 'a, o, u", gradi) invece che UTF-8. unzip, che
        # assume UTF-8 su questo sistema, genera per quei nomi sequenze di byte
        # non valide per il filesystem e abortisce l'INTERO archivio con
        # "cannot create" invece di limitarsi ad avvisare (scoperto analizzando
        # gli archivi falliti: 'D?sseldorf', 'Unit? Organizzate', ecc. al posto
        # degli accenti). Si ritenta esplicitando la code page storica; se anche
        # questo fallisce, si prova 7z che gestisce l'OEM charset autonomamente
        # ed e' generalmente piu' tollerante sui singoli nomi problematici.
        if name.endswith(".zip") and "cannot create" in err.lower():
            log(f"[Fase2] {archive_path.name}: fallito con encoding UTF-8 di default, ritento con code page CP850...")
            retry_cmd = ["unzip", "-o", "-q", "-O", "CP850", str(archive_path), "-d", str(dest_dir)]
            retry_result = _run_extract_cmd(retry_cmd)
            if retry_result.returncode in (0, 1):
                if retry_result.returncode == 1:
                    rerr = retry_result.stderr.decode(errors="replace") if retry_result.stderr else ""
                    log(f"[Fase2] {archive_path.name}: estratto con code page CP850, avvisi non fatali: {rerr[:200]}")
                else:
                    log(f"[Fase2] {archive_path.name}: estratto correttamente con code page CP850")
                return True
            log(f"[Fase2] {archive_path.name}: fallito anche con CP850, ultimo tentativo con 7z...")
            fallback_cmd = ["7z", "x", "-y", f"-o{dest_dir}", str(archive_path)]
            fallback_result = _run_extract_cmd(fallback_cmd)
            if fallback_result.returncode == 0:
                log(f"[Fase2] {archive_path.name}: estratto correttamente con 7z (fallback finale)")
                return True
            err = fallback_result.stderr.decode(errors="replace") if fallback_result.stderr else err

        log(f"[Fase2] estrazione fallita per {archive_path.name}: {err[:300]}")
        return False
    except subprocess.TimeoutExpired:
        log(f"[Fase2] estrazione troppo lenta/bloccata per {archive_path.name}, salto")
        return False


def _fix_invalid_utf8_name(path: Path) -> Path:
    """Rinomina sul disco un file/cartella il cui nome contiene byte non
    validi in UTF-8, restituendo il nuovo path. Necessario perche' lo scratch
    ext4 (a differenza di exFAT/NTFS, che li rifiutava a monte) accetta
    qualsiasi sequenza di byte come nome file: molti vecchi backup Windows
    usano una code page storica (tipicamente CP850, per le lettere accentate
    italiane/tedesche) invece di UTF-8. Estrarli ora funziona, ma salvare quel
    path nel database SQLite (che richiede UTF-8 valido), o anche solo
    stamparlo in un log, fallisce con UnicodeEncodeError. Si ricodifica il
    nome in UTF-8 a partire dai byte originali (recuperati con surrogateescape,
    lo stesso meccanismo con cui Python li rappresenta internamente)."""
    name = path.name
    try:
        name.encode("utf-8")
        return path
    except UnicodeEncodeError:
        raw = os.fsencode(name)
        try:
            fixed_name = raw.decode("cp850")
        except UnicodeDecodeError:
            fixed_name = raw.decode("utf-8", errors="replace")
        new_path = path.with_name(fixed_name)
        counter = 0
        while new_path.exists():
            counter += 1
            new_path = path.with_name(f"{fixed_name}_{counter}")
        try:
            path.rename(new_path)
        except OSError:
            return path
        return new_path


def index_extracted_content(conn, extract_dir: Path, archive_id: int, depth: int):
    new_count = 0
    for dirpath, dirnames, filenames in os.walk(extract_dir):
        dirnames[:] = [d for d in dirnames if d.lower() not in EXCLUDE_DIR_NAMES]
        # corregge i nomi delle sottocartelle con byte non validi in UTF-8
        # PRIMA che os.walk ci scenda dentro, cosi' i percorsi restano coerenti
        fixed_dirnames = []
        for d in dirnames:
            try:
                fixed = _fix_invalid_utf8_name(Path(dirpath) / d)
                fixed_dirnames.append(fixed.name)
            except Exception as e:
                log(f"[Fase2] impossibile correggere il nome cartella '{d}': {e}, la salto")
        dirnames[:] = fixed_dirnames
        for fname in filenames:
            try:
                fpath = _fix_invalid_utf8_name(Path(dirpath) / fname)
                fp_str = str(fpath)
            except Exception as e:
                log(f"[Fase2] impossibile correggere il nome file '{fname}': {e}, lo salto")
                continue
            if conn.execute("SELECT id FROM files WHERE full_path=?", (fp_str,)).fetchone():
                continue
            try:
                st = fpath.stat()
            except OSError:
                continue
            category = classify(fpath)
            conn.execute(
                "INSERT INTO files (full_path, source_root, size, mtime, category, status, "
                "origin_archive_id, updated_at) VALUES (?, 'archivio', ?, ?, ?, 'indexed', ?, ?)",
                (fp_str, st.st_size, st.st_mtime, category, archive_id, time.time()),
            )
            new_count += 1
            if category == "archive":
                conn.execute(
                    "INSERT OR IGNORE INTO archives (full_path, source_root, size, status, depth, "
                    "parent_archive_id, updated_at) VALUES (?, 'archivio', ?, 'pending', ?, ?, ?)",
                    (fp_str, st.st_size, depth + 1, archive_id, time.time()),
                )
    conn.commit()
    return new_count


def process_nested_archives_inline(conn, dry_run, parent_aid):
    """Processa IMMEDIATAMENTE, nella stessa sessione del genitore, eventuali
    archivi annidati appena scoperti al suo interno - PRIMA che la cartella di
    estrazione del genitore (da cui dipendono fisicamente) venga cancellata.
    Senza questo, un archivio annidato scoperto ora verrebbe rimandato al
    prossimo riavvio dello script, quando pero' la sua unica fonte (dentro lo
    scratch del genitore, gia' ripulito) sarebbe sparita per sempre (scoperto:
    53370 archivi annidati bloccati cosi' prima di questo fix). La ricorsione
    e' limitata da MAX_ARCHIVE_DEPTH (verificato ad ogni livello), quindi non
    puo' andare oltre la profondita' massima configurata."""
    nested = conn.execute(
        "SELECT id, full_path, depth FROM archives WHERE parent_archive_id=? AND status='pending'",
        (parent_aid,),
    ).fetchall()
    for nid, nfull_path, ndepth in nested:
        narchive_path = Path(nfull_path)

        if ndepth > MAX_ARCHIVE_DEPTH:
            conn.execute(
                "UPDATE archives SET status='error', error_msg='profondita di annidamento eccessiva', "
                "updated_at=? WHERE id=?",
                (time.time(), nid),
            )
            conn.execute(
                "UPDATE files SET status='error', error_msg='archivio: profondita eccessiva', "
                "updated_at=? WHERE full_path=? AND status='indexed'",
                (time.time(), nfull_path),
            )
            conn.commit()
            continue

        if not narchive_path.exists():
            # Non dovrebbe succedere qui (siamo ancora dentro l'estrazione
            # del genitore, appena completata), ma per sicurezza non blocca.
            log(f"[Fase2] annidato non raggiungibile subito dopo l'estrazione: {nfull_path}")
            continue

        nscratch = pick_scratch_disk()
        if nscratch is None:
            log("[Fase2] nessun disco di scratch per l'annidato, verra' ritentato al prossimo giro")
            continue
        nextract_dir = nscratch / SCRATCH_SUBDIR / f"archive_{nid}"
        if nextract_dir.exists():
            shutil.rmtree(nextract_dir, ignore_errors=True)

        conn.execute(
            "UPDATE archives SET status='extracting', scratch_disk=?, extract_dir=?, "
            "updated_at=? WHERE id=?",
            (str(nscratch), str(nextract_dir), time.time(), nid),
        )
        conn.commit()
        log(f"[Fase2] estraggo {narchive_path.name} (annidato) su {nscratch}...")
        ok = extract_archive(narchive_path, nextract_dir)
        if not ok:
            conn.execute(
                "UPDATE archives SET status='error', error_msg='estrazione fallita', "
                "updated_at=? WHERE id=?",
                (time.time(), nid),
            )
            conn.execute(
                "UPDATE files SET status='error', error_msg='archivio: estrazione fallita', "
                "updated_at=? WHERE full_path=? AND status='indexed'",
                (time.time(), nfull_path),
            )
            conn.commit()
            shutil.rmtree(nextract_dir, ignore_errors=True)
            continue

        conn.execute(
            "UPDATE archives SET status='extracted', updated_at=? WHERE id=?",
            (time.time(), nid),
        )
        conn.commit()

        already_indexed = conn.execute(
            "SELECT COUNT(*) FROM files WHERE origin_archive_id=?", (nid,)
        ).fetchone()[0]
        if already_indexed == 0:
            new_count = index_extracted_content(conn, nextract_dir, nid, ndepth)
            log(f"[Fase2] {narchive_path.name}: {new_count} file trovati nell'archivio (annidato)")
        register_pending_archives(conn)

        # Ricorsione: se questo annidato contiene a sua volta altri annidati,
        # processarli anche loro ORA, finche' MAX_ARCHIVE_DEPTH lo consente.
        process_nested_archives_inline(conn, dry_run, nid)

        process_pending_files(conn, dry_run, only_origin_archive_id=nid)

        shutil.rmtree(nextract_dir, ignore_errors=True)
        conn.execute(
            "UPDATE archives SET status='cleaned', updated_at=? WHERE id=?",
            (time.time(), nid),
        )
        conn.execute(
            "UPDATE files SET status='archived', updated_at=? WHERE full_path=? AND status='indexed'",
            (time.time(), nfull_path),
        )
        conn.commit()
        log(f"[Fase2] {narchive_path.name} (annidato) completato e scratch space liberato")


def phase2_process_archives(conn, dry_run):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_path, depth, source_root, status, extract_dir FROM archives "
        "WHERE status IN ('pending','extracting','extracted') ORDER BY id"
    )
    archives = cur.fetchall()
    if not archives:
        return
    log(f"[Fase2] {len(archives)} archivi da elaborare (estrazione parallela per disco fisico)")

    semaphores = {}

    def sem_for(source_root):
        if source_root not in semaphores:
            limit = SOURCE_EXTRACT_CONCURRENCY.get(source_root, DEFAULT_EXTRACT_CONCURRENCY)
            semaphores[source_root] = threading.Semaphore(limit)
        return semaphores[source_root]

    result_queue = queue.Queue(maxsize=EXTRACTION_QUEUE_MAXSIZE)
    stop_flag = threading.Event()

    def extract_worker(aid, full_path, depth, source_root):
        """Esegue SOLO il lavoro che non tocca il database: scegliere lo
        scratch, marcare 'extracting', estrarre l'archivio (subprocess, libera
        il GIL durante l'attesa I/O). Nessuna scrittura di contenuto al
        database qui: quella resta interamente al thread principale, unico
        scrittore - stessa garanzia di sicurezza gia' usata in Fase1."""
        if stop_flag.is_set():
            return
        with sem_for(source_root):
            if stop_flag.is_set():
                return
            archive_path = Path(full_path)
            if not archive_path.exists():
                result_queue.put(("offline", aid, full_path, depth, None, None))
                return
            scratch = pick_scratch_disk()
            if scratch is None:
                result_queue.put(("no_scratch", aid, full_path, depth, None, None))
                return
            extract_dir = scratch / SCRATCH_SUBDIR / f"archive_{aid}"
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
            conn_free_log = f"[Fase2] estraggo {archive_path.name} su {scratch}..."
            log(conn_free_log)
            ok = extract_archive(archive_path, extract_dir)
            result_queue.put(("extracted", aid, full_path, depth, extract_dir, ok))

    to_extract = []
    for aid, full_path, depth, source_root, status, extract_dir_str in archives:
        if status == "extracted" and extract_dir_str and Path(extract_dir_str).exists():
            archive_path = Path(full_path)
            log(f"[Fase2] ripresa: {archive_path.name} gia' estratto in precedenza, continuo")
            result_queue.put(("extracted", aid, full_path, depth, Path(extract_dir_str), True))
        else:
            to_extract.append((aid, full_path, depth, source_root))

    # NB: gli archivi in coda restano 'pending' finche' un worker non li prende
    # davvero in carico (lo stato passa a 'extracted'/'error' solo quando il
    # thread principale legge il risultato dalla coda) - marcarli 'extracting'
    # in blocco qui falserebbe le statistiche di avanzamento senza alcun
    # beneficio per la ripresa: 'pending' ed 'extracting' vengono comunque
    # trattati allo stesso identico modo (ri-estrazione da zero) in caso di
    # riavvio, dato che solo lo stato 'extracted' con una cartella valida
    # viene ripreso senza ripetere l'estrazione.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=EXTRACTION_POOL_WORKERS)
    for item in to_extract:
        executor.submit(extract_worker, *item)
    executor.shutdown(wait=False)

    total = len(archives)
    processed = 0
    try:
        while processed < total:
            kind, aid, full_path, depth, extract_dir, ok = result_queue.get()
            archive_path = Path(full_path)

            if kind == "offline":
                log(f"[Fase2] archivio non raggiungibile (fonte offline?): {full_path} - salto per ora")
                processed += 1
                continue

            if kind == "no_scratch":
                log("[Fase2] nessun disco di scratch disponibile, interrompo la Fase 2 per ora")
                stop_flag.set()
                break

            if depth > MAX_ARCHIVE_DEPTH:
                conn.execute(
                    "UPDATE archives SET status='error', error_msg='profondita di annidamento eccessiva', "
                    "updated_at=? WHERE id=?",
                    (time.time(), aid),
                )
                conn.execute(
                    "UPDATE files SET status='error', error_msg='archivio: profondita eccessiva', "
                    "updated_at=? WHERE full_path=? AND status='indexed'",
                    (time.time(), full_path),
                )
                conn.commit()
                if extract_dir:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                processed += 1
                continue

            if not ok:
                conn.execute(
                    "UPDATE archives SET status='error', error_msg='estrazione fallita', "
                    "updated_at=? WHERE id=?",
                    (time.time(), aid),
                )
                conn.execute(
                    "UPDATE files SET status='error', error_msg='archivio: estrazione fallita', "
                    "updated_at=? WHERE full_path=? AND status='indexed'",
                    (time.time(), full_path),
                )
                conn.commit()
                if extract_dir:
                    shutil.rmtree(extract_dir, ignore_errors=True)
                processed += 1
                continue

            conn.execute(
                "UPDATE archives SET status='extracted', updated_at=? WHERE id=?",
                (time.time(), aid),
            )
            conn.commit()

            already_indexed = conn.execute(
                "SELECT COUNT(*) FROM files WHERE origin_archive_id=?", (aid,)
            ).fetchone()[0]
            if already_indexed == 0:
                new_count = index_extracted_content(conn, extract_dir, aid, depth)
                log(f"[Fase2] {archive_path.name}: {new_count} file trovati nell'archivio")
            register_pending_archives(conn)

            # Gli eventuali archivi annidati appena scoperti vanno processati
            # SUBITO, mentre l'estrazione del genitore (extract_dir) esiste
            # ancora - vedi process_nested_archives_inline per il motivo.
            process_nested_archives_inline(conn, dry_run, aid)

            process_pending_files(conn, dry_run, only_origin_archive_id=aid)

            shutil.rmtree(extract_dir, ignore_errors=True)
            conn.execute(
                "UPDATE archives SET status='cleaned', updated_at=? WHERE id=?",
                (time.time(), aid),
            )
            # L'archivio stesso ha una riga nella tabella 'files' (category='archive',
            # status='indexed' fin dalla Fase 1): va marcata con uno stato terminale
            # qui, altrimenti resta 'indexed' per sempre e verrebbe inclusa nei
            # conteggi/report come file ancora da elaborare.
            conn.execute(
                "UPDATE files SET status='archived', updated_at=? WHERE full_path=? AND status='indexed'",
                (time.time(), full_path),
            )
            conn.commit()
            log(f"[Fase2] {archive_path.name} completato e scratch space liberato")
            processed += 1
    finally:
        stop_flag.set()


# ---------------------------------------------------------------------------
# Fase 7 - Report
# ---------------------------------------------------------------------------

def phase7_report(conn):
    cur = conn.cursor()
    print("\n=== REPORT ===")
    print("\n-- File per fonte --")
    for row in cur.execute("SELECT source_root, COUNT(*) FROM files GROUP BY source_root"):
        print(f"  {row[0]}: {row[1]} file")

    print("\n-- File per stato --")
    for row in cur.execute("SELECT status, COUNT(*) FROM files GROUP BY status"):
        print(f"  {row[0]}: {row[1]} file")

    print("\n-- Archivi per stato --")
    for row in cur.execute("SELECT status, COUNT(*) FROM archives GROUP BY status"):
        print(f"  {row[0]}: {row[1]}")

    total = cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status IN ('copied','would_copy')"
    ).fetchone()
    print(f"\nTotale foto/video unici da conservare: {total[0]} ({human_size(total[1])})")

    errors = cur.execute(
        "SELECT full_path, error_msg FROM files WHERE status='error' LIMIT 20"
    ).fetchall()
    if errors:
        print(f"\nPrimi errori riscontrati (max 20 mostrati):")
        for path, msg in errors:
            print(f"  {path}: {msg}")


# ---------------------------------------------------------------------------
# Fase 8 - Pulizia finale
# ---------------------------------------------------------------------------

def phase8_cleanup(conn):
    for scratch in SCRATCH_ROOTS:
        subdir = scratch / SCRATCH_SUBDIR
        if not subdir.exists():
            continue
        leftover = list(subdir.iterdir())
        if not leftover:
            continue
        log(f"[Fase8] trovate {len(leftover)} cartelle residue in {subdir}, verifico...")
        for item in leftover:
            m = re.match(r"archive_(\d+)", item.name)
            if m:
                aid = int(m.group(1))
                status = conn.execute(
                    "SELECT status FROM archives WHERE id=?", (aid,)
                ).fetchone()
                if status and status[0] == "cleaned":
                    shutil.rmtree(item, ignore_errors=True)
                    log(f"[Fase8] rimossa cartella residua {item}")


# ---------------------------------------------------------------------------
# Completamento copie reali dopo un dry-run precedente
# ---------------------------------------------------------------------------

def execute_dry_run_pending_copies(conn):
    """Quando si passa da --dry-run all'esecuzione reale, i file gia' marcati
    'would_copy' in un giro precedente hanno gia' hash, target_path calcolato
    e una riga in 'hashes' che li designa come 'primo esemplare' del proprio
    contenuto. Vanno copiati fisicamente ORA con questa funzione dedicata,
    SENZA farli ripassare dalla normale pipeline di hashing/deduplica in
    dedup_and_copy_batch: quella funzione, vedendo che il loro stesso hash e'
    gia' presente in 'hashes' (perche' ce l'hanno messo loro durante il
    dry-run), li marcherebbe come 'duplicate' di se stessi senza mai copiare
    nulla."""
    cur = conn.cursor()
    cur.execute("SELECT id, full_path, target_path, sha256, cache_path FROM files WHERE status='would_copy'")
    rows = cur.fetchall()
    if not rows:
        return
    log(f"[Fase5/6] {len(rows)} file gia' valutati in un dry-run precedente: eseguo la copia fisica ora...")
    done = 0
    for fid, full_path, target_path, digest, cache_path in rows:
        # Se il file veniva da un archivio, la cache persistente (creata al
        # momento del dry-run) e' l'unica fonte ancora valida: l'estrazione
        # originale e' gia' stata cancellata.
        src = Path(cache_path) if cache_path else Path(full_path)
        if not src.exists():
            conn.execute(
                "UPDATE files SET status='error', error_msg='file non piu trovato dopo il dry-run', "
                "updated_at=? WHERE id=?",
                (time.time(), fid),
            )
            conn.commit()
            continue
        try:
            copy_and_verify(src, Path(target_path), digest)
            conn.execute(
                "UPDATE files SET status='copied', updated_at=? WHERE id=?",
                (time.time(), fid),
            )
        except Exception as e:
            conn.execute(
                "UPDATE files SET status='error', error_msg=?, updated_at=? WHERE id=?",
                (f"errore copia: {e}", time.time(), fid),
            )
        conn.commit()
        done += 1
        if done % 500 == 0:
            log(f"[Fase5/6] copiati {done}/{len(rows)} file in sospeso dal dry-run...")
    log(f"[Fase5/6] copia dei file in sospeso dal dry-run completata: {done} file processati")


# ---------------------------------------------------------------------------
# Configurazione da file JSON
# ---------------------------------------------------------------------------

EXAMPLE_CONFIG = {
    "_comment": "Copia questo file, modifica i percorsi con i tuoi dischi reali, poi lancia: python3 consolidate.py --config config.json --dry-run",
    "sources": [
        {"name": "DiscoA", "path": "/media/utente/DiscoA", "network": False},
        {"name": "DiscoB", "path": "/media/utente/DiscoB", "network": False}
    ],
    "destination": {
        "primary": "/media/utente/FOTOGRAFIE",
        "fallback": [
            {"path": "/media/utente/DiscoDiRiserva", "subdir": "FOTOGRAFIE"}
        ]
    },
    "scratch_roots": [
        "~/.photo-consolidator/_scratch"
    ],
    "state_dir": "~/.photo-consolidator",
    "min_width": 640,
    "min_height": 480,
    "min_dest_free_gb": 5,
    "min_scratch_free_gb": 2,
    "hash_workers": None,
    "extraction_pool_workers": 10,
    "default_extract_concurrency": 4,
    "rotational_source_concurrency": {
        "DiscoA": 1,
        "DiscoB": 1
    }
}


def write_example_config(path: Path):
    path.write_text(json.dumps(EXAMPLE_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Config di esempio scritta in: {path}")
    print("Modificala con i tuoi percorsi reali, poi lancia:")
    print(f"  python3 consolidate.py --config {path} --dry-run")


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        print(f"ERRORE: file di configurazione non trovato: {config_path}")
        print("Generane uno di esempio con: python3 consolidate.py --init-config config.json")
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def apply_config(cfg: dict):
    """Applica la configurazione letta da JSON alle costanti globali del
    modulo. Nessun percorso e' hardcoded nel codice: TUTTO viene da qui,
    cosi' lo stesso script funziona su qualsiasi macchina/disco."""
    global HOME, STATE_DIR, STATE_DB, LOG_FILE, CACHE_DIR
    global SOURCE_ROOTS, DEST_PRIMARY, DEST_FALLBACK_POOL, SCRATCH_ROOTS
    global MIN_WIDTH, MIN_HEIGHT, MIN_DEST_FREE, MIN_SCRATCH_FREE
    global HASH_WORKERS, EXTRACTION_POOL_WORKERS, DEFAULT_EXTRACT_CONCURRENCY
    global SOURCE_EXTRACT_CONCURRENCY, NETWORK_SOURCE_NAMES

    if "state_dir" in cfg:
        STATE_DIR = Path(_expand(cfg["state_dir"]))
    STATE_DB = STATE_DIR / "state.db"
    LOG_FILE = STATE_DIR / "run.log"
    CACHE_DIR = STATE_DIR / "_would_copy_cache"

    sources_cfg = cfg.get("sources", [])
    if not sources_cfg:
        print("ERRORE: la configurazione non elenca nessuna fonte in 'sources'.")
        sys.exit(1)
    SOURCE_ROOTS = [(s["name"], _expand(s["path"])) for s in sources_cfg]
    NETWORK_SOURCE_NAMES = {s["name"] for s in sources_cfg if s.get("network")}

    dest_cfg = cfg.get("destination", {})
    if "primary" not in dest_cfg:
        print("ERRORE: la configurazione non specifica 'destination.primary'.")
        sys.exit(1)
    DEST_PRIMARY = Path(_expand(dest_cfg["primary"]))
    DEST_FALLBACK_POOL = [
        (Path(_expand(fb["path"])), fb.get("subdir"))
        for fb in dest_cfg.get("fallback", [])
    ]

    scratch_cfg = cfg.get("scratch_roots") or [str(STATE_DIR / "_scratch")]
    SCRATCH_ROOTS = [Path(_expand(s)) for s in scratch_cfg]

    MIN_WIDTH = cfg.get("min_width", MIN_WIDTH)
    MIN_HEIGHT = cfg.get("min_height", MIN_HEIGHT)
    MIN_DEST_FREE = cfg.get("min_dest_free_gb", 5) * 1024 ** 3
    MIN_SCRATCH_FREE = cfg.get("min_scratch_free_gb", 2) * 1024 ** 3

    HASH_WORKERS = cfg.get("hash_workers") or (os.cpu_count() or 4)
    EXTRACTION_POOL_WORKERS = cfg.get("extraction_pool_workers", 10)
    DEFAULT_EXTRACT_CONCURRENCY = cfg.get("default_extract_concurrency", 4)
    SOURCE_EXTRACT_CONCURRENCY = cfg.get("rotational_source_concurrency", {})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def check_space(conn):
    if DEST_PRIMARY.exists():
        free = shutil.disk_usage(DEST_PRIMARY).free
        log(f"[Fase0] Spazio libero su destinazione primaria {DEST_PRIMARY}: {human_size(free)}")
    for base, subdir in DEST_FALLBACK_POOL:
        if base.exists():
            free = shutil.disk_usage(base).free
            log(f"[Fase0] Spazio libero su riserva {base / subdir}: {human_size(free)}")
    for s in SCRATCH_ROOTS:
        if s.exists():
            free = shutil.disk_usage(s).free
            log(f"[Fase0] Spazio libero su scratch {s}: {human_size(free)}")
    for name, path in SOURCE_ROOTS:
        reachable = Path(path).exists()
        log(f"[Fase0] Fonte '{name}' ({path}): {'raggiungibile' if reachable else 'NON raggiungibile'}")


def run(dry_run):
    conn = get_conn()
    log(f"=== Avvio {'(DRY RUN)' if dry_run else '(ESECUZIONE REALE)'} ===")

    if pick_dest_root() is None:
        log("ERRORE FATALE: nessun disco di destinazione raggiungibile o con spazio libero "
            "(FOTOGRAFIE o riserve). Verifica che almeno un disco sia collegato e montato.")
        conn.close()
        return

    try:
        check_space(conn)
        if not dry_run:
            execute_dry_run_pending_copies(conn)
        phase1_index_all(conn)
        process_pending_files(conn, dry_run, direct_only=True)
        phase2_process_archives(conn, dry_run)
        if not dry_run:
            phase8_cleanup(conn)
        phase7_report(conn)
        log("=== Fine esecuzione ===")
    except BaseException as e:
        # Ultima rete di sicurezza: qualunque crash imprevisto finisce nel log
        # principale (quello monitorato), con traccia completa, invece che solo
        # nell'output grezzo del redirect che potrebbe passare inosservato per ore.
        import traceback
        log(f"!!! CRASH IMPREVISTO: {type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines():
            log(f"    {line}")
        log("Il lavoro gia' completato resta salvo nel database di stato. "
            "Rilancia lo stesso comando per riprendere.")
        raise
    finally:
        conn.close()


def show_status():
    conn = get_conn()
    phase7_report(conn)
    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="photo-consolidator - Consolidamento archivio fotografico "
                     "multi-sorgente con resilienza ai blackout"
    )
    parser.add_argument("--init-config", metavar="PATH",
                         help="Scrive un file di configurazione di esempio nel percorso indicato ed esce")
    parser.add_argument("--config", metavar="PATH", default="config.json",
                         help="Percorso del file di configurazione JSON (default: config.json)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Esegue tutto tranne la copia finale")
    parser.add_argument("--status", action="store_true",
                         help="Mostra solo il report dello stato attuale")
    args = parser.parse_args()

    if args.init_config:
        write_example_config(Path(args.init_config))
        return

    cfg = load_config(Path(args.config))
    apply_config(cfg)

    if args.status:
        show_status()
        return

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
