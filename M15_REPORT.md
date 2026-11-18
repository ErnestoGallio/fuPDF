# M15 — Save / Save As e round-trip sicuro

Implementazione limitata a M15. Nessuna nuova dipendenza; M15.1 non iniziata.
Nessun flattening delle annotazioni e nessuna modifica del modello worker/process
stabilito nei milestone precedenti.

## Stato sintetico

M15 implementa Save e Save As sicuri con validazione separata, replace atomico,
reopen nello stesso worker, mantenimento della history e checkpoint dirty.
Durante il lavoro sono stati inoltre corretti alcuni problemi emersi soltanto con
la suite completa e con il gate manuale Acrobat:

- ricorsione GUI / stack overflow durante rejection con editor FreeText aperto;
- teardown storici dei test incompatibili con il nuovo prompt Save/Discard/Cancel;
- import di PyMuPDF nel processo pytest principale;
- serializzazione FreeText/Callout non pienamente compatibile con l'editor Acrobat;
- distinzione fra nomi font PDF e nomi CSS usati da Acrobat nel rich text.

Il safe-save e il round-trip strutturale risultano stabili nei test automatici.
Il gate Acrobat è sostanzialmente superato per editing, colori, posizione,
round-trip e struttura FreeText/Callout. Resta da eseguire, sul nuovo artefatto
finale, l'ultima verifica manuale del mantenimento dei font per-run dopo doppio
clic in Acrobat. CJK/emoji sono documentati come limite dell'editor FreeText
Acrobat nel profilo/font disponibile, non come blocker M15.

InDesign resta **NOT RUN**.

---

## Implemented

### 1. Pipeline safe-save

Pipeline:

mutation già inviate
→ barriera
→ snapshot revisione confermata
→ TEMP sibling
→ flush/fsync
→ validazione in processo separato
→ chiusura document owner quando necessaria
→ `os.replace`
→ reopen nello stesso worker
→ cleanup

Il file originale non viene scritto direttamente mentre è aperto.

In caso di failure:

- l'originale resta intatto;
- dirty/history restano coerenti;
- la sessione rimane recuperabile quando possibile;
- i TEMP vengono ripuliti;
- se il recupero finale fallisce, l'unica copia valida non viene eliminata.

### 2. Validator

Il validator gira in un processo `spawn` separato e apre soltanto il candidate.

Verifica almeno:

- apertura senza repair/protezione;
- numero e geometria delle pagine;
- testo base;
- grafo raggiungibile dal trailer;
- dizionari e stream decodificati;
- contenuti, font, immagini e metadata;
- `/AP`;
- relazioni fra annotazioni;
- Unicode;
- QuadPoints;
- Replace composta M10.

Il confronto è semantico: ordina le chiavi e rietichetta i riferimenti per visita,
senza usare i numeri xref come identità.

Un controllo separato dei locator impedisce inoltre di accettare un candidate che
invaliderebbe gli snapshot nativi usati dalla history M14.

### 3. Save / Save As

Implementati:

- `Ctrl+S` → Save;
- `Ctrl+Shift+S` → Save As;
- menu File;
- `QFileDialog` nativo;
- overwrite;
- cancel senza effetti;
- collisione con documenti già aperti;
- equivalenza path/case/hard-link su Windows.

Save mantiene il path corrente.

Save As cambia path, identità e titolo soltanto dopo il successo. Il source
originale resta invariato.

### 4. Reopen e generation

Dopo Save/Save As:

- resta lo stesso documento logico;
- resta lo stesso worker/PID;
- `document_generation` viene incrementata;
- risposte vecchie diventano stale;
- cache text/inventory/raster della generation precedente vengono invalidate;
- zoom, scroll, layout, sidebar, query e posizione di ricerca restano preservati.

### 5. History e dirty

History e cursore M14 vengono mantenuti.

Lo stato corrente diventa il saved checkpoint:

- Save riuscito → `dirty = false`;
- Undo dal checkpoint → `dirty = true`;
- Redo fino allo stesso checkpoint → `dirty = false`.

La history non viene azzerata dal salvataggio.

### 6. Concorrenza e close

Durante Save:

- nuove mutation sono bloccate;
- Undo/Redo sono bloccati;
- altri Save sono bloccati;
- il close concorrente non interrompe il worker.

Se Save viene richiesto con Text Box, Callout, Insert o Replace ancora in editing,
l'editor viene confermato **prima** della barriera Save; la barriera attende la
mutation già inviata.

Se la conferma fallisce:

- Save non parte;
- il contenuto editor resta recuperabile;
- viene mostrato un solo errore.

La chiusura dirty offre Salva / Scarta / Annulla e attende correttamente il Save
prima dello shutdown.

---

## Hardening emerso dalla full suite

### Reentrancy / stack overflow GUI

La full suite aveva inizialmente esposto:

`session_changed`
→ `show_failure`
→ `set_active`
→ auto-confirm editor
→ mutation reject
→ nuovo `session_changed`
→ ricorsione

fino a `STATUS_STACK_OVERFLOW`.

Correzione:

- `show_failure()` è idempotente;
- mostrare un failure non auto-conferma più editor;
- `set_active()` auto-conferma soltanto durante un vero cambio tab;
- i segnali dell'editor dismesso vengono bloccati per evitare un secondo
  auto-confirm da `focusOut`.

### Teardown storici

Il nuovo close dirty M15 rendeva obsoleti numerosi teardown che falsificavano
revisioni o chiudevano finestre senza scegliere Save/Discard/Cancel.

È stato introdotto `close_window_discard()` e i teardown storici non interessati
al lifecycle close sono stati convertiti al percorso test-safe
`request_shutdown(discard_changes=True)`.

Sono stati trovati e corretti 39 teardown analoghi. I test M15 che verificano
davvero Salva/Scarta/Annulla non sono stati bypassati.

### Isolamento PyMuPDF nel processo pytest

`tests/test_save_m15_ui.py` importava `pymupdf` a livello modulo durante pytest
collection, violando l'invariante:

`"pymupdf" not in sys.modules`

nel processo pytest/main/GUI.

L'ispezione PDF è stata spostata nel subprocess helper:

`python -m tests.m15_helpers inspect-freetext ...`

Nessun product code importa PyMuPDF nel processo GUI.

---

## Interoperabilità FreeText / Callout con Acrobat

### Problema iniziale

Il primo gate manuale Acrobat ha evidenziato:

- XML/XHTML mostrato letteralmente al doppio clic;
- testo non allineato come una normale FreeText Acrobat;
- colore bordo non rispettato;
- font per-run normalizzati entrando in editing;
- CJK/emoji visualizzati correttamente nell'appearance iniziale ma trasformati in
  placeholder quando Acrobat entrava in editing.

### `/AP` e stato editoriale

È stato verificato che `/AP` e stato editoriale Acrobat sono due livelli distinti.

L'`/AP` app-created può usare font Type0/fallback e rendere correttamente anche
glifi che Acrobat non sa poi ricostruire nel proprio editor.

Entrando in editing, Acrobat ricostruisce invece il contenuto soprattutto da:

- `/RC`;
- `/DA`;
- `/DS`;
- `/Contents`.

### Correzione `/RC`

PyMuPDF 1.28 generava nel percorso usato un namespace XHTML non adatto al profilo
Acrobat (`.../xtml`). Il wrapper lo lasciava nel PDF.

Ora:

- `/Contents` resta plain text;
- `/RC` viene serializzato come XHTML Acrobat-valido ed escaped;
- `/RC` viene installato prima della rigenerazione dell'`/AP`;
- whitespace/riga fantasma non vengono più introdotti;
- `/C`, `/DA`, `/DS` e `/RC` vengono separati semanticamente per fill, bordo,
  fallback e stile dei run.

### Ground truth Acrobat sui font

Nel riferimento Acrobat nativo sono state identificate nuove FreeText create
direttamente in Acrobat.

Nel loro `/RC`, Acrobat usa famiglie CSS come:

- `Helvetica`;
- `Times`;
- `'Courier Std'`;
- `'Myriad Pro'` per specifici run formattati.

Le appearance native usano invece nomi/resource PDF differenti, ad esempio
`/Helv`, `/TiRo`, `/CourierStd`, `/MyriadPro-*`.

L'app serializzava precedentemente nomi PostScript/PDF (`Times-Roman`, `Courier`)
anche dentro `/RC`. Acrobat quindi normalizzava il font quando apriva l'editor.

Il profilo è stato corretto separando:

- nomi PDF/resource;
- nomi CSS Acrobat in `/RC` e `/DS`.

Esempi:

- PDF/resource → `/TiRo`;
- editor CSS → `Times`;

and:

- PDF/resource → `/Cour`;
- editor CSS → `'Courier Std'`.

Nessuna manipolazione speculativa delle risorse `/AP`, nessun embedding/subsetting
font aggiuntivo e nessuna modifica alla geometria Callout.

### Unicode

CJK/emoji non sono considerati blocker M15.

Nel test manuale, anche una nuova FreeText creata direttamente in Acrobat mostrava
placeholder per i caratteri CJK incollati con il profilo/font disponibile.

Quindi:

- la nostra appearance iniziale può comunque visualizzarli grazie ai fallback;
- entrando in editing in Acrobat, tali glifi possono essere sostituiti;
- non è stata introdotta nuova infrastruttura di embedding/subsetting font.

Questo limite è documentato.

### Geometria Callout

L'app usa un knee deterministico a 90°.

Acrobat visualizza correttamente la geometria iniziale; dopo editing può
ricalcolare il knee secondo la propria geometria, con angolo variabile vicino al
box.

La differenza è accettata come non bloccante: `/IT /FreeTextCallout`, `/CL`, `/RD`,
anchor, box e round-trip restano corretti.

---

## Preservation

Il salvataggio usa opzioni conservative:

- `garbage=0`;
- `clean=False`;
- nessun flattening;
- nessuna ricostruzione arbitraria delle annotazioni esterne;
- nessuna modifica intenzionale dei page content stream;
- nessuna ottimizzazione aggressiva.

Restano preservati:

### App-created

- Ink;
- Highlight;
- FreeText rich;
- Callout rich;
- Delete;
- Insert;
- Replace.

### External

- FreeText/Callout Acrobat con `/AP`;
- Ink;
- Highlight/markup;
- Sticky Note;
- editorial corrections;
- subtype sconosciuti.

Le annotation esterne restano non editabili in M15. La sola cancellazione resta
quella già autorizzata dai controlli M12. L'editing interoperabile delle external
annotation resta M15.1.

---

## Performance Safe Save

Profiling aggiunto per:

- barriera mutation;
- scan semantico owner;
- write/fsync candidate;
- startup validator;
- scan semantico validator;
- backup;
- close;
- `os.replace`;
- reopen;
- rebind/invalidation;
- cleanup.

Benchmark storico Save As su una prova locale riservata:

- totale GUI end-to-end: **2,983 s**;
- scan semantico owner: **1,041 s** (~34,9%);
- startup validator: **0,605 s** (~20,3%);
- scan semantico validator: **1,156 s** (~38,8%);
- candidate write + fsync: **~32 ms**;
- close + replace + reopen: **~9,8 ms**;
- barriera mutation: **~6 ms**.

Il costo è quindi dominato dalle due verifiche semantiche e dallo startup del
validator, non dalla scrittura fisica del PDF.

Non è stata applicata alcuna ottimizzazione speculativa: la sicurezza M15 resta
prioritaria. Un'eventuale riduzione del costo dello scan potrà essere valutata in
un milestone performance successivo.

Il rallentamento percepito nel caricamento della sidebar commenti Acrobat non è
stato attribuito causalmente al safe-save: non è misurabile dal profilo locale.

---

## File principali modificati

- `worker/save_service.py`
- `worker/save_validation.py`
- `worker/document_service.py`
- `worker/protocol.py`
- `worker/client.py`
- `worker/annotation_adapter.py`
- `worker/annotation_dispatch.py`
- `worker/freetext_adapter.py`
- `app/save_coordinator.py`
- `app/document_manager.py`
- `app/document_session.py`
- `app/file_resolver.py`
- `app/main_window.py`
- `services/annotation_service.py`
- `viewer/pdf_view.py`
- `util/paths.py`
- `tests/conftest.py`
- `tests/m15_helpers.py`
- `tests/m15_benchmark.py`
- `tests/test_save_m15.py`
- `tests/test_save_m15_ui.py`
- `tests/test_document_lifecycle.py`
- teardown storici aggiornati per il nuovo lifecycle M15
- `README.md`
- `M15_REPORT.md`

---

## Verifiche automatiche

Nel corso del milestone sono stati eseguiti, fra gli altri:

- test M15 + lifecycle/history/inventory mirati: **77 passed**;
- UI + lifecycle dopo isolamento PyMuPDF: **36 passed**;
- UI + M15 save + lifecycle: **62 passed**;
- regressioni viewer M8/M9: **14 passed**;
- viewer M6: **18 passed**;
- viewer M7: **27 passed**;
- rich FreeText M8.2: **10 passed**;
- servizi M5/M8: **22 passed**;
- M8/M8.2/M9 dopo fix Acrobat: **27 passed**;
- M15 Save/UI dopo fix Acrobat: **37 passed**;
- test finali mirati sul mapping font Acrobat: **9 passed**;
- benchmark M15: **PASS**;
- `compileall`: **PASS**;
- `git diff --check`: **PASS** salvo warning CRLF.

Una full suite completa è stata eseguita con esito verde dopo i fix di
reentrancy, teardown e isolamento PyMuPDF.

**Nota:** dopo l'ultimo mapping dei nomi CSS font-family basato sul ground truth
Acrobat sono stati eseguiti i test mirati sopra, ma non una seconda full suite
completa.

---

## Gate manuale Acrobat

### Verificato manualmente

- apertura dei PDF M15 senza warning/corruzione;
- FreeText/Callout visualmente corretti;
- XML non più mostrato come contenuto dopo la correzione `/RC`;
- posizione testo corretta;
- bordo/fill/testo distinti;
- modifica in Acrobat e Save;
- riapertura nella nostra app delle modifiche Acrobat;
- round-trip leggibile e coerente;
- Callout funzionante;
- CJK identificato come limite nativo Acrobat/font.

### Ultimo controllo consigliato prima del commit

Aprire:

`output/m15/M15-acrobat-freetext-gate.pdf`

e verificare con doppio clic che, dopo l'ultimo mapping CSS:

- Helvetica resti Helvetica;
- Times resti Times;
- Courier resti Courier;
- bold/italic/colore per-run restino invariati.

Salvare poi in Acrobat e riaprire nell'app.

Se questo controllo passa, il gate FreeText/Callout Acrobat può essere segnato
**PASS** con la sola limitazione documentata CJK/emoji.

---

## Acceptance status

- safe Save / Save As: **PASS automatico**
- failure recovery: **PASS automatico**
- source invariato su Save As: **PASS automatico**
- history checkpoint/dirty: **PASS automatico**
- collisioni path/case/hard-link: **PASS automatico**
- PyMuPDF isolation: **PASS automatico**
- Acrobat visualizzazione/editing/round-trip: **PASS manuale**, salvo ultimo
  check finale del mapping font per-run indicato sopra
- CJK/emoji in editor FreeText Acrobat: **LIMITAZIONE DOCUMENTATA**
- InDesign: **NOT RUN**
- M15.1 external annotation editing: **NOT STARTED**

---

## Checklist finale

1. Aprire un PDF, creare annotazioni di più tipi → `Ctrl+S` → chiudere/riaprire.
2. `Ctrl+Shift+S` → verificare Cancel, overwrite e collisione con altro PDF aperto.
3. Verificare Undo → dirty e Redo → saved checkpoint clean.
4. Aprire una prova annotata autorizzata, eseguire Save As su una copia e verificare le annotazioni esterne.
5. Aprire il risultato in Acrobat e verificare FreeText/Callout/editoriali.
6. Salvare in Acrobat e riaprire nell'app.
7. Verificare il campione finale dei font per-run.
8. Lasciare InDesign come **NOT RUN** finché non sarà disponibile un ambiente di test.

## API di riferimento

- PyMuPDF `Document.save`
- PyMuPDF `DisplayList` / `Page.get_displaylist`
- Qt `QFileDialog`
