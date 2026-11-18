# M15.1 — External annotation editing + Acrobat round-trip

## Final release status — PASS

The final full suite completed with **468 passed, 1 skipped, 0 failed** in
1487.58s. The only skip is intentional:
`tests/test_custom_callout_ap_spike.py`, the historical full-custom Callout AP
architecture rejected after failing the Acrobat interoperability gate and
superseded by the native-parent child-Form design.

- Manual production gate: **PASS**.
- Save / Save As: **PASS**.
- Undo / Redo: **PASS**.
- External FreeText plain/rich editing: **PASS**.
- External Callout rich editing, including anchor: **PASS**.
- Source Sans 3 / Courier Prime canonical physical fonts: **PASS**.
- app → Acrobat → edit/save → reopen in app round-trip: **PASS**.
- Callout attachment side / OpenArrow: **PASS**.
- No global red flash: **PASS**.
- `VERIFIED_EXTERNAL_EDITS` enables only the approved production profiles:
  `freetext-plain-v1`, `freetext-rich-v1`, and `callout-rich-v1`, for their
  approved operations only.
- InDesign: **NOT RUN**.
- Historical full-custom Callout spike: **intentionally skipped / rejected**.
- Child-Form spike: **PASS**. MuPDF rendering is mandatory; the Poppler check
  is availability-aware and does not fail when Poppler is unavailable.

The historical interim notes below are retained as investigation evidence;
their earlier pending/blocking gate statuses are superseded by this final
release status.

## Policy definitiva: normalizzazione al primo re-layout

1. **Policy.** Apertura, selezione, view, move FreeText, move box/anchor
   Callout e delete mantengono l'appearance importata. Resize box, text confirm
   e style/font edit adottano una volta la representation nativa.
2. **Font.** Le famiglie perdono l'eventuale subset prefix e le varianti
   Bold/Italic restano attributi; la famiglia col maggior numero di caratteri
   non-whitespace (pareggio: primo run visibile) diventa Helvetica, le altre
   Courier. Run boundaries, size, color, bold e italic sono conservati.
3. **AP/reflow.** Il primo layout edit emette `/Contents`, `/RC`, `/DA`, `/DS`,
   stile e una nuova `/AP`; non scala la vecchia appearance importata.
4. **Editor.** L'apertura costruisce solo una preview Helvetica/Courier e
   nasconde il solo AP dell'annotazione corrente. Escape ripristina il paint
   importato; durante Ctrl+Invio il viewer mostra la preview normalizzata, non
   un frame della vecchia AP.
5. **Callout.** Normalizzazione tipografica non cambia `/CL` o `/RD`; resize
   cambia la geometria richiesta e conserva leader/anchor/knee, senza freccia
   duplicata.
6. **History.** Undo della prima conversione ripunta esattamente il root/AP
   importato; Redo ripristina lo stato normalizzato sullo stesso xref.
7. **Save/reopen.** Una FreeText plain normalizzata acquisisce `/RC` e rientra
   nel profilo rich pieno; l'identità `/NM` e i metadata non coinvolti restano.
8. **Test.** Nuove fixture coprono una famiglia, più famiglie, mapping 3→2,
   attributi/run, move conservativo e Undo dell'AP. Gli inspect PyMuPDF restano
   nel solo subprocess `tests.m151_helpers`.
9. **Gate manuale.** Ripetere in Acrobat view/move, resize reflow, Escape,
   Ctrl+Invio, Undo/Redo e Callout leader; il milestone resta aperto.

## Fidelity UI finale (M15.1 aperto)

Il launcher storico mostrava `Arial`/`Courier New`, ma usava una tabella locale
obsoleta: non era il resolver dell'editor. Il launcher ora chiama lo stesso
resolver production e registra i font privati; l'editor usa **Source Sans 3**
per il ruolo semantico Helvetica e **Courier Prime** per Courier. Il PDF usa le
stesse otto facce embedded nella child Form; `/RC`, `/DA` e `/DS` mantengono i
ruoli semantici, non un alias di font Windows.

Il bordo spariva perché l'AP FreeText viene generata da `Annot.update()` e non
dal solo `/DA`; il percorso normalizzato ora passa `border_color`, `fill_color`,
width e opacity a quell'update prima di restaurare il root allowlisted. Non usa
`set_colors`, API non valida per FreeText. Le preview di gesture continuano a
riusare l'inventory/AP corrente e non costruiscono uno style di default.

## Spike isolata: FreeText multi-font embedded

Codice solo sperimentale: `pdf_annotator.spikes.embedded_multifont_freetext`.
Usa `Page.insert_font(fontfile=...)` come factory pubblica: Source Sans 3 3.052
e Courier Prime statici completi sono inseriti come Type0 / Identity-H con
ToUnicode e FontDescriptor / FontFile raggiungibili. Una piccola `/AP` Form
isolata collega le due risorse e codifica i run usando la tabella Unicode→CID
pubblica di `Document.get_char_widths`; non esiste alcuna importazione dal
runtime di prodotto.

L'artefatto contiene due `/Subtype /FreeText`, incluso `FSource → FCourier →
FSource` per il testo multi-run, `/Contents`, `/RC`, `/DA`, `/DS` e `/AP`.
`inspection.json` registra xref, BaseFont, subtype,
ToUnicode, FontFile e l'uso effettivo delle risorse AP. Il test subprocess
salva, riapre e verifica struttura/font/text extraction. Il solo limite aperto
è l'editing effettuato da Acrobat senza i font installati: è da osservare nel
gate, non è stato assunto automaticamente.

Implementazione e verifiche automatiche completate: **171 test mirati passati**,
più verifica conclusiva descritta sotto. Il successivo gate manuale Acrobat è
**PASS** per i profili e le operazioni elencati nello stato finale iniziale.
Safe-delete M12 conserva le proprie regole indipendenti.

## Spike isolata: stato semantico del box Acrobat

Ground truth: le FreeText originarie di Acrobat nel campione locale storico hanno `/C` con
il giallo di riempimento, `/DA` e `/DS` con il rosso di bordo/default, e il
nero dei caratteri nei run `/RC`. Il loro `/BS` descrive stile e spessore;
`/CA` descrive l'opacità. Non è stata osservata una chiave `/IC` alternativa
per il background/interior. La prima variante della spike lasciava `/DA` e
`/DS` neri e dipingeva solo il bordo nella `/AP`: l'aspetto iniziale non
conteneva il fill e Acrobat non riceveva lo stato editoriale corretto per
rigenerare il box.

La sola spike `embedded_multifont_freetext` ora serializza `/C [1 .95 .3]`,
`/DA .9 .1 .1 rg /FSource 14 Tf`, `/DS ... color:#E61A1A`,
`/BS <</W 2 /S /S>>` e `/CA 1`; la `/AP` isolata disegna lo stesso fill giallo
e bordo rosso da 2 pt. I run `/RC` restano neri e l'embedding Type0 /
Identity-H, i font Source Sans 3/Courier Prime e la codifica multi-run non
sono stati modificati. L'artefatto finale è
`output/m15_1/font_spike/M15.1-embedded-fonts-style-spike.pdf` e contiene
soltanto una FreeText Source Sans 3 e una `FSource → FCourier → FSource`.

Verifica strutturale isolata:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_embedded_multifont_freetext_spike.py -q
```

Esito: **1 passed**. Il test usa la spike in subprocess, riapre il PDF e
verifica `/C`, `/DA`, `/DS`, `/BS`, `/CA`, assenza di `/IC`, `/RC`, Type0,
ToUnicode, FontFile e le risorse AP. Il gate ancora necessario in Acrobat è:
double-click senza edit, move, resize, Save e reopen su entrambe le FreeText;
in ogni passaggio testo nero, fill giallo e bordo rosso da 2 pt devono restare
invariati. Nessun codice di produzione, modello capability o
`VERIFIED_EXTERNAL_EDITS` è stato modificato.

## Spike isolata: fix del primo paint multi-run

Il mancato paint iniziale non era un problema Type0, ToUnicode, `/RC` o CID:
la stream selezionava già `FSource`, `FCourier`, `FSource` e ogni stringa era
codificata contro il rispettivo xref tramite `Document.get_char_widths`. La
causa era il graphics state della `/AP`: `re B` dipingeva il fill giallo e
lasciava attivo il non-stroking color giallo per i successivi `Tj`. Il testo
nero semantico in `/RC` non modifica automaticamente il colore della content
stream, perciò il testo veniva dipinto giallo sul giallo.

La sola spike ora emette `0 0 0 rg` tra `re B` e `BT`; non modifica `/RC`,
`/DA`, `/DS`, `/C`, `/BS`, font embedding o risorse. L'inspection registra i
triples `Tf`/stringa CID, verifica che coincidano con il mapping del font
selezionato e verifica il reset nero prima del text object. La v2 viene salvata
e riaperta nel subprocess di test. Il render Poppler è un sanity check visivo,
non una prova Acrobat.

Artefatto nuovo: `output/m15_1/font_spike/M15.1-embedded-fonts-style-spike-v2.pdf`.
Test isolato `tests/test_embedded_multifont_freetext_spike.py -q`: **1 passed**.
Stato: **READY FOR ACROBAT GATE**. Ripetere il gate iniziale senza click,
double-click, move, resize, edit e Save/reopen; verificare in tutti i passaggi
testo nero, fill giallo, bordo rosso e le due famiglie nei run. Produzione e
`VERIFIED_EXTERNAL_EDITS` restano invariati.

## Revisione strutturale post-gate (M15.1 resta aperto)

Il gate ha dimostrato che la precedente classificazione conservativa applicava
vincoli di creazione anche a baseline PDF native valide. La validazione è ora
operation-scoped: move, anchor, resize, style e text non richiedono più il fit
del testo e un overflow importato resta contenuto, visibile e riapribile.
`PdfPoint`/`PdfRect` rappresentano coordinate PDF finite anche negative;
soltanto i comandi di creazione mantengono il controllo CropBox. L'inventory non
clampa più `/Rect` o `/CL`, e l'adapter esterno non clampa move/anchor/resize.

FreeText e Callout con CSS/DA/DS/AP non completamente mappati ricevono un
baseline editoriale conservativo e capability candidate complete; le chiavi e
le risorse che una singola operation non tocca restano native. Il detail usa lo
stesso baseline e non può più fallire perché il profilo non aveva un editor
style. Il gate di rilascio resta invariato: `VERIFIED_EXTERNAL_EDITS` è vuoto.

## Revisione AP/font post-gate (M15.1 resta aperto)

### Causa e fix della normalizzazione

Il problema non era di import: `/RC`, `/DA`, `/DS` e l'`/AP` iniziali venivano
letti correttamente, e gli snapshot nativi di History li ripristinavano. Era
la mutation AFTER: resize/style/text passavano da `Annot.update()`, che per una
FreeText creata da un altro producer rigenera una nuova appearance usando il
fallback Base-14/Charis di MuPDF. Un font Acrobat, embedded o subset non
presente nella piccola toolbar veniva quindi sostituito pur senza una scelta
font dell'utente.

Resize, move, anchor e modifica della geometria ora non chiamano
`Annot.update()`: cambiano soltanto la geometria nativa e riusano il form `/AP`
esistente. Per text/style senza cambio esplicito di famiglia viene copiato il
solo form `/AP` dell'annotazione (mai mutato in-place), mantenendo le stesse
risorse font; il testo usa la `ToUnicode` reale del font importato e `/RC` viene
aggiornato nei soli nodi testuali, conservando CSS e font-family per-run.
`/DA` e `/DS` aggiornano soltanto i colori, lasciando invariato il token font.
Un cambio esplicito della famiglia resta l'unico percorso che può rigenerare
un'appearance con il font scelto dalla toolbar.

La toolbar continua a esporre soltanto i font v1 per la creazione. Il suo
valore di presentazione non è un mapping né un'autorizzazione a convertire un
font nativo. Le risorse embedded/subset esistenti restano referenziate dal
nuovo form `/AP`; i nuovi glifi vengono codificati esclusivamente attraverso
la `ToUnicode` dichiarata dal font, senza dedurre nomi PostScript o installare
font di sistema. Il limite da verificare al gate è il nuovo glifo assente dal
CMap del subset: il codice non deve mai normalizzare i run esistenti.

### Rendering editor e leader

Quando un editor esterno è aperto, il viewer non dipinge l'appearance testuale
nativa di quella singola annotazione: il `QTextEdit` è l'unica resa del testo.
Escape/cancel ripristina immediatamente il normale paint dell'`/AP`; durante il
commit resta visibile l'appearance confermata fino alla risposta worker. Non
avviene alcuna mutation PDF per nascondere l'overlay.

Per un Callout il viewer non aggiunge più l'intera `/CL` sopra un `/AP` che già
la contiene. La leader sintetica viene clippata alla sola porzione esterna alla
copertura raster dell'appearance: l'arrowhead e il tratto in-AP restano uno,
mentre il segmento che esce dall'AP resta visibile. `/CL`, knee e anchor PDF
non sono modificati da questa scelta di paint.

### Regressioni aggiunte

- FreeText importata con font non-toolbar: resize, Undo/Redo, text edit breve e
  style edit preservano font resources dell'`/AP`, CSS `/RC` e token `/DA`/`/DS`.
- Qt reale: editor aperto non dipinge il suo AP nativo sotto il `QTextEdit`;
  Escape e commit riportano una sola appearance.
- Callout: il paint passa alla leader il rettangolo realmente coperto dall'AP,
  quindi il disegno sintetico è limitato all'esterno e non duplica la freccia.

### Test post-fix

```powershell
.venv\Scripts\python.exe -m pytest tests/test_external_edits_m151.py -q
.venv\Scripts\python.exe -m pytest tests/test_freetext_m8.py tests/test_freetext_rich_m82.py tests/test_callout_m9.py -q
```

Esito: **75 passed** (M15.1) e **27 passed** (M8/M8.2/M9). Le ispezioni di
font/AP nei test sono eseguite soltanto dal subprocess `tests.m151_helpers`;
il processo GUI/pytest non importa PyMuPDF.

### Verifica manuale storica (completata)

Su Acrobat verificare una FreeText con Myriad Pro/Courier Std e una con font
embedded/subset: move, resize, Undo resize, Escape dall'editor, modifica breve
di un run esistente e cambio fill/border non devono cambiare i font esistenti.
Verificare un nuovo glifo non contenuto nel subset separatamente. Durante
l'editing deve vedersi un solo testo; su Callout deve vedersi una sola freccia,
anche quando una parte della `/CL` esce dal Rect. Il gate è stato completato;
il registro production contiene solo i profili/operazioni dichiarati nello
stato finale.

## 1. Capability model

Capability distinte: move, resize, text, style, anchor, delete. Inventory, service,
worker e viewer applicano la stessa autorizzazione. Il profilo controlla subtype,
IT, RC/DA/DS, RD/CL, AP/resources/font, flags, popup/reply e chiavi sconosciute.
L'operazione deve essere sia rappresentabile sia approvata nel registro per
`(profile, operation)`. Nessuna opzione prodotto consente di aggirare il gate.

## 2. Profili riconosciuti

| Profilo candidato | Operazioni implementate |
| --- | --- |
| freetext-plain-v1 | move, resize, text, style |
| freetext-rich-v1 | move, resize, text, style |
| freetext-translation-v1 | solo move |
| callout-rich-v1 | move box, resize box, anchor, text, style |
| ink-translation-v1 | solo move |
| unsupported | editing disabilitato; safe-delete solo se M12 lo permette |

Il campione locale storico contiene 64 annotazioni. Tra le 36 FreeText/Callout: **12 candidati
completi** (1 FreeText, 11 Callout), **18 geometry-only**, **6 read-only**.
I 9 Ink sono candidati al move. Le altre 19 annotazioni non acquisiscono nuove
capability. Il manifest JSON elenca profilo, motivo e capability candidate per
oggetto; le pagine del campo `classification` sono zero-based.

I geometry-only hanno font/CSS/default appearance o risorse non completamente
mappabili: la sola traslazione conserva AP e testo senza rigenerazione. I sei
FreeText/Callout unsupported sono p6 xref474/480/483, p10 xref502, p23 xref638/642:
geometria fuori dal dominio per x480; testo/stile/appearance non rigenerabili
senza perdita negli altri casi. La classificazione dipende dalla struttura,
non dal nome del file. In produzione anche i candidati restano read-only.

## 3. FreeText supportata e geometry-only

Si riusa il modello rich text M8/M15 con parser XHTML/CSS conservativo, font
mappati e corrispondenza esatta RC/Contents. L'editor Text Box esistente serve
anche gli esterni. Testo e modifica del box style possono essere un unico comando.
Il fit guard impedisce il commit che taglierebbe testo. Nei profili non mappabili
resize resta disabilitato: cambiare Rect scalando AP non prova conservazione
del layout. Move non normalizza RC, DA, DS, font o AP.

## 4. Callout

Si usano Rect/RD/CL nativi, incluso il knee obliquo. Move box conserva anchor
e trasla insieme knee/attach; resize conserva la loro relazione; anchor edit
modifica il primo punto e l'envelope necessario. IT e le chiavi non coinvolte
restano identici. UI con editor e handle esistenti, preview canonica e un solo
comando al rilascio. Nessun knee a 90 gradi imposto.

## 5. Ink

Move trasla Rect e tutti gli InkList. AP, strokes relativi, width, color,
opacity, metadata e popup/reply rimangono preservati. Un reply ordinario non
autorizza una cancellazione insicura del gruppo. Highlight resta non movable.

## 6. Preservation strategy

Mutation in-place sullo stesso xref, senza conversione ad app-owned. Allowlist:

| Operazione | Chiavi autorizzate |
| --- | --- |
| FreeText move | Rect, M |
| Ink move | Rect, InkList, M |
| FreeText resize | Rect, RD, AP, M |
| Callout geometry | Rect, RD, CL, AP, M |
| Text | Contents, RC, AP, M |
| Style | C, DA, DS, BS, CA, AP, M |

Text con style usa l'unione delle due allowlist. M rappresenta l'edit corrente;
CreationDate, NM, author, flags e relazioni non sono normalizzati. Quando serve
rigenerare AP, il vecchio AP viene staccato prima di `Annot.update()` e mantenuto
immutabile; dopo update si reinstalla il dizionario originale con le sole chiavi
ammesse. Rollback ripristina il root originale in caso di errore.

La verifica dell'artefatto finale conferma **60 annotazioni non toccate e 841
oggetti nativi/stream decodificati invariati**. Save As completo espande 28
contenitori ObjStm/XRef e può riordinare chiavi: la verifica confronta valori PDF,
non il layout testuale dei dizionari. Gli oggetti contenuti sono verificati
singolarmente. Nessun flattening o modifica al testo originale delle pagine.

## 7. Undo/Redo

Il comando A-06 passa da AnnotationService al worker dedicato. History conserva
snapshot dei root nativi prima/dopo e summary corrispondenti. Undo/Redo reinstalla
il root, inclusi riferimenti AP, relazioni e capability, senza ricostruzione.
Save/Save As usa il percorso e le revision barrier M15 esistenti.

## 8. Inventory incrementale

HistoryDelta aggiorna soltanto i summary/detail interessati; nessun full inventory
rescan dopo external edit, Undo o Redo. Le risposte detail/summary obsolete vengono
scartate per gli ID modificati durante la richiesta. Il detail necessario viene
richiesto di nuovo. Test con worker che fallisce al secondo scan e verifica
dell'identità delle righe sidebar non interessate. Preview pendente locale,
nessun aggiornamento globale per un gesto; cancel non invia mutation.

## 9. Test

Run finale mirato, senza full suite:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_external_edits_m151.py tests/test_freetext_m8.py tests/test_freetext_rich_m82.py tests/test_freetext_viewer_m8.py tests/test_callout_m9.py tests/test_annotation_inventory_m12.py tests/test_undo_redo_m14.py tests/test_save_m15.py tests/test_save_m15_ui.py tests/test_annotation_domain.py tests/test_annotation_service.py -q
```

Esito: **171 passed in 759.00s (0:12:38)**; log `tmp/m151-targeted-tests.log`.

M15.1 include 50 casi: 36 combinazioni edit/rotazione (0/90/180/270, CropBox non
nullo), save-reopen, semantic-diff, Undo/Redo, gate, unknown keys, overflow,
inventory/stale detail, gesti UI ed editor condiviso. Fixture e ispezioni PyMuPDF
eseguite esclusivamente in subprocess; GUI/pytest principale non importa PyMuPDF.
Il test aggiunto nella revisione finale verifica che il default nativo `/HeBo`
non perda il bold: `-k plain_native_bold -q` → **1 passed, 49 deselected**.
Verifica conclusiva di gesti/editor e bold dopo gli ultimi type hint:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_external_edits_m151.py -k 'viewer or existing_editor or plain_native_bold' -q
```

Esito: **9 passed, 41 deselected in 25.11s**.

```powershell
.venv\Scripts\python.exe -m tests.m151_fixtures output/m15_1/imported-example.pdf
.venv\Scripts\python.exe -m tests.m151_probe output/m15_1/imported-example.pdf output/m15_1/imported-example-reviewed.pdf
.venv\Scripts\python.exe -m tests.m151_verify_artifact output/m15_1/imported-example.pdf output/m15_1/imported-example-reviewed.pdf
git diff --check
```

Sul campione storico, probe, verifica semantica e `git diff --check`: **PASS**. Anche il comando
`.venv\Scripts\python.exe -m compileall -q src/pdf_annotator tests/m151_helpers.py tests/m151_probe.py tests/m151_verify_artifact.py tests/m151_gate_app.py tests/test_external_edits_m151.py`
è passato. La probe rifiuta percorsi di output già esistenti;
per rigenerare scegliere un nuovo nome. Verifica visuale dei quattro edit sul
render finale: PASS per geometria, leader, testo e assenza di clipping osservabile.
I render non costituiscono prova Acrobat.

Copertura richiesta: ANN-05/06/07/11, PDF-02/03/04; casi automatici pertinenti
AT-ANN-02/04/09/11/12, AT-SIDE-04, AT-CMD-01 e AT-SAVE-01..05: **PASS per le
porzioni automatiche pertinenti**, senza estendere l'esito alle prove Adobe.
Gate manuali esterni AT-ACROBAT-01/03/04/05: **NOT RUN/BLOCKED** in attesa
di esecuzione in Adobe. Nessuna nuova dichiarazione InDesign (NOT RUN, fuori
dal gate M15.1). M15 rimane chiuso come richiesto.

## 10. Artefatto e checklist Acrobat

Il collaudo storico ha applicato quattro edit: testo FreeText, resize Callout,
move Ink e move FreeText geometry-only. Il campione e i suoi report grezzi
restano materiali locali. Per le verifiche ripetibili e per un nuovo
collaudo Adobe, usare la [procedura delle fixture pubblicabili](tests/manual/public_pdf_fixtures.md).

## 11. Limiti e file cambiati

La prova manuale completa per ciascuna capability è ancora necessaria. Nessuna
capability è abilitata in produzione; non basta approvare soltanto i quattro edit
dell'artefatto per abilitare tutte le operazioni. Profili font/CSS/AP, flags e
relazioni non rappresentabili restano limitati o read-only. CJK/emoji conservano
il limite M15; nessuna nuova infrastruttura font o dipendenza.

- `domain/external_edits.py`, commands/kinds/serialization/validation/inventory:
  comando esterno, capability e detail mappato.
- `worker/external_profiles.py`, `external_edit_adapter.py`: classifier e mutation
  native con allowlist; adapter/dispatch/inventory/document_service: history e delta.
- `services/annotation_service.py`: autorizzazione per operazione.
- `app/document_manager.py`, `document_session.py`, `main_window.py`: detail asincrono
  e rigetto dei risultati obsoleti.
- `viewer/external_interaction.py`, `pdf_view.py`: gesti, preview ed editor condiviso.
- `tests/test_external_edits_m151.py`, `m151_helpers.py`, `m151_probe.py`,
  `m151_verify_artifact.py`, `m151_gate_app.py`: test isolati e strumenti del gate.
- Questo report e `output/m15_1`: evidenza per la revisione.

Percorsi sorgenti relativi a `src/pdf_annotator`. Working tree inizialmente pulito.
Nessun commit/push, full suite, nuova feature fuori M15.1 o nuova dipendenza.
API verificate su documentazione ufficiale
[Annot](https://pymupdf.readthedocs.io/en/latest/annot.html) e
[Document](https://pymupdf.readthedocs.io/en/latest/document.html), runtime PyMuPDF 1.28.2.

## Production integration: native AP + isolated canonical text Form

La variante full-custom AP e la sostituzione integrale della Form restano
scartate: avrebbero ricostruito clipping, geometry e leader di Callout. Il
gate manuale Acrobat della spike `pymupdf_ap_child_text_xobject` resta
**BLOCKED** dopo il caso storico FreeText xref 516; nessuna capability production è
stata abilitata.

La pipeline production ora usa `worker/canonical_text_ap.py`. Dopo un
`Annot.update()` PyMuPDF genera il parent AP completo; il patcher accetta solo
la forma stretta 1.28.2 (un solo BT/ET generato subito dopo il clip nativo),
crea una child Form `/CanonicalText` e sostituisce soltanto quel BT/ET con
`q /CanonicalText Do Q`. Il parent conserva BBox, Matrix, clip, ExtGState,
fill, bordo, leader, knee, OpenArrow, `/CL` e `/RD`; al parent viene aggiunta
solo la resource `/XObject` necessaria. Non esiste un parser content-stream
generale: una shape diversa genera `CanonicalTextPatchError` e l'adapter
ripristina lo snapshot root nativo atomico.

La child ha Resources isolate, font Type0/Identity-H embedded dai file ufficiali
Source Sans 3 e Courier Prime, ToUnicode, FontFile e CID per run. Il layout
preserva run, dimensione, grassetto, corsivo, colore, newline e wrapping; il
clip parent mantiene l'overflow solo visuale senza perdere contenuto. Il ruolo
dominante e Source Sans 3, tutte le famiglie alternative diventano Courier
Prime; Times e un'alternativa esterna usano il secondo ruolo. La registrazione
temporanea via `Page.insert_font` viene annullata prima del ritorno: nessuna
resource del page originale viene cambiata.

Qt carica le otto facce dagli asset come application-private in
`app/canonical_fonts.py`, verificando `QFontInfo`. L'editor usa Source Sans 3 e
Courier Prime, non Arial/Courier New; un fallback fallisce in modo controllato.
Il PDF garantisce la visualizzazione tramite embedding. Un edit diretto in
Acrobat conserva quelle famiglie quando sono disponibili sul sistema; non sono
installate da M15.1 (packaging M17).

Normalizzazione: select/open, move, delete e Callout anchor move preservano AP
originale. Resize, text confirm e style edit creano una sola normalizzazione;
Undo reinstalla il root nativo completo e Redo quello canonical. Save/Save As
mantengono FreeText/FreeTextCallout e l'inventory/detail rilegge il modello
normalizzato senza tradurlo in unsupported. Il gate Acrobat ha registrato le
operazioni production autorizzate nello stato finale; gli altri profili restano
read-only.

Test aggiunti/verificati per questa integrazione:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_external_edits_m151.py -k 'strict_canonical' -q
# 2 passed
.venv\Scripts\python.exe -m pytest tests/test_external_edits_m151.py -k 'imported_freetext_normalizes or imported_font_roles or editor_font_mapping' -q
# 3 passed
.venv\Scripts\python.exe -m pytest tests/test_external_edits_m151.py -q
# 83 passed in 588.03s
.venv\Scripts\python.exe -m pytest tests/test_freetext_m8.py tests/test_freetext_rich_m82.py tests/test_callout_m9.py -q
# 27 passed
git diff --check
# PASS
```

Le nuove regression coprono guard strict/no patch euristica, rollback della
normalizzazione, child `/CanonicalText`, Resources isolate, Type0/ToUnicode/
FontFile, Source/Courier (inclusi bold/italic), CID multi-run, move senza
normalizzazione, undo/redo e i percorsi save/reopen esistenti. Rimangono
**NOT RUN/BLOCKED** i gate manuali Acrobat su output production (render,
double click, resize, Save/Save As, Callout off-page con freccia unica) e ogni
prova InDesign; nessun test automatico viene presentato come prova Adobe.

### Blocco manual gate: FreeText storico xref 516 (pagina UI 12 / indice 11)

Il comando UI non ha né serializza una pagina di destinazione: `move` e
`resize` portano soltanto `page_index: 11`. Service e worker autorizzano lo
stesso logical id `native:11:516` su quella pagina. La precedente etichetta UI
"Pagina di destinazione non valida" non descriveva pertanto una validazione di
destinazione del percorso live.

La traccia worker ha individuato due cause reali. Dopo il move, `Annot.update()`
produceva per il resize un parent PyMuPDF valido con un unico `BT…ET`: il primo
run era `Tf/Tm/TJ`, le righe wrappate successive erano `Td/TJ`. Il patcher
accettava solo la prima grammatica e sollevava `CanonicalTextPatchError`; nel
rollback, un `reload_page` sulla pagina già corrente poteva inoltre sollevare
un'asserzione del binding PyMuPDF, chiudendo il documento e trasformando il
fallimento in un errore protocollo. Il worker ora registra localmente il
traceback classificato, accetta esclusivamente anche questa seconda forma
PyMuPDF deterministica (non un parser generale) e, nel solo caso dell'asserzione
di refresh, usa un nuovo `load_page` per completare il rollback atomico.

La regressione originale eseguiva GUI → service → worker su una copia
del campione locale storico. La fixture sintetica copre move nella stessa pagina, resize, Save/reopen,
verifica della child `/CanonicalText` e dei font privati embedded, Undo con
snapshot AP nativo pre-normalizzazione, e Redo. Il test di rifiuto del patcher
ora salva anche dopo il rollback e verifica che root e AP non abbiano una child
raggiungibile. Questa correzione non cambia l'architettura parent-AP + child
Form o la coordinata canonica; il gate ha poi autorizzato soltanto i profili e
le operazioni riportati nello stato finale.

### Follow-up gate: bordo xref 516 dopo resize

Il cambio famiglia è **atteso**, non un fallback: xref 516 ha una sola
famiglia dominante importata (Myriad Pro), quindi la child production contiene
il Type0 `/BaseFont /Source#20Sans#203#20Regular` con discendente
`/SourceSans3-Regular`; Qt carica e risolve `Source Sans 3`. I run alternativi
continuano a usare Courier Prime nelle regressioni multi-run. Il launcher e il
test Qt riportano le famiglie production effettivamente caricate, non Arial,
Helvetica, Courier New o Myriad Pro.

La perdita del bordo non era nella child Form. Prima del fix, per questa
FreeText Acrobat `/C` giallo veniva esposto da PyMuPDF anche come
`annot.colors['stroke']`; il profilo lo usava erroneamente come bordo. Il rosso
autorevole era invece in `/DA` e `/DS`. Inoltre l'annotazione non aveva `/BS`:
il parent AP nativo usava `B` (fill+stroke) senza `w`, cioè la width PDF di
default 1; PyMuPDF riferiva perciò `annot.border.width == -1`. La shell
normalizzata finiva così con bordo giallo su fill giallo e `/BS /W 0`.

Il tracciamento A→E nel subprocess worker ora verifica `/C`, `/DA`, `/DS`,
`/BS`, `/Border`, `/CA`, `annot.colors`, `annot.border`, opacity e gli operatori
del parent senza esportare testo PDF. A/B mantengono fill giallo e bordo rosso;
C contiene `/BS <</W 1/S/S>>`; D, immediatamente dopo `Annot.update()`, ha
`1 w`, rosso `RG` e paint di stroke; E conserva gli stessi operatori e aggiunge
solo `/CanonicalText`. La child contiene soltanto operatori testo/font: non
disegna rettangoli, fill o bordo.

La correzione legge il colore semantico da `/DA`, rileva il solo token di paint
del parent AP per distinguere un bordo FreeText visibile da uno assente, e
invoca `annot.set_border(width=..., style="S")` prima di `Annot.update()`, lo
stesso contratto PyMuPDF usato dal percorso Text Box. Una fixture FreeText con
`/BS /W 0` verifica che la normalizzazione conservi `/W 0` e non produca paint
di stroke. Rollback rimane lo snapshot root/AP già verificato.
