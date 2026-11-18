# M10 — Editorial text corrections interoperability spike

**2026-09-08: primitive e probe implementati; interoperabilità Acrobat e round-trip save/reopen PASS, confermati dall'utente. Suite completa eseguita manualmente dall'utente: PASS, exit code 0. InDesign Import PDF Comments / Accept: NOT RUN, applicazione non disponibile. M10 non è PASS definitivo; M11 non iniziato.** Il preflight del 2026-09-07 era bloccato dalla reference mancante; l'utente ha poi fornito il campione Acrobat e autorizzato il completamento tecnico dello spike senza accesso attuale a InDesign. Nessuna UI, sidebar, AI, Save/Save As di produzione, commit o push.

## Ground truth

Il riferimento storico era un PDF annotato in Acrobat e fornito per il collaudo locale.
Il PDF originale e i dump completi restano materiali privati. La procedura
ripetibile è descritta nelle [fixture pubblicabili](tests/manual/public_pdf_fixtures.md).

Una pagina PDF 1.7, MediaBox/CropBox circa 595.32 × 841.92 pt, rotazione 0. Il testo contiene `The quick brown fox jumps over the lazy dog.`. Il producer del documento base è Microsoft Print To PDF; i Caret contengono in `/RC` il marker `xfa:APIVersion="Acrobat:26.1.0"`. Questo non identifica da solo il build installato. Il documento non è un export InDesign.

L'inspector M10 ha letto ogni entry `/Annots`, l'array indiretto, popup e quattro appearance stream: 14 oggetti nel grafo, inclusa la pagina come confine della visita. L’ispezione originale ha conservato la reference invariata.

## Encoding empiricamente osservato

Gli xref seguenti sono locator della sola reference, mai costanti nell'adapter.

| Operazione | Root e componenti osservati | Target e payload |
| --- | --- | --- |
| Delete `brown` | xref 29: `/Subtype /StrikeOut`, senza `/IT`, `/RT`, `/IRT`; popup 30 | `/QuadPoints` ordinati sul range; `/Contents` assente |
| Insert | xref 35: `/Subtype /Caret`, senza `/IT`, `/RT`, `/IRT`; popup 36 | posizione tramite `/Rect` e `/RD`; `/Contents ( very)` e `/RC` corrispondente |
| Replace `lazy` → `sleepy` | root 33: `/Subtype /Caret /IT /Replace`; membro 31: `/Subtype /StrikeOut /IT /StrikeOutTextEdit /RT /Group /IRT 33 0 R`; popup 34 e 32 | range nei quad dello StrikeOut; payload `sleepy` nel Caret root |

**Dettaglio rispetto alla descrizione iniziale:** il payload Insert effettivo è `" very"`, con uno spazio iniziale. Il probe conserva quel payload dopo `The`. Nessun trim, casefold, NFKC o interpretazione di markup viene applicato al testo utente. Il Caret Acrobat è prossimo al bordo dopo `The`; il nostro confine è il bordo esatto del carattere confermato M6, senza riprodurre un offset di click arbitrario.

Replace è composto: lo StrikeOut punta al Caret mediante **IRT + RT Group**. Direzione del riferimento e due intent sono riprodotti esattamente. Il Caret non ha un IRT inverso. Non sono stati usati FreeText, Callout, modifiche al testo originale o flattening.

```text
Delete:  StrikeOut ──Popup──> Popup ──Parent──> StrikeOut
Insert:  Caret ──────Popup──> Popup ──Parent──> Caret
Replace: StrikeOut (/IT StrikeOutTextEdit, /RT Group)
           ├─ IRT ─────────> Caret (/IT Replace, /Contents sleepy)
           └─ Popup          └─ Popup
```

Delete e Insert hanno ciascuno una primaria più popup; Replace ha due primarie collegate più due popup. Il popup è un accessorio, non una correzione indipendente. Replace rimane una sola operazione logica.

### Inventario dei campi richiesti

| Campi | Reference Acrobat | Probe |
| --- | --- | --- |
| `/Subtype`, `/IT`, `/RT`, `/IRT` | struttura descritta sopra | stessa struttura, riferimenti dinamici |
| `/Rect` | bounds StrikeOut; Caret a fine range o confine | derivati da quad/confine canonici M6 |
| `/QuadPoints` | 8 numeri su ogni StrikeOut; assenti sui Caret | quad confermati, anche multilinea |
| `/Contents` | assente sugli StrikeOut; `sleepy` / ` very` sui Caret | stessa distribuzione; Unicode aggiuntivo preservato |
| `/NM` | quattro UUID distinti sulle primarie; popup senza nome | identità logica sul root; suffix `:strike` sul membro Replace; popup senza nome |
| `/T`, `/M`, `/CreationDate` | autore della fixture storica, date Acrobat | autore `PDF Annotator`, date del run |
| `/Popup`, `/Open`, `/Parent` | link reciproci e popup chiusi | medesima relazione; Open false |
| `/State`, `/StateModel` | assenti | assenti |
| `/RC` | XHTML con payload, namespace xfa e marker versione Acrobat | XHTML fisso escaped, senza falso marker Acrobat |
| `/DA`, `/LE`, `/Sy` | assenti | assenti |
| `/RD` | margini sui Caret, circa .741 pt Replace / .723 pt Insert | margine proporzionale all'altezza del carattere |
| `/AP /N` | quattro XObject Form con stream vettoriali | appearance native PyMuPDF, separate dal contenuto pagina |
| `/C`, `/F`, `/Subj`, `/P` | rosso Delete/Replace, viola Insert; F=4 e popup F=28; subject localizzati; link pagina | stessi colori/flag, subject inglesi, pagina effettiva |

Il testo originale non è copiato in un Contents dello StrikeOut: resta nel contenuto base e nei quad. Nel dominio resta anche `TextRangeTarget.original_text` come consistenza. Non sono emersi altri collegamenti editoriali, allegati o oggetti privati fuori dal grafo descritto.

### Standard PDF, Acrobat e semantica

- **A — strutture PDF:** Caret/StrikeOut, quad, appearance, popup, Contents, identità e relazioni IRT/RT sono strutture di annotazione PDF. Il solo subtype non dimostra interoperabilità.
- **B — Acrobat:** la combinazione specifica di intent `Replace` / `StrikeOutTextEdit`, RT Group, direzione IRT e collocazione del payload è la ground truth riprodotta, non un'ipotesi ricavata dalla somiglianza visiva.
- **C — semantica:** lo spike conserva l'intera combinazione osservata. Non è un esperimento di eliminazione delle singole chiavi e non dimostra il sottoinsieme minimo sufficiente a ogni lettore.
- **D — metadata/presentazione:** timestamp, autore, nomi effettivi, subject localizzato, posizione del popup e byte del disegno AP non sono confrontati letteralmente. RC resta serializzazione fissa del plain text.
- **E — Adobe/InDesign:** namespace xfa e marker versione compaiono nel RC Adobe; non vengono falsificati. Il gate manuale comunicato dall'utente conferma il riconoscimento del nostro probe in Acrobat e il round-trip save/reopen. L'applicabilità InDesign resta non verificata.

## Implementazione e identità

Il percorso è **AnnotationCommand → AnnotationService → IPC revisionato → dispatcher → ProductAnnotationAdapter → editorial_adapter → PyMuPDF**. I tre comandi sono typed e serializzabili. Il runner è soltanto un caller di sviluppo, senza nuove API o UI.

Delete/Replace riusano `WorkerTextService.resolve` e `require_confirmed`, snapshot, indici globali inclusivi M6 e quad ordinati. Preview/commit non ricercano nuovamente una stringa. Sono coperti reverse, multilinea e selezione nella stessa colonna.

Insert estende il target esistente con `snapshot_id`, `char_index` e `boundary_top`; `anchor` è l'estremo inferiore dello stesso bordo. `TEXT_INSERTION` conferma BEFORE/AFTER di uno specifico carattere M6, distinguendo estremi delle righe e occorrenze ripetute. Non introduce una seconda estrazione. La cache delle conferme è condivisa e limitata a 64 range/confini; target espulsi, alterati, obsoleti o di un altro documento sono rifiutati. Nessun confine viene inventato su pagine senza testo.

Il root porta `/NM = annotation_id`. Il membro Replace porta `/NM = annotation_id + ':strike'`. I nomi distinti persistono dopo reopen. `PdfBackingReference` contiene il root e i locator associati, inclusi popup; gli xref non sono identità. M11/M12 potranno usare il singolo DTO; inventario interattivo/sidebar/undo non sono implementati qui.

La creazione controlla collisioni NM e target, crea il gruppo in una transazione locale e restituisce un solo modello. Il dispatcher incrementa la revisione una volta, dopo il completamento. DeleteAnnotation verifica il gruppo app-owned e rimuove tutte le primarie e i popup insieme. Una relazione esterna incidente provoca rifiuto read-only invece di cancellare reply estranei.

Rollback: snapshot del dizionario pagina, dell'eventuale array Annots indiretto e dei componenti esistenti toccati dalla cancellazione. Su errore vengono ripristinati, i nuovi oggetti diventano null e la pagina viene ricaricata. Se il rollback stesso fallisce, il documento viene chiuso per impedire ulteriori scritture su uno stato incerto; non viene dichiarato pulito/salvato. Slot xref null o AP non più referenziati possono restare nel contenitore fino a un'eventuale garbage collection; non restano frammenti di correzione raggiungibili in Annots. Nessuna garbage collection di produzione è aggiunta.

### API e operazioni xref

- Pubbliche: `add_strikeout_annot`, `add_caret_annot`, `set_info`, `set_colors`, `set_flags`, `update`, `set_popup`, `set_open`, `set_irt_xref`, `reload_page`.
- `xref_set_key`: NM (set_info non imposta id), IT, RT, Contents, RC, Rect/RD del Caret, BBox del suo AP, flag popup. `xref_object`/`xref_get_key` leggono strutture; `update_object` serve a rollback/rimozione atomica. Nessun byte patching o xref hardcoded.
- In PyMuPDF 1.28.2, Caret.set_rect seguito da update ripristina l'icona a 20×14 pt. Dopo l'AP pubblico, l'adapter imposta bounds derivati dal confine e include nell'AP l'inset RD.
- Le proporzioni circa .70/.55/.055 dell'altezza del carattere per larghezza interna/altezza/margine derivano dal Caret Replace osservato. Sono parametri di appearance, non regole InDesign. Il centro inferiore interno coincide col bordo M6, senza offset zoom/DPR.
- La conversione al Rect PDF viene ricavata dai bounds API/nativi della nuova annotazione, evitando le particolarità di Page.transformation_matrix con rotazione e CropBox. Le aspettative geometriche dei test sono indipendenti dal mapper.

Nessuna dipendenza runtime aggiunta; PyMuPDF resta 1.28.2. Il font CJK della fixture è incorporato tramite `Font('cjk').buffer`, senza dipendere dai font installati.

## Probe e confronto

Output: **`output/m10/probe/m10_editorial_comments_probe.pdf`**.
Sorgente sintetico: `output/m10/probe/m10_editorial_source.pdf`.
Comandi e identità: `output/m10/probe/m10_probe_manifest.json`.
Ispezione: `output/m10/probe/m10_probe_inspection.json`.
Confronto: `output/m10/probe/m10_structure_comparison.json`.

| Caso | Probe | Confronto |
| --- | --- | --- |
| Delete brown | StrikeOut senza intent/reply, quad e popup | stessa struttura |
| Insert dopo The, payload `" very"` | Caret senza intent/reply, Contents/RC e popup | stessa struttura/payload |
| Replace lazy → sleepy | Caret Replace + StrikeOutTextEdit, Group/IRT al root | stessa struttura/payload |
| Replace editable con Unicode/literal | stessa struttura composta; `città, perché, Über, 日本語 & <ok> é ①` | estensione automatizzata, assente nella reference |
| Delete multilinea | due quad da reverse selection | estensione automatizzata, assente nella reference |

Le firme confrontano subtype, intent, RT/IRT relativo, payload, componenti/popup e numero di quad: **PASS strutturale per i tre casi**. Il test confronta geometria e preservazione del grafo sintetico. Il confronto con oggetti realmente scritti da Acrobat richiede un riferimento Adobe fornito localmente. La serializzazione può riordinare chiavi o trasformare `[/FlateDecode]` in `/FlateDecode`: i byte dei relativi stream restano identici.

Testo estratto, hash dei singoli content stream e raster base con annots=False sono invariati. Probe e reference sono stati renderizzati con Poppler e ispezionati visivamente. Nessun glyph CJK mancante nel probe finale. Poppler emette messaggi generali di configurazione font Symbol/ArialUnicode; il contenuto incorporato è correttamente visibile. Questo non è un gate Acrobat.

## Riproduzione e verifiche

L'inspector richiede un output nuovo:

```powershell
.\.venv\Scripts\python.exe -m pdf_annotator.spikes.editorial_inspector output/m10/acrobat-reference.pdf --include-streams --output output/m10/reference-inspection.json
```

Il runner richiede una directory nuova per non sovrascrivere sorgenti:

```powershell
.\.venv\Scripts\python.exe -m tests.m10_probe output/m10/probe
```

La fixture viene creata e chiusa offline prima dell'ownership worker. Il runner usa AnnotationService e il vero adapter; l'export usa e getta avviene nel worker, poi viene ispezionato offline. Non qualifica Save/Save As di produzione.

Test eseguiti:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_editorial_m10.py tests/test_editorial_persistence_m10.py -q
```

**22 passed**, ultimo run 145.42 s.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_annotation_domain.py tests/test_annotation_service.py tests/test_text_selection.py tests/test_m6_service_persistence.py tests/test_protocol.py tests/test_m7_service_persistence.py tests/test_freetext_m8.py::test_create_persists_native_freetext_plain_text_independent_style_and_nm tests/test_callout_m9.py::test_callout_native_rich_create_resize_anchor_persists -q
```

**60 passed**, 101.32 s. Suite completa non eseguita da Codex durante lo spike, come richiesto; successivamente eseguita manualmente dall'utente: **PASS, exit code 0**. Comando, conteggio e durata di questo run manuale non sono stati comunicati. Nessun test rieseguito per questo aggiornamento documentale.

Controlli finali: `git diff --check` PASS (solo avvisi Git LF/CRLF); `compileall -q` sui moduli e test M10 modificati PASS. `Get-Process -Name python,pythonw -ErrorAction SilentlyContinue`: nessun processo Python residuo dopo la chiusura dei test e del runner. SHA-256 del probe finale verificato contro il confronto JSON; dump dell'inspector riallineati al PDF consegnato. Rimossi soltanto gli output temporanei prodotti durante questo spike.

Copertura M10: DTO JSON/Unicode, service/worker, reference, NM, type/intent/group, quad/caret, save/reopen, contenuto originale e annotazioni esterne, delete completo, errori dopo StrikeOut/Caret/link e durante delete, revisioni, target obsoleti/alterati/cross-document, cache conferme, colonna e no-text. Il vecchio test preflight ora verifica STALE_TEXT_TARGET per target non confermati anziché imporre UNSUPPORTED_COMMAND a tutte le primitive.

| Acceptance | Stato |
| --- | --- |
| AT-CMD-01, AT-EDIT-01..03 | PASS per le porzioni automatiche dello spike; gate Acrobat PASS, gate InDesign NOT RUN |
| AT-EDIT-04 | PASS targeting/atomicità/revisioni/preservazione coperti; UI/sidebar/undo fuori M10 |
| AT-COORD-01/02, parti editoriali | PASS automatico su quattro rotazioni e CropBox |
| AT-ACROBAT-06..08 | PASS, gate manuale confermato dall'utente: riconoscimento/visualizzazione, range, payload e raggruppamento Replace corretti |
| AT-ACROBAT-09 | PASS, round-trip Acrobat save/reopen confermato dall'utente |
| AT-INDESIGN-01..04 | **NOT RUN: InDesign non disponibile** |

## Gate manuali e limiti aperti

**Acrobat: PASS storico, esito manuale comunicato dall'utente.** Il probe del collaudo originale apre correttamente. Delete, Insert e Replace sono riconosciuti e visualizzati correttamente. Range, payload e raggruppamento Replace risultano corretti. Anche il round-trip Acrobat save/reopen del probe storico è **PASS**. Versione/build Acrobat e percorso della copia salvata non sono stati comunicati. La rigenerazione delle fixture richiede un nuovo collaudo sul campione pubblicabile.

**InDesign, successivamente: NOT RUN.** Preparare una coppia sorgente .indd / export PDF invariata; annotare quell'export con le medesime primitive. Ripartire da una copia fresca del sorgente per ciascun caso: File > Import PDF Comments, verificare tipo/target/payload, scegliere manualmente Accept e confrontare il testo risultante esatto. Ripetere i percorsi via Acrobat di AT-INDESIGN-04. La reference attuale e la fixture sintetica non sostituiscono questa coppia.

La reference copre tre casi ASCII, una riga e testo orizzontale. Il gate Acrobat sul probe è PASS; per Unicode/multilinea/trasformazioni restano disponibili le evidenze automatiche, senza un dettaglio manuale distinto per ciascuna variante. Caret su run oblique/verticali e altre varianti Adobe non hanno ground truth empirica in questa reference. Mapping interattivo dopo riapertura, sidebar e undo restano ai milestone successivi. L'assenza di InDesign non ha alterato l'encoding osservato, ma mantiene chiuso l'exit gate verso M11.

## File coinvolti

- `src/pdf_annotator/worker/editorial_adapter.py`: encoding, geometria, identità, creazione/delete/rollback.
- `src/pdf_annotator/worker/annotation_adapter.py`: dispatch e delete completo.
- `src/pdf_annotator/domain/targets.py`, `serialization.py`: target Insert confermato.
- `src/pdf_annotator/worker/text_service.py`, `protocol.py`, `document_service.py`: conferma confini con M6/IPC.
- `src/pdf_annotator/spikes/editorial_inspector.py`: campi diagnostici e array Annots indiretto.
- `tests/m10_helpers.py`, `tests/m10_probe.py`, `tests/test_editorial_m10.py`, `tests/test_editorial_persistence_m10.py`: fixture, runner, confronto e test.
- `M10_REPORT.md`, `ROADMAP.md`: conclusioni e gate.
- `output/m10/probe/m10_*`: sorgente/probe, manifest, ispezioni e confronto.

Erano già presenti i file del preflight M10 e la reference dell'utente: preservati/estesi. La reference è invariata. Nessun commit/push; M11 non iniziato.

## Fonti ufficiali consultate

- [PyMuPDF Page — Caret e markup](https://pymupdf.readthedocs.io/en/latest/page.html)
- [PyMuPDF Annot — geometria, info, popup e IRT](https://pymupdf.readthedocs.io/en/latest/annot.html)
- [PyMuPDF Document — xref e stream](https://pymupdf.readthedocs.io/en/latest/document.html)
- [Adobe InDesign — Import PDF Comments](https://helpx.adobe.com/indesign/desktop/collaborate-and-review/share-and-collaborate/import-pdf-comments.html)
