# M24.1b — Estrazione lineare per pagina

## Implemented

Sostituito il nucleo sperimentale a paragrafi sul branch esistente
`codex/m24-1-text-extraction`. Le modifiche M24.1/M24.1a erano già presenti
nel working tree e non committate; questo intervento le semplifica.

Percorso: menu File → controller background → export progressivo → richiesta
IPC per pagina → worker dedicato → `base_textpage` →
`page.get_text("text", textpage=textpage, sort=False)` → sostituzione degli
accapo con spazi → `PageText(page, text)` → scrittura JSON atomica.

Il TextPage content-only viene associato localmente alla pagina, come richiesto
dal controllo `parent` del binding PyMuPDF 1.28.2. La funzione condivisa
`base_textpage` resta invariata: il testo delle annotazioni non entra nell'export.
Non vengono ricostruiti o ordinati blocchi, righe, parole o paragrafi.

CRLF diventa un solo spazio; CR, LF e gli altri separatori di riga diventano
spazi. Tutti gli altri caratteri e le sequenze di spazi restano invariati,
anche ai margini. Una pagina composta soltanto da whitespace diventa `""`.
Unicode, sillabazioni, numeri di pagina e ordine nativo non sono normalizzati.

## Schema definitivo

```json
{
  "pages": [
    {"page": 1, "text": "Testo della prima pagina..."},
    {"page": 2, "text": ""}
  ]
}
```

Nessun altro campo. UTF-8 con caratteri Unicode leggibili, un oggetto per riga;
le stringhe non contengono CR/LF. Ogni pagina fisica compare una sola volta.
Nessun batch o duplicato del testo nell'export.

M24.2 potrà consumare direttamente `StructuredTextWorker.page` / `PageText`,
che conservano numero fisico e testo. Il precedente batching di paragrafi è
eliminato, senza introdurre un nuovo sistema speculativo. La localizzazione
multimodale per pagina, passaggio e contesto, la riconferma snapshot/live e A-06
restano invariate. Nessuna M24.2 o chiamata OpenAI implementata/eseguita.

## File modificati ed eliminati

- `src/pdf_annotator/worker/structured_text.py`: estrazione nativa lineare;
  eliminate `_paragraph_groups`, `_reading_order`, RAWDICT e geometrie aggregate.
- `src/pdf_annotator/domain/structured_text.py`: rimossi `Segment`,
  `StructuredPage`, schema sperimentale e digest/ID di estrazione. Restano
  `PageText` e il binding documento/generazione/revisione usato solo internamente.
- `src/pdf_annotator/services/structured_text_export.py`: streaming del solo
  array `pages`, senza spool dei batch né metadati nel JSON finale.
- **Eliminato** `src/pdf_annotator/services/structured_text_batches.py`.
- `tests/m24_text_fixtures.py`, `tests/test_structured_text.py`,
  `tests/test_structured_text_ui.py`: fixture con spazi intenzionali, contratto
  minimo, confronto nativo e collaudo opzionale del PDF locale di 44 pagine.
- `PROJECT_SPEC.md`, `ARCHITECTURE.md`, `ACCEPTANCE_TESTS.md`, `ROADMAP.md`:
  aggiornato il contratto M24.1b; report precedenti marcati come storici.
- `M24_1B_REPORT.md`: questo report.

L'integrazione File e il controller Qt preesistenti, il trasporto worker,
il motore multimodale, le annotazioni, le dipendenze e la versione 0.18.0
non sono stati modificati da M24.1b.

## Controlli interni conservati

Un worker PDF per documento, elaborazione progressiva per pagina e nessun
PyMuPDF sul thread GUI. Limiti IPC invariati; i dati testuali passano attraverso
file privati con dimensione/hash verificati, senza grandi frame di controllo.
Hash e identità restano interni e non vengono copiati nel JSON esportato.

Ogni pagina deve rispettare documento/generazione/revisione attesi; Save As e
chiusura invalidano il lavoro. L'output viene pubblicato solo dopo completamento,
flush/fsync e sostituzione atomica. Un errore preserva l'output precedente.
I limiti di pagina (250.000 caratteri / 64 MiB serializzati) causano un errore
esplicito dell'export, senza sostituire testo non estratto con una pagina vuota.

## Documento di collaudo

Verificato il PDF locale originale **Due Ragni in un Boccale**, 44 pagine:

- Tutte le 44 pagine esportate coincidono esattamente con l'estrazione nativa
  content-only `sort=False`, dopo la sola sostituzione degli accapo.
- Pagina fisica 25: sequenza «queste astrazioni e di come in loro abbiamo
  riposto speranze deformi di onnicomprensività» presente nell'ordine corretto.
- Pagine 21–24: tutti i caratteri della disposizione espressiva e le sequenze
  di spazi sono conservati; soltanto gli accapo diventano spazi.
- Export nuovo: **117.430 byte** (circa **114,68 KiB**).
- JSON M24.1a preesistente: **317.011 byte**; riduzione di circa **63%**.
  Il file originale e il vecchio JSON non sono stati sovrascritti.

L'export di collaudo è stato scritto nella directory temporanea dei test.

## Tests e acceptance

Prima selezione M24.1b / Qt: **15 passed in 9.70s**.
La verifica finale aggiunge i controlli di dimensione dei file privati e
binding delle risposte, oltre alle regressioni condivise elencate sotto.

```text
.venv\Scripts\python.exe -m pytest tests/test_structured_text.py tests/test_structured_text_ui.py tests/test_review_batches.py tests/test_review_snapshot.py tests/test_review_application.py tests/test_text_selection.py -q -s --tb=short
```

Risultato finale: **68 passed in 57.42s**.
`git diff --check`: **PASS**.
AT-TEXT-01..06: copertura automatica mirata di schema, ordine/fedeltà, spazi,
pagine vuote, esclusione commenti, trasporto, revisioni, lifecycle Qt e
regressioni multimodali. I vecchi test dei paragrafi sono sostituiti perché
quel comportamento è esplicitamente rimosso dal nuovo requisito.

I primi tentativi hanno rilevato il controllo `TextPage.parent` sopra descritto;
corretto localmente e verificato nuovamente. Nessuna full suite o build eseguita.
Gate manuali Adobe: NOT RUN; nessuna nuova dichiarazione di compatibilità.

## Limiti residui

L'ordine nativo dipende dall'autore del PDF: non garantisce un ordine semantico
universale per colonne o contenuti costruiti fuori sequenza. La rimozione degli
accapo perde inevitabilmente la disposizione bidimensionale; non ricompone
lettere distanziate né desillaba. Testo assente o font con mapping difettoso non
vengono ricostruiti e non viene eseguito OCR. Una singola estrazione MuPDF di
pagina resta non interrompibile a metà; le pagine successive sono progressive.

Riferimento verificato: [Page.get_text](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_text)
e controllo del binding installato 1.28.2.
