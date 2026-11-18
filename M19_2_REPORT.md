# M19.2 — Creazione atomica di correzioni motivate

10 novembre 2026. Implementazione sul working tree M19.1, senza commit.

## Implementato e perimetro

I tre comandi editoriali esistenti accettano `metadata=EditorialMetadata(reason=..., author=...)`.
Il campo è aggiunto dopo gli argomenti preesistenti; in assenza di metadati il
codec mantiene anche il precedente payload. `reason=None` non crea risposte o
marker; l'autore può essere specificato anche senza motivazione. Il default
rimane `PDF Annotator`. Non vengono normalizzati testo editoriale o motivazione.

Un solo invio ad AnnotationService crea la correzione, la risposta e un elemento
di history. Restano obbligatori contesto documento/generazione e target confermato
dal worker. Non è stato implementato alcun localizzatore, motore IA, endpoint di
automazione o controllo UI per inserire motivazioni.

Profili nativi riutilizzati:

| Comando | Radice | Componenti aggiuntivi |
| --- | --- | --- |
| Delete | StrikeOut, Contents della radice assente | Popup, Text con motivo e proprio Popup |
| Insert | Caret, Contents con testo da inserire | Popup, Text con motivo e proprio Popup |
| Replace | Caret con IT Replace, Contents con sostituzione | StrikeOut con IT StrikeOutTextEdit, IRT verso Caret e RT Group; Text con motivo; Popup dei tre componenti |

La risposta Text usa Contents distinto, IRT verso la radice e RT R. L'autore è
impostato durante la costruzione, prima della geometria definitiva del Caret.
Nessun update tardivo della radice viene usato per collegare la risposta.

## Provenienza persistente

La sola radice riceve la chiave privata `/fuPDFCorrection`, una stringa PDF
contenente JSON compatto e limitato. Esempio dello schema v1 per Replace:

```json
{"v":1,"group":"<uuid-v4>","kind":"replace_text","root":"<uuid-v4>","auxiliaries":["<uuid-v4>:strike"],"replies":["<uuid-v4>:reason"]}
```

I valori identificano gli NM già utilizzati dal writer; il marker non sostituisce
NM, IRT, RT o Contents. Nessun campo PDF standard viene riutilizzato per il
manifest. Delete e Insert hanno `auxiliaries: []`. Il reader accetta anche più
risposte elencate esplicitamente, con nomi univoci `:reason:<uuid-v4>`; questa
milestone crea una sola risposta e non espone un comando per aggiungerne altre.

Limiti: motivo 16.384 caratteri, autore 256, manifest 8.192, massimo 32 risposte
gestite. Motivo e autore presenti devono essere non vuoti, Unicode valido e senza
NUL. Il limite di creazione non restringe la lettura M19.1 dei commenti esterni.

La ricostruzione risolve gli NM nel documento, anche dopo rinumerazione degli
xref, e verifica versione/schema senza chiavi duplicate, unicità, pagina,
sottotipi e ruoli, tipi PDF dei riferimenti, geometrie finite, appearance stream,
flag consentiti e coppie Popup/Parent reciproche. Verifica inoltre tutti gli
archi entranti IRT/Popup/Parent, comprese risposte annidate o su altre pagine.
Componenti e chiavi sconosciuti, flag protettivi, marker mancante/alterato o
versione ignota impediscono la cancellazione coordinata. Le annotazioni esterne
non vengono marcate retroattivamente.

Il marker è una dichiarazione strutturale, **non una firma né una prova
crittografica dell'origine**. Un editor esterno può copiarlo, modificarlo o
rimuoverlo. La compatibilità del nuovo marker con Acrobat richiede il collaudo
manuale seguente; non viene dedotta dalla riapertura con PyMuPDF.

## Transazione, history e rollback

Il dispatcher estende il confine della transazione di pagina già esistente:

1. Salva stato owned, revisione, stack/cursore/stati/contabilità della history,
   dizionari di pagina e Annots, componenti preesistenti e soglia nuovi xref.
2. Esegue il writer editoriale comune, crea Text/Popup, relazioni e manifest,
   quindi verifica il gruppo.
3. Prepara snapshot espliciti di radice, ausiliari, risposte e Popup, registra
   un elemento di history e costruisce il delta.
4. Prepara un inventory candidato senza pubblicarlo e serializza l'esatto frame
   IPC di successo, verificandone anche il limite prima di uscire dal guard.
5. Solo dopo il successo pubblica l'inventory e invia il frame già preparato.

Un errore ripristina dizionari/componenti, annulla gli oggetti appena allocati,
owned e stato del dispatcher; scarta inventory candidato e frame. Gli slot xref
annullati possono restare null/free come contabilità interna: non restano oggetti
vivi o annotazioni parziali. I test verificano anche il documento salvato dopo il
fallimento, contenuto originale, revision, dirty e ramo redo preesistente.
Gli ID dei comandi falliti restano deduplicati: per un nuovo tentativo serve un
nuovo command_id.

La transazione esterna non trattiene un Page wrapper durante l'esecuzione:
PyMuPDF 1.28.2 richiede questa attenzione per il reload interno. Se il rollback
stesso fallisce, il documento viene chiuso e il worker ritirato; la sessione
diventa Failed e gli altri documenti restano indipendenti.

Undo/redo ripristina gli snapshot originali, senza rigenerare testo, nomi o date.
Il salvataggio corrente conserva i locator; Save durante uno stato undone e
successivo Redo è coperto. Dopo riapertura la history precedente non viene
persistita: il gruppo è riconosciuto dal marker e la sua nuova cancellazione ha
un singolo Undo/Redo nella nuova sessione.

L'atomicità copre le eccezioni applicative fino alla preparazione del risultato.
Non è un protocollo durevole contro crash/terminazione del processo o perdita
del trasporto dopo il commit. In quel caso la sessione segue il normale percorso
di worker failure; il sorgente su disco resta invariato finché non viene salvato.

## Cancellazione, inventory e reader

DeleteAnnotation in sessione e DeleteExternalAnnotation dopo riapertura passano
entrambi dalla verifica del gruppo marcato prima di utilizzare lo snapshot e la
cancellazione nativa già esistenti. Si elimina esclusivamente l'insieme
esplicitamente verificato, mai una cascata generica di tutti gli IRT. Lo stesso
controllo precede Undo/Redo che rimuovono un gruppo vivo.

La policy ordinaria M15.1 e i profili esterni non sono ampliati: è aggiunto un
ramo specifico per la sola struttura fuPDF verificata. Una risposta esterna
successiva, anche annidata, blocca l'intero gruppo e viene preservata. Il reader
continua a mostrare strutture non gestibili in sola lettura.

I delta trasportano radice e risposte con ID nativi. Un unico raccordo nel
DocumentManager associa la radice nativa all'UUID applicativo e rimappa il
parent della risposta solo nella presentazione; gli IRT reali restano intatti.
La sidebar mostra una sola correzione con motivazione, autore e testo completo
copiabili, senza una riga Sticky Note indipendente. I delta completi evitano un
refresh globale dell'inventory. La verifica di sicurezza legge comunque i
metadati delle annotazioni nell'intero documento per rilevare archi esterni;
non estrae testo né rasterizza pagine.

## File e responsabilità

Nuovi file di prodotto:

- `src/pdf_annotator/domain/editorial_metadata.py`: DTO e validazione.
- `src/pdf_annotator/worker/managed_corrections.py`: marker, risposta nativa,
  ricostruzione e autorizzazione strutturale del gruppo.

File di prodotto estesi:

- `domain/annotation_commands.py`, `annotation_models.py`, `serialization.py`:
  metadati opzionali e codec retrocompatibili.
- `worker/editorial_adapter.py`: autore, risposta, backing completo, riuso e
  rafforzamento della transazione di pagina, cancellazione motivata verificata.
- `worker/annotation_adapter.py`: guard esterno, gruppi nei delta/snapshot/history.
- `worker/annotation_dispatch.py`: checkpoint della history e finalizzazione
  entro il guard transazionale.
- `worker/annotation_inventory.py`: capability del gruppo verificato, lettura
  circoscritta del gruppo e staging dei delta.
- `worker/external_delete.py`: ramo marker verificato prima della policy ordinaria.
- `worker/document_service.py`: inventory candidato e frame IPC preparati prima
  del commit; ritiro del worker in caso di rollback fallito.
- `app/document_manager.py`: ponte degli ID nei delta e nelle letture inventory.

Supporto nuovo:

- `tests/test_motivated_corrections_m192.py`: regressioni e fault injection.
- `tests/m192_helpers.py`: fixture chiuse, ispezione separata e worker con fault.
- `tests/m192_probe.py`: campione generato con comandi e Save As di produzione.
- `output/pdf/m192/`: PDF e manifest/ispezione del campione.
- `M19_2_REPORT.md`: questo report.

Il diff complessivo comprende ancora le modifiche M19.1 già presenti. Non sono
state eliminate o consolidate; in particolare sidebar, session, viewer,
annotation_replies e relativi test M19.1 restano nel working tree. I file viewer,
le dipendenze, il codice Save/Save As dedicato, gli artefatti/report M19 e M19.1,
l'analisi M19.2 e l'EXE stabile sono verificati separatamente per integrità.

## Verifiche

Comandi eseguiti dalla radice del repository con `.venv\Scripts\python.exe`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_motivated_corrections_m192.py tests/test_editorial_persistence_m10.py tests/test_annotation_service.py tests/test_undo_redo_m14.py tests/test_annotation_inventory_m12.py tests/test_inventory_detail_rebuild.py tests/test_annotation_replies_m191.py tests/test_sticky_note_reader.py tests/test_save_m15.py --basetemp tmp/pytest-m192-regression2 -q
```

**226 PASS, 1 errore di ambiente, 275,95 s.** L'unico fallimento è
`test_save_as_collision_cancel_overwrite_and_external_conflict`: `os.link` fra
due fixture temporanee viene negato dal sandbox (WinError 5), prima delle
asserzioni relative all'hard link. Nessun test è stato indebolito o saltato.
I **57 test M19.2** sono tutti PASS in questa esecuzione.

Ripetizione del solo caso con permessi fuori sandbox:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_save_m15.py::test_save_as_collision_cancel_overwrite_and_external_conflict --basetemp tmp/pytest-m192-hardlink-confirm -q
```

**1 PASS, 2,10 s.** La selezione principale risulta quindi verificata per tutti
i suoi 227 casi, con il limite ambientale e la ripetizione indicati sopra.

Regressione specifica della policy e dei gesti M15.1:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_external_edits_m151.py -k '(native_edit_save_history_preservation and ink) or incremental_inventory_sidebar_and_detail or external_viewer_gestures_emit_one_command' --basetemp tmp/pytest-m192-native -q
```

**11 PASS, 83 deselected, 21,87 s.** Totale dei casi distinti verificati:
**238**, inclusi i 57 nuovi M19.2. Le regressioni comprendono comandi legacy,
Unicode/autore predefinito, profili nativi, history ripetuta e Save mentre undone,
delete/undo/redo, gruppi rinumerati dopo riapertura e gruppi con più risposte.

Fault injection: primo componente, creazione risposta, IRT, RT, manifest,
preparazione snapshot/history, history_after, registrazione dopo push,
inventory candidato, serializzazione e validazione finale; inoltre Undo, Redo,
cancellazione e rollback stesso. Verificati rifiuti conservativi per risposta
esterna diretta/annidata/altra pagina, marker assente/versione ignota, NM
duplicati, componente ignoto, lock, Parent errato, IRT con tipo stringa e Popup
mancante. Preservato anche un ramo redo con annotazioni esterne preesistenti.

Ulteriori controlli:

- `git -c core.safecrlf=false diff --check`: PASS, nessun errore di whitespace.
- SHA-256 di 32 file protetti: tutti invariati rispetto all'inizio di M19.2
  (viewer, artefatti M19, report M19/M19.1, analisi M19.2, dipendenze,
  save_coordinator/save_service/save_validation, profili M15.1 ed EXE stabile).
- `python -m tests.m192_probe output/pdf/m192`: PASS, 3 comandi, 3 revisioni,
  3 gruppi, 14 annotazioni fisiche contando i Popup, produzione Save As e
  contenuto base/source hash invariati.
- `pdftoppm -png -scale-to 1000 ...`: tre PNG ispezionati, geometrie e testo
  leggibili. Poppler segnala l'assenza locale dei font di display Symbol e
  ArialUnicode, senza difetti visibili nelle tre pagine; non è un gate Adobe.
- `python tmp/m192_visual_check.py`: riapertura del campione nella vera MainWindow,
  tre righe e tre gruppi cancellabili, tre motivi in sola lettura; screenshot di
  tutti e tre i casi ispezionati. Nessun cambiamento del PDF o dirty state.

La full suite non viene eseguita, come richiesto. Nessun packaging, commit, push
o tag; nessun aggiornamento automatico dei gate Adobe.

### Stato di accettazione della porzione M19.2

| Requisiti/gate | Esito di questa consegna |
| --- | --- |
| A-06, AT-CMD-01, ANN-08..11 | PASS per i nuovi metadati e il percorso comune dei tre comandi |
| AT-EDIT-01..04 | PASS automatico per persistenza, target, contenuto invariato e ciclo logico motivato |
| AT-SIDE-02..04, AT-ANN-12 | PASS per raggruppamento, dettaglio copiabile e preservazione coperti dai test mirati |
| AT-SAVE-01..05 | Regressioni automatiche pertinenti PASS, incluso retry hard link; pipeline invariata |
| AT-ACROBAT-06..09 sul nuovo campione M19.2 | NOT RUN: serve il nuovo collaudo reale, anche per il marker persistente |
| AT-INDESIGN-01..04 | NOT RUN: nessuna nuova evidenza né modifica dei gate preesistenti |

I PASS sono limitati alle porzioni indicate, non una certificazione generale
della milestone o dell'interoperabilità Adobe.

## Campione consegnato

PDF: `output/pdf/m192/m192_motivated_corrections.pdf`

SHA-256: `bd8b1cb1feb72cc983f8c7fcd6ec18ca2797440d28785676e542865a0914cbe3`.
Tre pagine sintetiche: `sono` → `è`, eliminazione della seconda occorrenza di
`molto `, inserimento di `è ` prima di `chiara`. Tutti i motivi hanno autore
`Revisione IA`; nessun motore IA è stato eseguito. `manifest.json` registra i
payload applicativi, le revisioni e gli hash; `inspection.json` registra
dizionari, gruppi e stato base dopo il salvataggio. Le nuove annotazioni sono
scritte dal worker applicativo, non dal vecchio writer sperimentale M19.

## Collaudo manuale da eseguire

1. Avviare fuPDF dal codice di questo working tree; aprire il PDF M19.2 indicato
   sotto. Verificare tre righe, i tre tipi corretti, autore `Revisione IA`, payload
   editoriale e motivo distinti e copiabili, nessuna Sticky Note separata.
2. Su una copia eliminare una correzione: deve sparire anche il motivo. Un Undo
   ripristina tutto, un Redo rimuove tutto. Ripetere e provare Save As, chiusura e
   riapertura; il gruppo deve tornare riconoscibile e cancellabile.
3. In Acrobat Desktop aprire una copia fresca, registrando versione e screenshot:
   verificare Replace, Delete e Insert nativi, payload, autore, motivo come
   risposta al commento giusto e nessuna alterazione del testo pagina. Per Replace
   la risposta deve appartenere alla radice Caret, senza rompere lo StrikeOut Group.
4. Salvare da Acrobat senza aggiunte e riaprire in fuPDF: verificare reader e
   persistenza del gruppo. Se Acrobat ha riscritto campi/strutture sconosciuti o
   rimosso il marker, attendersi cancellazione bloccata e annotazioni preservate;
   registrare l'esito, senza "riparare" automaticamente la provenienza.
5. Su un'altra copia aggiungere in Acrobat una risposta esterna a una correzione,
   salvare e riaprire in fuPDF: la nuova risposta deve essere leggibile e la
   cancellazione coordinata deve essere bloccata senza perdere commenti.
6. Annotare PASS/FAIL con file ed evidenze. Questa consegna non attribuisce nuovi
   PASS ad Acrobat o InDesign; l'importazione/Accept InDesign resta un gate separato
   e richiede la coppia sorgente/export prescritta, non questo PDF sintetico.

Riferimenti API ufficiali consultati: [Annot.set_irt_xref](https://pymupdf.readthedocs.io/en/latest/annot.html#Annot.set_irt_xref),
[Document.xref_set_key](https://pymupdf.readthedocs.io/en/latest/document.html#Document.xref_set_key).
