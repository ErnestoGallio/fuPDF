> Historical report superseded by M24_1B_REPORT.md and the pages-only export.

# M24.1a — Pagine e paragrafi senza geometria persistente

## Implemented

Semplificata M24.1 sul branch esistente `codex/m24-1-text-extraction`.
Le modifiche M24.1 erano già presenti e non committate; vengono aggiornate,
senza toccare release, dipendenze o motore nativo delle annotazioni.

Il JSON usa `fupdf.paragraph-text.v2`: pagina fisica, paragrafi con ID e ordine,
testo e diagnostica, oltre ai batch. Non conserva coordinate, QuadPoints,
dimensioni di pagina, glifi, tabelle di parole o mappe geometriche di caratteri.
La precedente operazione STRUCTURED_TEXT_CONFIRM e il relativo percorso
alternativo di riconferma sono eliminati.

Esempio di contenuto della pagina (ID abbreviato solo per leggibilità):

```json
{
  "page_number": 1,
  "paragraphs": [
    {
      "paragraph_id": "estrazione:p1:b0:p0",
      "reading_order": 0,
      "text": "Prima riga del paragrafo.\nSeconda riga con sillaba-\nzione.",
      "issues": ["line_hyphen_preserved"]
    }
  ],
  "issues": ["paragraph_and_reading_order_heuristic", "cross_page_continuation_not_inferred"]
}
```

Il DTO completo include anche schema e identità documento/generazione/revisione,
per invalidare risultati obsoleti. I batch usano `paragraph_id`, pagina, testo,
intervalli Unicode locali e ruolo target/contesto; nessuna geometria.

## Struttura e localizzazione

I blocchi RAWDICT PyMuPDF sono il primo riferimento. All'interno del blocco,
forti spaziature verticali, nuovi rientri e corsie orizzontali disgiunte danno
evidenza per separare paragrafi. Scarti minimi di allineamento e semplici fine
riga non li separano. Il layout viene usato solo temporaneamente, anche per
l'ordine geometrico tra colonne. Le euristiche non garantiscono una struttura
editoriale corretta per ogni tabella, nota o layout complesso.

I fine riga restano nel testo come evidenza della composizione; non diventano
automaticamente accapo editoriali. Sillabazione e Unicode restano invariati.
Non si forza l'unione di paragrafi fra pagine: le porzioni restano identificate
per pagina fisica, possono condividere batch e contesto adiacente, e la
continuità non inferita è esplicita nella diagnostica.

Per le future correzioni si riusa `review_localization.localize_document`,
con pagina, passaggio originale, contesto precedente/successivo e le regole
esistenti di normalizzazione/localizzazione. Il paragrafo è un aiuto opzionale.
Non esiste un nuovo localizzatore basato sui paragrafi. Le coordinate vengono
recuperate dal PDF nel percorso esistente, quando si prepara l'annotazione,
con successiva riconferma snapshot/live e applicazione A-06 già disponibili.
Una corrispondenza non univoca resta ambigua e non applicabile.

L'export mantiene worker dedicato, richieste per pagina, limiti, file temporanei
privati, scrittura finale atomica e controller Qt in background. Nessuna
estrazione anticipata all'apertura, OCR, chiamata API o nuova modalità IA.

## Files changed

- `domain/structured_text.py`: sostituiti DTO geometrici con pagina/paragrafo.
- `worker/structured_text.py`: euristiche conservative e layout transitorio.
- `services/structured_text_batches.py`: batch di paragrafi senza mappe di glifi.
- `services/structured_text_export.py`: export testuale, rimossa riconferma dedicata.
- `worker/protocol.py`, `worker/client.py`, `worker/document_service.py`:
  eliminata l'operazione di conferma alternativa.
- `tests/test_structured_text.py`, `tests/m24_text_fixtures.py`: aggiornati i
  test al nuovo contratto, rimossi casi geometrici diventati fuori requisito.
- Documenti normativi: aggiornata M24.1a; report M24.1 marcato come superato.

## Tests

```text
.venv\Scripts\python.exe -m pytest tests/test_structured_text.py tests/test_structured_text_ui.py tests/test_review_application.py tests/test_review_batches.py -q --tb=short
-> 30 passed in 31.47s

.venv\Scripts\python.exe -m pytest tests/test_structured_text.py::test_existing_localizer_needs_no_paragraph_id_and_rejects_ambiguity -q --tb=short
-> 1 passed in 1.33s

git diff --check
-> PASS
```

Copertura: paragrafi/fine riga/Unicode/sillabazione, colonne, pagine vuote,
rotazioni, JSON senza geometria, batch attraverso pagine, conservazione del
testo, lifecycle Qt e applicazione multimodale offline. Il test aggiuntivo
verifica esplicitamente ambiguità del testo ripetuto e risoluzione tramite
contesto senza alcun ID di paragrafo. Un tentativo iniziale di aggiornamento
dei test ha fallito per decodifica Windows; corretto usando UTF-8 esplicito.

## Acceptance status e collaudo

AT-TEXT-01..06: PASS per le porzioni automatiche mirate sopra descritte.
Collaudo manuale su PDF editoriali reali e gate Adobe: NOT RUN.
Le euristiche vanno controllate su colonne irregolari, rientri sospesi,
testatine, tabelle e note. Nessuna nuova garanzia di compatibilità Adobe.

Aprire il PDF e usare File > Esporta testo strutturato. Ispezionare `pages`
e `text_batches`: nessun campo geometrico; i fine riga devono restare nel
paragrafo, i confini editoriali plausibili devono separarlo, le pagine restare
fisiche. Provare passaggi a cavallo di pagina e testo ripetuto. L'applicazione
delle correzioni continua a usare il flusso multimodale esistente; la nuova
revisione testuale rimane lavoro futuro.

Nessuna full suite, build, chiamata OpenAI, commit o push.
