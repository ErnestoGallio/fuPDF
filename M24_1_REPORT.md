> Historical report superseded by M24_1B_REPORT.md and the pages-only export.

> Historical M24.1 design, superseded by M24.1a. Geometric DTOs and the separate confirmation operation described below have been removed. See M24_1A_REPORT.md for the current contract.

# M24.1 — Estrazione locale del testo strutturato

## Implemented

Branch `codex/m24-1-text-extraction`, creato da `origin/main` dopo `git fetch
origin main`; base `60a28ac36020b85c15394492a68bca5f016c1bbb`.
Working tree inizialmente pulito. Release/versione `0.18.0` invariata.

L'esportazione si avvia da **File > Esporta testo strutturato…** per il PDF
aperto. Produce un unico bundle JSON locale con due rappresentazioni collegate:
`pages` conserva geometria e diagnostica; `text_batches` contiene i soli
identificativi, intervalli e testi destinabili alla futura modalità testuale.
Nessuna chiamata OpenAI, nuova modalità di revisione, OCR o modifica del motore
di annotazione. Nessun commit, push, merge, tag, release o build Windows.

## Architettura e ricognizione

- `WorkerTextService` già estrae caratteri, conserva indici/quads canonici,
  limita la cache e riconferma range/caret per il dominio delle annotazioni.
  Rimane invariato.
- `review_localization_text` già dispone di `base_textpage`, `character_quad`,
  `page_bounds` e `line_groups`: sono riutilizzati. `base_textpage` esegue solo
  il contenuto della pagina, escludendo testo delle appearance dei commenti.
- La localizzazione multimodale esistente usa `PageMatcher`, contesto e prove
  dello snapshot. Il nuovo percorso usa direttamente ID/intervalli/geometria,
  senza cambiare quel matcher. Riutilizza `locate_glyph` per riconciliare i
  caratteri estremi con la selezione viva, rifiutando corrispondenze multiple.
- Il batching multimodale (`review_batches`) assegna quattro pagine fisiche e
  contesto PDF: non è adatto al budget testuale e resta invariato. Il nuovo
  iteratore usa segmenti/intervalli con le stesse distinzioni target/contesto.
- Le due nuove operazioni IPC sono `STRUCTURED_TEXT_PAGE` e
  `STRUCTURED_TEXT_CONFIRM`. Sono ammesse attraverso lo stesso canale limitato
  delle operazioni review, con la stessa pausa per Save e routing per
  documento/generazione/richiesta. Un nuovo estratto attende le richieste già
  pendenti: rendering e interazioni possono inserirsi fra le pagine.

PyMuPDF opera esclusivamente nel worker persistente del documento, sul suo
thread principale. Non vengono aperti nuovi Document né aggiunti processi PDF.
Il worker scrive una pagina JSON temporanea privata; IPC trasporta percorso,
dimensione e SHA-256. Il consumatore verifica e rimuove il file usando il reader
lock già esistente, che impedisce cleanup concorrente alla lettura.

Un thread senza PyMuPDF legge una pagina alla volta, scrive `pages` progressivamente
e usa un file temporaneo per accodare `text_batches`. Non conserva la geometria
dell'intero documento. Solo un export diagnostico per applicazione. Il candidato
è un file fratello della destinazione, pubblicato con `os.replace` dopo flush e
fsync; errori/interruzioni preservano un output precedente e rimuovono il candidato.
Il controller Qt non attende IPC, letture, join o serializzazione pesante.

## Schema JSON

Schema eseguibile e validatori: `domain/structured_text.py`.

| Livello | Campi |
| --- | --- |
| Bundle | `schema = fupdf.structured-text.v1`, `extraction_id`, `identity`, `page_count`, `offset_unit`, `coordinate_space`, `batch_policy`, `pages`, `text_batches` |
| Identity | `document_id`, `document_generation`, `document_revision` |
| Pagina locale | `schema`, `extraction_id`, `identity`, `page_number`, `width`, `height`, `rotation`, `cropbox`, `segments`, `issues`, `page_digest` |
| Segmento | `segment_id`, `block_id`, `block_index`, `group_index`, `reading_order`, `text`, `lines`, `words`, `glyphs`, `issues` |
| Riga/parola | `start`, `end`, `line` |
| Glifo | `start`, `end`, `line`, `span`, `char`, `quad` oppure `null` |
| Batch testuale | `batch_id`, `extraction_id`, `items` |
| Elemento batch | `page_number`, `segment_id`, `start`, `end`, `text`, `ends_segment`, `role = target/context` |

Le pagine fisiche sono 1-based; gli indici RAWDICT e `reading_order` sono
0-based. Gli offset sono intervalli Python Unicode `[start, end)`, non byte,
UTF-16 o token. La geometria è in punti PyMuPDF non ruotati, con ordine dei
quattro punti `UL, UR, LL, LR`; DPR e zoom non entrano nei riferimenti.

Esempio concreto del primo batch della fixture sintetica, con budget di 64
caratteri target e 10 caratteri di contesto. Questo oggetto è il payload
testuale: non contiene coordinate, hash della pagina o metadati geometrici.

```json
{
  "batch_id": "1744e292a6729213d333f049cce33afebf08fd986ec2c4cde6eb0c56f6fd297e",
  "extraction_id": "208be228233d6309fa81388601a602573ac2c7481e93787b11480cecdaebe8e3",
  "items": [
    {
      "page_number": 1,
      "segment_id": "208be228233d6309fa81388601a602573ac2c7481e93787b11480cecdaebe8e3:p1:b0:s0",
      "start": 0,
      "end": 61,
      "text": "Primo paragrafo con spazi.\nSeconda riga con sillaba-\nzione e ",
      "ends_segment": false,
      "role": "target"
    },
    {
      "page_number": 1,
      "segment_id": "208be228233d6309fa81388601a602573ac2c7481e93787b11480cecdaebe8e3:p1:b0:s0",
      "start": 61,
      "end": 71,
      "text": "Unicode: c",
      "ends_segment": false,
      "role": "context"
    }
  ]
}
```

Nella rappresentazione locale il primo carattere `P`, nello stesso segmento,
ha questo riferimento geometrico effettivamente estratto:

```json
{
  "start": 0, "end": 1, "line": 0, "span": 0, "char": 0,
  "quad": [
    [50.0, 60.011624813079834],
    [56.93600082397461, 60.01162338256836],
    [50.0, 75.69562530517578],
    [56.93600082397461, 75.6956238746643]
  ]
}
```

`StructuredPage.from_payload` verifica schema, campi, tipi, identità, digest,
copertura di righe/glifi/parole e coordinate finite nei limiti canonici.
SHA-256 rileva alterazioni e cambiamenti; non costituisce una firma di fiducia
per un file importato. Il worker riestrae comunque la pagina originale.

## Corrispondenza testo-coordinate

`extraction_id` deriva deterministicamente da schema/documento/generazione/
revisione. Blocco e segmento aggiungono pagina, indice RAWDICT e gruppo di righe;
la riconferma aggiunge il digest di testo, struttura, diagnostica e geometria
della pagina. Gli ID restano uguali nella stessa estrazione, non sono identità
persistenti fra riaperture o revisioni diverse.

Ogni glifo conserva intervallo Unicode, indici della riga/span/carattere sorgente
e quad; righe e parole permettono di risalire agli stessi intervalli. I newline
strutturali separano le righe e non possiedono quad. Il testo non viene
normalizzato, desillabato o corretto; i caratteri privi di geometria sicura
rimangono nel testo con `quad: null` e diagnostica.

`TextReference` contiene estrazione, pagina/digest, segmento, intervallo e testo
esatto. Il worker riestrae la pagina e rifiuta ID/digest/revisione obsoleti,
testo differente, estremi interni a un glifo multipunto, geometria incerta e
direzioni editoriali non supportate. Confronta tutti i glifi selezionati con
quelli vivi; le occorrenze ripetute sono distinte dalla geometria, mai scegliendo
la prima stringa trovata. Corrispondenze geometriche duplicate sono rifiutate.
Restituisce un `TextRangeTarget` o `TextInsertionTarget` confermato, senza mutazioni.

## Batch

`iter_text_batches` è lazy e mantiene ordine di pagina/lettura. Default:
12.000 code point target, massimo 128 elementi, contesto disabilitato.
Il servizio permette fino a 1.000 code point per lato, comunque non oltre un
quarto del budget target. La UI non introduce impostazioni aggiuntive.

Un segmento viene mantenuto intero quando entra nel budget; quelli troppo
lunghi vengono spezzati preferendo spazi/newline e senza dividere glifi.
Non si perdono né duplicano caratteri. I batch possono attraversare pagine,
conservando separatamente gli ID delle porzioni. Il contesto ha ruolo esplicito
e non può ricevere correzioni tramite `TextBatch.validate_target`. Gli intervalli
target sono disgiunti. Un inserimento sul confine fra frammenti appartiene al
successivo; alla fine del segmento appartiene al frammento finale.
La deduplicazione semantica di proposte multiple del modello resta a M24.2.

## Files changed

- `domain/structured_text.py`: DTO locali, identità/digest, JSON e riferimenti.
- `worker/structured_text.py`: estrazione, ordine geometrico e riconferma.
- `services/structured_text_batches.py`: batching lazy e proprietà dei target.
- `services/structured_text_export.py`: lettura verificata e scrittura atomica.
- `app/text_export.py`: controller background e lifecycle Qt.
- `app/main_window.py`: sola azione File, dialogo salva e stato sintetico.
- `worker/protocol.py`, `worker/client.py`, `worker/document_service.py`:
  instradamento delle due operazioni e riuso del workspace privato.
- `tests/m24_text_fixtures.py`, `tests/test_structured_text.py`,
  `tests/test_structured_text_ui.py`: fixture sintetiche e controlli mirati.
- `PROJECT_SPEC.md`, `ARCHITECTURE.md`, `ROADMAP.md`, `ACCEPTANCE_TESTS.md`:
  perimetro M24.1 e test AT-TEXT-01..06.
- `M24_1_REPORT.md`: questo report e procedura manuale.

## Tests

Comandi eseguiti dalla root con Python 3.14.10, PyMuPDF 1.28.2 e PySide6
6.11.2 già presenti. Nessuna dipendenza aggiunta.

```text
.venv\Scripts\python.exe -m pytest tests/test_structured_text.py tests/test_structured_text_ui.py tests/test_review_batches.py tests/test_review_snapshot.py tests/test_review_application.py tests/test_review_dialog.py tests/test_text_selection.py -q --tb=short
-> 84 passed in 106.58s

.venv\Scripts\python.exe -m pytest tests/test_structured_text.py tests/test_structured_text_ui.py -q --tb=short
-> 19 passed in 15.45s (inclusi gli ulteriori casi di limiti/lifecycle)

.venv\Scripts\python.exe -m pytest tests/test_structured_text.py::test_oversized_confirmation_reply_is_controlled_without_retiring_worker -q --tb=short
-> 1 passed in 1.23s (risposta d'errore senza payload sovradimensionato)

git diff --check
-> PASS
```

La prima selezione comprende 71 regressioni esistenti e i primi 13 casi M24.1;
la seconda selezione comprende tutti i 19 casi nuovi. Il controllo finale
verifica anche che l'errore IPC scarti il payload sovradimensionato prima di
serializzare la risposta. Le esecuzioni iniziali di
sviluppo hanno individuato e corretto l'ordine colonne/piè di pagina, un'asserzione
sul DTO dei comandi esistente e la selezione del nome file nel test Qt.
Le prove con processi separati sono state eseguite fuori dal sandbox ristretto
Windows, nel quale i primi due tentativi erano rimasti sospesi e sono stati
interrotti. Nessuna full suite o build Windows avviata.

## Acceptance status

- **AT-TEXT-01..05 PASS** automatico: fedeltà/struttura, riferimenti,
  serializzazione, batching, limiti, export, Save As, cambio scheda, chiusura.
- **AT-TEXT-06 PASS** per la regressione automatica offline; **NOT RUN** per
  il collaudo manuale su PDF editoriali reali.
- Porzioni pertinenti di **AT-EDIT-04, AT-COORD-01/02, AT-PERF-04/10/13 PASS**:
  rifiuto obsoleti, coordinate attese, lifecycle/event loop del nuovo percorso.
  Questo non ricertifica integralmente quei gate di prodotto.
- Gate manuali **Acrobat/InDesign NOT RUN**, nessuna nuova compatibilità dichiarata.

## Known limitations / dipendenze M24.2

- Un PDF può non avere ordine semantico/paragrafi affidabili. Si usa una
  suddivisione geometrica XY e il raggruppamento conservativo delle righe;
  colonne irregolari, tabelle, testatine, note e layout sovrapposti vanno
  ispezionati. Le euristiche sono indicate in `issues`.
- Non si uniscono automaticamente paragrafi fra pagine. I batch mantengono
  l'ordine e i riferimenti fisici di entrambe le parti.
- Testo estratto da font con mappature Unicode difettose può essere incompleto
  o non affidabile. Nessuna ricostruzione linguistica/OCR. Il testo RTL/verticale
  può essere esportato, ma la riconferma editoriale non supportata viene rifiutata.
- Pagine oltre 250.000 code point o 64 MiB di JSON sono esplicitamente
  diagnosticate come non processabili. La memoria resta legata alle pagine
  correnti e al batch; non è una promessa di RSS totale pari a 64 MiB. Una
  singola estrazione MuPDF di pagina non è interrompibile a metà.
- Save As/riapertura e nuove revisioni richiedono nuovi riferimenti; durante
  l'export è preferibile mantenere invariato il documento.
- Il budget è in caratteri, non token: M24.2 dovrà usare i limiti del modello.

M24.2 può usare automaticamente `StructuredTextWorker.page` e
`iter_text_batches` senza esportazione manuale; dovrà conservare la mappa
locale, verificare proprietà e testo dei target, riconfermare con
`StructuredTextWorker.confirm` e inviare i comandi attraverso A-06.
Scheduler, API, schema delle proposte, deduplicazione di risultati e nuova
modalità UI non sono implementati in questa milestone.

## Procedura di collaudo manuale — NOT RUN

1. Avviare il sorgente con `.venv\Scripts\python.exe -m pdf_annotator.main`.
2. Aprire un PDF digitale rappresentativo; scegliere File > Esporta testo
   strutturato e salvare il file `.text.json`.
3. Aprire il JSON con un editor: verificare pagine fisiche, segmenti nell'ordine
   previsto, Unicode/spazi/newline/sillabazione, `issues` e i batch separati.
4. Confrontare due occorrenze identiche nella stessa pagina: ID/intervalli/quads
   devono distinguere la posizione. Controllare anche colonne e CropBox/rotazioni.
5. Esportare un PDF lungo e scorrere/cambiare scheda: la GUI deve continuare a
   rispondere. L'output resta associato al PDF dal quale è partito il dialogo.
6. Provare una pagina immagine/vuota: deve apparire `no_extractable_text`, senza
   testo inventato. Provare Save As o chiusura durante un export lungo: niente
   pubblicazione tardiva dell'estrazione obsoleta e niente modifica del PDF.
7. Verificare che Revisione con IA conservi l'interfaccia multimodale precedente.
   Un eventuale collaudo reale con API richiede un avvio esplicito successivo;
   questa milestone non lo esegue.

Riferimenti ufficiali consultati per RAWDICT/ordine e quad:
[TextPage](https://pymupdf.readthedocs.io/en/latest/textpage.html),
[recover_char_quad](https://pymupdf.readthedocs.io/en/latest/functions.html#recover_char_quad),
[Text extraction recipes](https://pymupdf.readthedocs.io/en/latest/recipes-text.html).
