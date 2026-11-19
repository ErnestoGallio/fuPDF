# M23.1 — Supporto alle annotazioni native Line

Implementazione completata sul branch `codex/m23-1-line`, creato dal `main`
pubblico aggiornato `abe43a9`. Worktree:
`C:\Users\matte\Desktop\pdf_editor\tmp\m23-1-fupdf`.
Il checkout pubblico principale era pulito. Nessuna modifica preesistente da
preservare nel worktree. Nessun commit, push, merge, tag, build Windows o chiamata
API OpenAI. Versione 0.18.0, tag e distribuzione Windows invariati.

## Supporto precedente e lacune

- La scansione nativa enumerava già `/Line` come tipo generico `line`, senza
  esporre gli estremi. La pagina mostrava il segnaposto generico del bounding box.
- La selezione usava l'intero rettangolo; il profilo esterno non consentiva
  spostamenti e la cancellazione escludeva Line dall'elenco dei tipi ammessi.
- Erano già disponibili inventario/sidebar, `EditExternalAnnotation(MOVE)`,
  `DeleteExternalAnnotation`, gesto con anteprima locale, snapshot nativi per
  undo/redo, revisioni, invalidazione locale delle apparenze e Save/Save As.
  M23.1 riutilizza questi percorsi, senza introdurre comandi di creazione Line.

## Modifiche e comportamento

- Gli estremi ordinati sono in `AnnotationGeometry.line`, distinti da Ink e
  Callout, e attraversano sia inventario sia risultati di modifica/cronologia.
- La sidebar identifica il tipo come **Linea**. La scheda dettaglio esistente
  espone estremi in punti, colore, spessore, terminazioni e metadati disponibili.
- La pagina usa il raster nativo dell'annotazione nel cache globale già limitato.
  Anche le Line senza `/AP` sono visualizzate dal worker, senza scrivere un'AP
  nel PDF. Le linee del contenuto della pagina restano escluse dall'inventario.
- Il click misura la distanza dal segmento in coordinate viewport: tolleranza
  minima 5 pixel logici, aumentata per lo spessore. Gli spazi vuoti del bounding
  box diagonale non selezionano la Line. Nessuna maniglia per gli estremi.
- Durante il drag viene traslata l'apparenza nativa in Qt senza inviare mutazioni.
  Il rilascio invia un solo comando esterno MOVE attraverso A-06. Il worker
  trasla `/L` e `/Rect` dello stesso vettore canonico e aggiorna soltanto `/M`.
  Non ricrea l'annotazione e non chiama `Annot.update()`.
- Lunghezza, inclinazione, terminazioni, colori, tratteggio, spessore, opacità,
  autore, contenuto, proprietà private e stream/risorse AP restano preservati.
- La cancellazione ordinaria rimuove realmente il grafo nativo verificato,
  incluso l'eventuale Popup, e aggiorna viewer, inventario e sidebar.
- Undo/redo usa gli snapshot nativi esistenti; funziona anche dopo la barriera
  di generazione di Save As. Save incrementale e Save As persistono geometria
  e cancellazione. Nessuna modifica alla pipeline di salvataggio.
- Esc e il ritorno al punto iniziale non inviano mutazioni. Comandi stale,
  duplicati, spostamenti sotto la soglia e un errore simulato dopo la scrittura
  non lasciano geometria parziale o revisioni riuscite fittizie.
- La copertura del raster Line viene riportata alle coordinate canoniche:
  nella versione installata 1.28.2 i pixel prodotti da `Annot.get_pixmap()`
  includono già la rotazione pagina. Test indipendenti verificano che il mapper
  del viewer non applichi due volte rotazione o offset CropBox.

## File modificati

| File | Scopo |
| --- | --- |
| `src/pdf_annotator/domain/annotation_inventory.py` | Estremi Line nel DTO |
| `src/pdf_annotator/domain/serialization.py` | Estremi nei delta di modifica e cronologia |
| `src/pdf_annotator/worker/annotation_inventory.py` | Lettura estremi/terminazioni e serializzazione inventario |
| `src/pdf_annotator/worker/external_profiles.py` | Capacità di sola traslazione per Line valide |
| `src/pdf_annotator/worker/external_edit_adapter.py` | Traslazione nativa con allowlist `/L`, `/Rect`, `/M` |
| `src/pdf_annotator/worker/external_delete.py` | Cancellazione nativa Line nel percorso comune |
| `src/pdf_annotator/worker/annotation_appearance.py` | Apparenza nativa Line limitata e copertura canonica |
| `src/pdf_annotator/viewer/pdf_view.py` | Hit testing sul segmento e cache AP esistente |
| `src/pdf_annotator/viewer/external_interaction.py` | Cursore sul segmento e riuso del drag nativo |
| `src/pdf_annotator/app/annotation_sidebar.py` | Etichetta e proprietà di sola lettura |
| `tests/m231_helpers.py` | Fixture, ispezione PDF isolata, fault injection |
| `tests/test_line_m231.py` | 28 casi M23.1 attraverso i percorsi di produzione |
| `PROJECT_SPEC.md`, `ARCHITECTURE.md`, `ACCEPTANCE_TESTS.md`, `ROADMAP.md` | Eccezione M23.1 esplicitamente richiesta e criteri AT-LINE |
| `M23_1_REPORT.md` | Questo report |

## Test e comandi

Ambiente verificato: Python 3.14.10, PySide6 6.11.2, PyMuPDF 1.28.2 del repository
pubblico. Solo test pertinenti; full suite **NON ESEGUITA**, come richiesto.
Da questo worktree, i comandi seguenti usano:

```powershell
$env:PYTHONPATH = "$PWD/src"
$python = 'C:/Users/matte/Desktop/fuPDF/.venv/Scripts/python.exe'
```

1. Verifica finale completa M23.1:

```powershell
& $python -m pytest tests/test_line_m231.py -q --maxfail=2
```

**28 PASS**. Include Line orizzontali/verticali/diagonali/invertite, senza AP,
Popup, metadati, conservazione degli stream, Save/Save As/riapertura e cronologia;
UI a 100/150/400/800%, rotazioni 0/90/180/270, CropBox non nullo, tre layout,
proprietà/sidebar, singolo comando al rilascio, anteprima raster rigida,
annullamento/no-op, duplicati/stale e rollback dopo errore.

2. Selezione di regressione sui percorsi condivisi:

```powershell
& $python -m pytest tests/test_line_m231.py tests/test_ink_m7.py tests/test_annotation_domain.py tests/test_annotation_service.py tests/test_annotation_inventory_m12.py tests/test_m16_annotation_appearance_invalidation.py tests/test_undo_redo_m14.py tests/test_save_m15.py tests/test_editorial_persistence_m10.py tests/test_annotation_replies_m191.py tests/test_motivated_corrections_m192.py -q --maxfail=5
```

**213 PASS, 1 errore ambientale**: il sandbox Windows blocca `os.link` con
`WinError 5` nel test preesistente sugli alias Save As. Nessun test indebolito.
Il solo caso è stato rieseguito fuori dal sandbox, su PDF temporanei:

```powershell
& $python -m pytest tests/test_save_m15.py::test_save_as_collision_cancel_overwrite_and_external_conflict -q --basetemp=tmp/pytest-m231-hardlink
```

**1 PASS**. La selezione è quindi verificata integralmente nei suoi 214 casi.

3. Regressione delle annotazioni esterne:

```powershell
& $python -m pytest tests/test_external_edits_m151.py -q -k 'production_gate or native_edit_save_history_preservation or external_viewer_gestures_emit_one_command' --maxfail=3
```

**59 PASS, 35 non selezionati**. Include profili esistenti, conservazione nativa,
cronologia, persistenza e gesti FreeText/Callout/Ink. Il registro storico delle
capacità verificate in Acrobat rimane invariato.

4. Regressione interattiva e persistenza Ink:

```powershell
& $python -m pytest tests/test_m7_viewer.py tests/test_m7_service_persistence.py -q --maxfail=3
```

**32 PASS**.

5. `git diff --check` → **PASS**, nessun output.

305 casi distinti pertinenti verificati complessivamente. Le ripetizioni dei
28 casi Line non vengono sommate al totale.

## Stato di accettazione e limiti

- AT-LINE-01/02/03: **PASS automatizzato**.
- AT-CMD-01, AT-ANN-10/11/12, AT-COORD-01/02, AT-SAVE-01/02:
  **PASS per i sottocasi automatizzati pertinenti**; non è una nuova qualifica
  manuale dell'intera applicazione.
- Regressioni automatizzate Ink, Delete/Insert/Replace, risposte/motivazioni,
  cronologia e Save: **PASS** nei test sopra elencati.
- AT-LINE-04 e collaudi manuali Adobe: **NOT RUN**. Le fixture Line sono
  sintetiche; nessuna nuova prova con Line effettivamente create in Acrobat.
- Le protezioni esistenti restano attive per geometrie malformate, flag che
  vietano lo spostamento e relazioni dipendenti. La cancellazione richiede un
  grafo sicuro: non vengono lasciate risposte esterne orfane. Nessun trasferimento fra
  pagine, ridimensionamento, rotazione o modifica separata degli estremi.
- L'abilitazione conservativa Line richiesta da M23.1 è esplicita e separata
  dal registro storico M15.1: non viene dichiarato un gate Acrobat mai eseguito.

## Collaudo manuale consigliato

1. Usare il checkout di sviluppo M23.1, lasciando invariata la stabile Windows.
   Creare in Acrobat un PDF con Line sottili, diagonali, tratteggiate, terminazioni
   diverse e commenti/autore; includere Ink e correzioni editoriali di controllo.
2. Aprire in fuPDF e verificare pagina, sidebar, proprietà e click sul tratto.
   Trascinare a zoom diversi, su pagina ruotata e nei tre layout. Verificare
   traslazione rigida, assenza di maniglie agli estremi ed Esc.
3. Cancellare, annullare e ripetere; provare undo/redo dello spostamento.
4. Eseguire Save e Save As, riaprire in fuPDF e Acrobat: verificare geometria,
   terminazioni, autore/commenti, tipo Line nativo ed editabilità in Acrobat,
   insieme alla conservazione degli oggetti di controllo.

Riferimenti API ufficiali consultati: [Annot](https://pymupdf.readthedocs.io/en/latest/annot.html)
per estremi, terminazioni, apparenza e metadati;
[Page](https://pymupdf.readthedocs.io/en/latest/page.html) per coordinate non ruotate
e derotation matrix. Il comportamento specifico 1.28.2 è verificato dai test.
