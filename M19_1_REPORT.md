# M19.1 - Lettura delle motivazioni collegate

## Implemented

Implementata la lettura delle risposte PDF `/Text` e la loro presentazione insieme
all'annotazione principale, senza modificare il PDF, la capability policy o la
pipeline Save/Save As. La milestone resta da collaudare manualmente in fuPDF.

La richiesta M19.1 è il perimetro di questa iterazione; non è stata aggiunta una
milestone al ROADMAP né sono stati modificati i documenti normativi. Copertura:
parti pertinenti di AT-SIDE-02/03/04, AT-ANN-12, AT-EDIT-04 e AT-SAVE-01/02/04.

Stato iniziale: HEAD `14a96dc61d28e20d897e0cec50509c18ad1b0575`, working tree con
i soli risultati M19 non committati (`M19_REPORT.md`, `experiments/`,
`output/pdf/m19/`). Quei file sono stati preservati. Nessuna modifica alla release
stabile `C:\fuPDF\stable\`, nessuna dipendenza aggiornata, packaging, commit,
push o tag. Il riscontro Acrobat positivo è riferito dall'utente; nessuno stato
dei gate Adobe è stato aggiornato automaticamente.

## Architettura e associazione

- `AnnotationReply` conserva xref e valore originale `/IRT`, valore originale
  `/RT`, radice logica risolta ed esito della risoluzione. I summary conservano
  anche `/NM` originale e gli ID delle risposte associate. Xref e `/NM` non
  vengono modificati; nomi duplicati non sono usati per stabilire il legame.
- Il resolver opera nel worker proprietario, leggendo i riferimenti PDF.
  Non usa autore, testo, coordinate o prossimità. Accetta `/RT /R` e l'assenza
  di `/RT`, il cui significato predefinito è R. Le relazioni diverse restano
  indipendenti. La distinzione deriva dalla
  [PDF Reference Adobe 1.6, tabella 8.17](https://opensource.adobe.com/dc-acrobat-sdk-docs/pdfstandards/pdfreference1.6.pdf).
- Il percorso delle risposte viene seguito iterativamente, con memoizzazione e
  rilevamento dei cicli. Le risposte a risposte sono raccolte sotto la radice
  finale, senza introdurre un editor o un sistema generale di conversazioni.
- Tutte le risposte continuano a essere summary nell'inventory e oggetti nel
  PDF. Il filtro è esclusivamente di presentazione. Il testo completo arriva
  tramite gli stessi dettagli a chunk da 48 KiB già esistenti; niente nuovo IPC.
- Le risposte sono ordinate secondo la posizione nativa nell'array `/Annots`.
  Non vengono riordinate secondo autore, testo o date potenzialmente mancanti.
- Autore, date, nome nativo, contenuto integrale e subject disponibile restano
  accessibili attraverso summary/detail. Il subject del primo chunk è mantenuto
  anche quando il testo richiede più risposte IPC.

### Replace composto

La radice rimane il Caret `/IT /Replace`. Il suo StrikeOut
`/IT /StrikeOutTextEdit`, `/RT /Group`, `/IRT <Caret>` rimane un componente della
correzione: non è una motivazione. I quad derivano dallo StrikeOut, il payload
editoriale dal Caret. Una risposta normale che punta allo StrikeOut viene
ricondotta alla stessa radice logica, conservando il riferimento PDF originale.

Sul campione M19: Caret 47 + StrikeOut 44 compongono Replace; risposta Text 50
punta a 47 con RT R. Delete 53 ha risposta 56; Insert 59 ha risposta 62.
Le risposte non entrano in `component_xrefs` della correzione e non diventano
oggetti da cancellare implicitamente insieme a essa.

## Sidebar e viewer

- La lista mostra soltanto la voce principale per una correzione con risposte.
  I dettagli distinguono `Contenuto editoriale` da `Motivazioni / risposte`.
  Ogni risposta è numerata, con autore o `Autore non indicato` e testo completo.
- Si riusa il QPlainTextEdit graphite in sola lettura: selezione, copia,
  Unicode, testo multilinea e scrolling restano disponibili. Nessun rich text
  della motivazione viene interpretato come HTML o mescolato al payload.
- Per altri tipi di annotazione il contenuto principale è indicato come
  `Contenuto annotazione`. Una normale nota con risposte mantiene il suo popup
  flottante sul testo principale e rende le risposte consultabili nella sidebar.
- Le risposte associate non disegnano icone indipendenti e non partecipano
  all'hit-testing. La selezione di un loro ID viene ricondotta al padre.
  Un lettore flottante obsoleto su una risposta associata viene chiuso.
- Le normali Sticky Notes senza legami mantengono icona, popup, selezione e copia.
  Renderer base, appearance PDF e coordinate canoniche non sono stati cambiati.

### Riferimenti anomali

Riferimenti mancanti, non validi, ciclici, fra pagine diverse, a Popup non
risolvibili o con RT non supportato restano note indipendenti. La sidebar mostra
un avviso comprensibile e consente di leggere/copiarne il testo. Gli esiti
diagnostici strutturati rimangono nei summary e nei test. Nessun caso viene
normalizzato nel PDF o nascosto sulla base di una supposizione.

Se il padre non è presente nell'inventory corrente, la risposta non viene
filtrata, anche durante un aggiornamento per delta: un'orfana resta accessibile.

## Caricamento asincrono e invalidazione

Una sola richiesta di dettaglio rimane in volo per documento. La sidebar carica
prima il contenuto principale e poi le risposte mancanti. Si conserva al massimo
un target successivo, quello più recente: quando cambia la selezione, la lettura
precedente viene ritirata al successivo chunk, senza sovrascrivere la nuova UI.

L'adozione controlla documento, generazione, request ID, operazione, logical ID e
avanzamento dell'offset. Un risultato con identità obsoleta viene scartato e
ritentato una sola volta; fallimenti ripetuti o chunk senza avanzamento diventano
errori terminali leggibili. Una motivazione fallita non blocca le altre.
Errori e richieste pendenti vengono resettati al cambio di generazione.

Move/resize/edit esterni e relativo undo/redo non cambiano IRT/RT: gli aggiornamenti
incrementali mantengono i metadati delle risposte senza invalidare l'intera lista.
Questo vale anche dopo una precedente invalidazione lazy dovuta a un'app mutation.
Soltanto la rimozione di elementi di un grafo collegato richiede una nuova
risoluzione dell'inventory. Save e riapertura usano l'invalidazione già esistente;
nessuna transazione di salvataggio è stata cambiata.

## Cancellazione attuale e proposta M19.2

La policy preesistente è preservata:

- `safe_components()` rifiuta la cancellazione esterna di un padre con risposte
  entranti e la cancellazione autonoma di un reply/group member.
- Il worker editoriale rifiuta anche la cancellazione app-owned quando rileva
  riferimenti IRT esterni ai componenti della correzione.
- La lettura e il raggruppamento non concedono nuove capability di cancellazione.
  Nessuna risposta viene cancellata o normalizzata da M19.1.

Per M19.2 è opportuno progettare un comando esplicito di cancellazione dell'intera
correzione motivata: radice, membri Replace, risposte e relativi Popup come una
singola operazione atomica, revisionata e undoable, con ripristino esatto del grafo.
Prima di abilitarlo occorre definire il trattamento delle risposte esterne e dei
legami anomali. Questa proposta non è implementata qui.

## Files changed

- `domain/annotation_inventory.py`: DTO della relazione e filtro di presentazione
  condiviso che rende visibili le orfane.
- `domain/serialization.py`: preserva gli stessi metadati anche nel percorso dei
  risultati di mutazione e dei delta undo/redo, oltre ai summary dell'inventory.
- `worker/annotation_replies.py`: resolver in sola lettura, gruppi, catene e cicli.
- `worker/annotation_inventory.py`: associazione ai summary, serializzazione e
  mantenimento dei legami durante i delta.
- `worker/document_service.py`: segnala l'eventuale invalidazione strutturale
  usando il flag di refresh già esistente.
- `app/document_session.py`: stato di lettura pendente, subject, retry ed errori.
- `app/document_manager.py`: lettura sequenziale, target più recente, controlli
  sugli offset e sulle identità, adozione dei delta e reset di generazione.
- `app/annotation_sidebar.py`: raggruppamento, sezioni, autori e contenuti integrali.
- `viewer/pdf_view.py`, `viewer/sticky_notes.py`: esclusione delle risposte dalla
  pagina e dal popup autonomo, selezione ricondotta alla radice.
- `tests/m191_fixtures.py`, `tests/test_annotation_replies_m191.py`: fixture e
  regressioni M19.1, con setup/ispezione PDF in processi separati dalla GUI.
- `tests/test_sticky_note_reader.py`: la vecchia aspettativa di reply autonomo è
  sostituita dal comportamento richiesto; aggiunta la prova di una nota padre.
- `tests/test_external_edits_m151.py`: il conteggio delle righe considera le
  sole annotazioni visibili; le risposte rimangono nell'inventory completa.
- `M19_1_REPORT.md`: questo report.

I percorsi dei moduli sono relativi a `src/pdf_annotator/`; i percorsi dei test
sono relativi alla radice del repository.

## Tests

Verifiche finali dopo l'allineamento di entrambi i percorsi di serializzazione:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_annotation_replies_m191.py tests/test_undo_redo_m14.py --basetemp tmp/pytest-m191-final-dto -q
# 32 passed in 36.44s (23 casi M19.1 + 9 regressioni history)

.\.venv\Scripts\python.exe -m pytest tests/test_external_edits_m151.py -k '(native_edit_save_history_preservation and ink) or incremental_inventory_sidebar_and_detail or external_viewer_gestures_emit_one_command' --basetemp tmp/pytest-m191-final-native -q
# 11 passed, 83 deselected in 24.30s

git diff --check
# exit 0; soli avvisi Git LF/CRLF, nessun errore whitespace

git diff --exit-code -- src/pdf_annotator/worker/save_service.py src/pdf_annotator/app/save_coordinator.py src/pdf_annotator/worker/external_profiles.py src/pdf_annotator/worker/external_delete.py src/pdf_annotator/worker/editorial_adapter.py pyproject.toml
# exit 0; writer, policy, transazioni Save e dipendenze invariati
```

**43 test finali PASS.** La verifica grafica offscreen sul campione M19 è PASS:
tre righe, motivazione nel pannello dei dettagli, nessuna icona di risposta,
dirty state e byte del campione invariati. Screenshot locale:
`tmp/m191-sidebar.png`.

È stato eseguito anche l'insieme esteso mirato:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_annotation_replies_m191.py tests/test_annotation_inventory_m12.py tests/test_inventory_detail_rebuild.py tests/test_sticky_note_reader.py tests/test_external_edits_m151.py --basetemp tmp/pytest-m191-qualified -q
# 197 passed, 2 failed in 362.20s, prima dell'ultima correzione DTO
```

I due fallimenti erano i nuovi casi M19.1 di edit/undo: il serializer dei delta
perdeva `/NM` e i campi aggiunti. Sono stati corretti senza indebolire le
asserzioni; entrambi passano nell'esecuzione finale di 32 test sopra riportata,
che include anche il round trip di summary e relazioni attraverso i delta.
L'insieme esteso non è stato ripetuto integralmente dopo questa correzione;
sono stati rieseguiti i test M19.1, history e gli 11 casi M15.1 direttamente
coinvolti. Le altre 176 regressioni dell'insieme esteso risultavano PASS.

Durante lo sviluppo sono stati corretti anche un refresh eccessivo dopo Move
e il retry delle identità obsolete. I primi tentativi di Save su Temp sandbox
hanno fallito per permessi/percorso: le verifiche successive usano `--basetemp`
nel workspace. Nessuna modifica alla pipeline Save per aggirare quel vincolo.

Copertura mirata: Replace/Delete/Insert sui due PDF M19, radice composta,
risposte multiple, RT predefinito, autori esterni, catene e riferimenti al membro,
Sticky Note ordinaria, errori di relazione, Unicode e motivazioni lunghe,
copia/read-only, assenza di icona/hit target, cambio rapido di selezione,
risposte tardive e fallite, progresso dei chunk, dirty state, mutazioni,
undo/redo, aggiornamenti incrementali, Save/Save As e riapertura.

La full suite è **NOT RUN per richiesta esplicita**. Nessun packaging o test
Adobe è stato eseguito. Non sono stati modificati gli stati dei gate Adobe.

## Verifica manuale in fuPDF

1. Dalla radice del repository avviare la versione di sviluppo, senza usare
   l'EXE stabile:

   ```powershell
   .\.venv\Scripts\python.exe -m pdf_annotator output/pdf/m19/m19_motivated_corrections.pdf
   ```

2. Mostrare la sidebar `Annotazioni` dal menu Visualizza, se è chiusa.
   Sul campione devono esserci tre voci: Sostituisci, Elimina e Inserisci testo.
   Nessuna voce Nota separata per le motivazioni e nessuna loro icona sulla pagina.
3. Selezionare Replace nella lista e poi con un click sulla correzione nella
   pagina. Verificare payload `è`, separato dalla risposta di `Revisione IA`.
   La motivazione deve essere quella della concordanza del soggetto singolare.
4. Ripetere per Delete (secondo `molto `) e Insert (`è ` prima di `chiara`).
   Selezionare e copiare il testo dei dettagli; provare a digitare: deve rimanere
   in sola lettura. La sola consultazione non deve rendere dirty il documento.
5. Cambiare rapidamente correzione e documento durante il caricamento: i testi
   non devono migrare da una selezione all'altra. Per risposte multiple/lunghe
   usare una copia della fixture di regressione o un PDF esterno equivalente;
   verificare numerazione, autori, testo completo e scrolling.
6. Su un PDF con Sticky Notes ordinarie verificare il popup flottante invariato.
   Su una copia di prova con padre mancante verificare che la risposta resti
   una Nota accessibile, con avviso nei dettagli.
7. Su una copia, aggiungere una normale annotazione, eseguire undo/redo, poi
   Save As su un nuovo percorso; chiudere e riaprire. Ripetere i controlli dei
   tre payload e delle motivazioni. Non sovrascrivere gli esperimenti M19.
8. Verificare che Delete su una correzione con risposte non attivi una
   cancellazione coordinata: le regole restano quelle precedenti.

## Acceptance status / limiti

| Copertura | Esito |
| --- | --- |
| M19.1: i 15 gruppi di regressioni richiesti | PASS nei 23 casi automatizzati dedicati |
| AT-SIDE-02/03/04, parti pertinenti a inventory, lettura, selezione e aggiornamenti | PASS mirato |
| AT-ANN-12 e AT-SAVE-01/02/04, preservazione delle risposte su Save/Save As/reopen | PASS dei sottocasi esercitati, non riesecuzione completa dei gate |
| AT-EDIT-04, correzioni composte e storia con risposte | PASS dei sottocasi di lettura/aggiornamento; cancellazione coordinata esclusa |
| Full suite, packaging, nuovi collaudi Adobe | NOT RUN per perimetro |
| Collaudo manuale utente M19.1 in fuPDF | NOT RUN, da eseguire prima del consolidamento |

La verifica manuale utente in fuPDF resta **NOT RUN** e precede il consolidamento.
La QA grafica offscreen sul PDF M19 verifica tre righe e la separazione delle
sezioni nel tema graphite; non sostituisce quel collaudo manuale.

Si riconoscono solo risposte `/Text` con relazione supportata. Le catene vengono
raccolte sotto la radice, senza UI ad albero o editing. Le relazioni anomale sono
consultabili ma non riparabili dall'app. Creazione di motivazioni, cancellazione
coordinata, AI e reader/editor di conversazioni più generali restano fuori scope.
