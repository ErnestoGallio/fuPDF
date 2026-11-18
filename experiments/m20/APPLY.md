# fuPDF M20.3 — Applicazione nativa e collaudo

Il servizio sperimentale usa un singolo worker persistente per il PDF. Verifica gli SHA-256 di origine e proposte, ricalcola la localizzazione nel worker e confronta il risultato con il piano. Per Replace e Delete verifica gli estremi nell'indice testuale del worker e riconferma testo e quad dell'intera selezione. Per Insert riconferma il carattere, il lato e le due estremità del bordo d'inserimento. Un piano è associato al documento aperto e alla sua generazione. Una geometria non confermata salta la proposta senza retargeting.

Il dispatcher e l'adattatore di fuPDF gestiscono una mutazione e un'unità di cronologia per proposta. Replace, Delete e Insert riusano i gruppi M19 con risposta di motivazione. Lo Sticky Note usa il comando tipizzato `CreateStickyNote` e la stessa transazione e cronologia. L'icona e il popup delle note autonome vengono collocati entro il CropBox. Su una pagina più piccola dell'impronta nativa di 16 punti, il worker rifiuta la nota con `invalid_geometry`.

Dalla radice del repository, usa il PDF originale e i JSON prodotti in precedenza sullo stesso documento:

```powershell
.\.venv\Scripts\python.exe -m experiments.m20.apply `
  'C:\percorso\originale.pdf' `
  'C:\percorso\corrections.json' `
  'C:\percorso\localization.json'
```

La CLI crea una directory `output/m20/application-*` esclusa da Git. `revision.pdf` viene prodotto da Save As alla revisione confermata. `application.json` registra i tre hash di input, l'esito per indice zero-based, il `/NM` e gli xref dei componenti, la destinazione confermata e le verifiche dopo riapertura. `application_report.md` è il riepilogo leggibile. Gli esiti `applied` descrivono le conferme del worker. Usa anche il risultato di salvataggio e verifica per stabilire che siano persistiti. Gli xref servono all'ispezione di questa copia. `/NM` è il riferimento persistente.

## Collaudo in fuPDF

1. Avvia `.\.venv\Scripts\python.exe -m pdf_annotator` da questo checkout e apri `revision.pdf`. Apri la sidebar Commenti e individua le pagine fisiche 4–6, stampate 6–8.
2. Controlla i tre Replace nel box della pagina 6, nella frase su due righe a pagina 7 e nel nodo «Calcolo determinante». Verifica che la selezione origini dal testo barrato e che le motivazioni collegate appaiano distinte dal testo sostitutivo.
3. A pagina 7 controlla il caret subito dopo `return 0;`, il payload con a capo e `}` e la motivazione. A pagina 8 apri le due note native, una presso `Acquisisci();` e una nel margine superiore. Confronta i contenuti con le rispettive motivazioni in `corrections.json`.
4. Per il round trip, salva una nuova copia con Save As, riaprila e confronta tipo, posizione e contenuto con `revision.pdf`. Conserva il campione e gli input come riferimento. La cronologia della sessione applicativa si verifica nei test prima di chiuderla.

## Collaudo in Adobe Acrobat Desktop

Sul campione M20.3 l'utente ha confermato tre Replace, un Insert, le motivazioni collegate e le due Sticky Note. Le verifiche di Delete e dei round trip restano distinte dal collaudo di quel campione.

1. Apri `revision.pdf` e mostra il pannello Commenti. Verifica la semantica dei tre Replace e dell'Insert e l'assenza di duplicati logici separati per la barratura raggruppata. Espandi ciascuna correzione e confronta payload e risposta di motivazione con il JSON originale.
2. Ingrandisci le pagine stampate 6–8. Confronta le barrature multilinea, il nodo del diagramma e il caret dopo il punto e virgola. Apri le due Sticky Note e verifica posizione e testo. Se Acrobat offre una copia di salvataggio, riaprila in fuPDF e ripeti la verifica di tipi, gruppi e risposte.
3. Per collaudare Delete, esegui AT-ACROBAT-06 con la fixture Delete. Il campione reale contiene soltanto Replace, Insert e note. Registra versione di Acrobat ed esiti separati per AT-ACROBAT-07/08/09. InDesign richiede i propri gate con il sorgente originale associato.

## Test mirati

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=short experiments/m20/test_apply.py
.\.venv\Scripts\python.exe -m pytest -q --tb=short `
  experiments/m20/test_apply.py experiments/m20/test_localize.py experiments/m20/test_review.py `
  tests/test_motivated_corrections_m192.py tests/test_annotation_domain.py tests/test_annotation_service.py `
  tests/test_sticky_note_reader.py tests/test_text_selection.py tests/test_m6_service_persistence.py `
  tests/test_save_m15.py::test_save_checkpoint_generation_and_undo_redo
git diff --check
```
