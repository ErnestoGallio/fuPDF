# M19 - Prototipo di correzioni motivate

Data: 10 novembre 2026. **Verifiche tecniche PASS; collaudo Adobe Acrobat NOT RUN.**

## Implemented / perimetro

- Creati un generatore CLI sperimentale e test isolati in `experiments/m19/`.
- Tre correzioni native, ciascuna con una risposta contenente la motivazione;
  autore delle correzioni e delle risposte: `Revisione IA`.
- Nessuna funzione IA viene eseguita: testo e motivazioni sono fixture statiche.
- Nessuna modifica a `src/`, capability policy, Save/Save As, dipendenze,
  packaging o documenti normativi. Nessun commit o push.
- Working tree iniziale pulito. HEAD e tag `v0.17.1` entrambi:
  `14a96dc61d28e20d897e0cec50509c18ad1b0575`. Nessuna scrittura alla release
  `C:\fuPDF\stable\fuPDF.exe`.
- M19 non è presente nel ROADMAP attuale. Il perimetro di questa iterazione è
  quello della richiesta esplicita allegata, inclusa l'esclusione della full suite.
  Le vecchie restrizioni sui tool non vengono cambiate né estese dal prototipo.

## Ricognizione della produzione

Percorso esaminato: comandi `CreateReplaceText` / `CreateDeleteText` /
`CreateInsertText`, `AnnotationService`, dispatcher autorevole del worker,
`ProductAnnotationAdapter`, `worker/editorial_adapter.py`, fixture e report M10.

Il prototipo invia gli stessi DTO serializzati al dispatcher/servizio autorevole
del worker, con target confermati da `WorkerTextService`, e riusa l'adapter di
produzione senza modificarlo. Il processo CLI è l'unico proprietario PDF durante
la sua esecuzione: apre un documento alla volta, senza thread PyMuPDF o GUI.
Questo harness non esercita il trasporto GUI/IPC, la pipeline Save o la policy.
La sola scrittura sperimentale nuova è quella delle risposte, esterna a `src/`.

Strutture effettivamente prodotte dall'adapter:

| Correzione | Radice logica | Componenti / semantica nativa |
| --- | --- | --- |
| Replace | `/Caret /IT /Replace` | `/Contents` e `/RC` della radice contengono il testo sostitutivo. Lo `/StrikeOut /IT /StrikeOutTextEdit` contiene i quad del range e punta alla radice con `/IRT` e `/RT /Group`. |
| Delete | `/StrikeOut` | Quad del range; nessun `/Contents`, `/IT`, `/IRT` o `/RT` sulla radice. |
| Insert | `/Caret` | Confine confermato rappresentato da `/Rect` e `/RD`; payload in `/Contents` e relativo `/RC`; nessun `/IT`, `/IRT` o `/RT`. |

Ogni componente ha un proprio `/Popup` con riferimento inverso `/Parent`.
Il `/Subj` del Caret Replace è `Inserted Text`, come nell'adapter esistente:
la classificazione Replace dipende dal grafo e dall'intent, non da quella label.
Gli xref sono locator del singolo file, non identità permanenti. Si usa `/NM`
per ritrovare la radice; il membro Replace conserva il suffisso `:strike`.

La reference M10 e il relativo report registrano evidenza storica Acrobat per i
profili delle correzioni. Non dimostrano l'interoperabilità delle nuove risposte.

## Collegamento scelto e fonti verificate

Risposta `/Subtype /Text`, `/IRT <radice> 0 R`, `/RT /R`, `/Contents` contenente
solo la motivazione, `/T (Revisione IA)`, `/Subj (Motivazione)`, `/NM` univoco
con suffisso `:reason`, `/CreationDate` e `/M`. Niente `/State` o `/StateModel`.
Il Popup della risposta punta alla risposta stessa, mai alla correzione.

La [PDF Reference Adobe 1.6, tabelle 8.17 e 8.30, pp. 583-584 e 599](https://opensource.adobe.com/dc-acrobat-sdk-docs/pdfstandards/pdfreference1.6.pdf)
prevede `/IRT` sulla stessa pagina; `/RT /R` distingue una risposta da un membro
`/Group`. Una radice di gruppo può avere entrambe le relazioni. `/RT` omesso
equivale a `/R`; `/T` identifica convenzionalmente l'autore. Il Popup è opzionale;
quando ha un Parent, usa i contenuti e i metadati del Parent. La motivazione
viene quindi memorizzata nella risposta, non nel Popup o nella radice.

La [documentazione ufficiale PyMuPDF Annot](https://pymupdf.readthedocs.io/en/latest/annot.html#Annot.set_irt_xref)
documenta `set_irt_xref()` senza necessità di `update()`, `set_info()` e
`set_popup()`. Verificate disponibilità e implementazione locale in **1.28.2**:
`set_irt_xref()` scrive `/IRT` ma non `/RT`; quest'ultimo viene impostato con
`xref_set_key()`. Lo script controlla versione e appartenenza alla stessa pagina.

| Possibilità | Valutazione per questo spike |
| --- | --- |
| Text + IRT + RT R esplicito | Scelta consigliata per il collaudo: relazione inequivocabile e verificabile. Compatibilità Acrobat ancora da provare. |
| Text + IRT, senza RT | Alternativa equivalente secondo lo standard; non generata né collaudata separatamente. Nessuna evidenza che sia più interoperabile. |
| RT Group per la motivazione | Scartata: farebbe della motivazione un componente del gruppo invece di una risposta. |
| Risposta al membro StrikeOut o al Popup di Replace | Scartata: target logico sbagliato; coperta dai test negativi. |
| Motivazione nei Contents/RC della radice | Scartata: contaminerebbe il payload editoriale. |

Non sono stati creati profili alternativi per le tre correzioni: sono riusati
quelli di fuPDF. Non viene introdotto alcun tool Sticky Note nel prodotto.

## Annotazioni composte e particolarità riscontrate

Replace conserva **due figli diversi della stessa radice**:

```text
StrikeOut 44 -- IRT / RT Group --> Caret 47 (Contents: è, IT: Replace)
Text 50 ----- IRT / RT R -------> Caret 47 (risposta con motivazione)
```

Il test distingue queste relazioni e verifica che l'aggiunta della motivazione
non alteri tipo, intent, payload, geometria, colori, metadati o appearance dei
componenti esistenti. Una sola correzione non coincide con un solo oggetto PDF.

In PyMuPDF 1.28.2 `set_info(title=...)` sul Caret già posizionato ha provocato
una successiva rigenerazione del Rect durante il rendering: il test ha rilevato
il passaggio da circa 18,92 x 15,42 pt a 20 x 14 pt. Per l'autore del solo
campione si scrive direttamente `/T`, senza rigenerare l'appearance. Per la
nuova risposta si usa l'API pubblica. Nessuna correzione al codice applicativo.

Poppler dipinge le icone Note delle risposte anche con IRT/RT corretti. Le icone
sono collocate nel margine destro per non coprire il testo. Non sono impostati
flag Hidden/NoView per mascherare eventuali difetti di interoperabilità.
Questo rendering non dimostra come Acrobat presenterà il thread.

## Risultato e controlli tecnici

Directory: `output/pdf/m19/`.

**PDF da aprire in Acrobat:** `m19_motivated_corrections.pdf` (3 pagine, PDF 1.7).

| Pagina/caso | Operazione richiesta | Radice | Risposta / IRT / RT | Altri componenti |
| --- | --- | --- | --- | --- |
| 1 / A | `sono` -> `è` in `La relazione sono chiara.` | Caret 47 | Text 50 / 47 0 R / R | StrikeOut 44 -> 47, Group; Popup 46, 49, 51 |
| 2 / B | Elimina il **secondo** `molto `, spazio incluso, da `Questo esempio è molto molto chiaro.` | StrikeOut 53 | Text 56 / 53 0 R / R | Popup 55, 57 |
| 3 / C | Inserisci `è `, spazio finale incluso, prima di `chiara` in `La relazione chiara.` | Caret 59 | Text 62 / 59 0 R / R | Popup 61, 63 |

Motivazioni attese, distinte dai payload:

- A: `Il soggetto 'La relazione' è singolare: il verbo deve concordare al singolare.`
- B: `La seconda occorrenza di 'molto' è una ripetizione involontaria e va eliminata.`
- C: `Manca la copula tra il soggetto e l'aggettivo: inserire il verbo 'è'.`

Controlli PASS:

- 3 correzioni logiche, 4 componenti editoriali, 3 risposte, 7 Popup:
  **14 oggetti annotazione** elencati dall'array Annots, senza residui inattesi.
- Conteggio revisioni delle sole correzioni tramite dispatcher: 1, 2, 3.
  Le risposte non hanno una revisione di prodotto: sono scritture del solo spike.
- Due cicli save/reopen PyMuPDF, preservando l'intero grafo osservato.
- Testo estratto, hash degli stream pagina decodificati, raster senza annotazioni,
  dizionari pagina escluso Annots e metadati base invariati.
- SHA-256 del sorgente invariato: viene annotata una copia temporanea privata.
- Profilo nativo, geometria e hash degli appearance stream delle correzioni
  invariati dopo le risposte e dopo entrambe le riaperture.
- Payload Unicode e motivazioni separati anche con accenti, caratteri combinanti,
  `&`, `<`, `>`, `①` e testo su più righe; `/RC` delle correzioni coerente.
- Test negativi per IRT al membro/Popup, RT Group al posto di R, contaminazione
  del payload, modifica del contenuto pagina e corruzione dell'appearance.
- Rifiuto di riutilizzare una directory output esistente, senza sovrascritture.
- Rendering e ispezione visiva di tutte le tre pagine con Poppler: testo leggibile,
  indicatori sul target, nessuna sovrapposizione delle icone con il testo.
  Poppler emette avvisi sui font di fallback `Symbol` e `ArialUnicode`.

SHA-256 PDF principale:
`06e9fc47e303b08ad4c59b194a544724cd3e0d7d11114d5d73e374015041ea8a`.

Altri file: `m19_source.pdf` (baseline), `m19_pymupdf_resaved.pdf` (secondo
salvataggio), `manifest.json` (comandi, profili prima delle risposte, hash),
`inspection.json` (controlli e dizionari annotazioni dopo reopen),
`native_inspection.json` (inspector M10 indipendente, sola lettura).

## Files changed

- `experiments/m19/motivated_corrections.py`: generatore, risposte isolate,
  verifiche, esportazione riproducibile su directory nuova.
- `experiments/m19/test_motivated_corrections.py`: 11 test mirati.
- `M19_REPORT.md`: ricognizione, evidenze, limiti e protocollo manuale.
- `output/pdf/m19/`: i tre PDF sperimentali e i tre JSON descritti sopra.

## Tests / comandi eseguiti

Dal repository, senza aggiornare dipendenze:

```powershell
.\.venv\Scripts\python.exe -m pytest experiments/m19/test_motivated_corrections.py -q
# 11 passed in 1.51s

.\.venv\Scripts\python.exe -m experiments.m19.motivated_corrections --output-dir output/pdf/m19
# exit 0; automatic_checks PASS; acrobat NOT RUN

.\.venv\Scripts\python.exe -m pdf_annotator.spikes.editorial_inspector output/pdf/m19/m19_motivated_corrections.pdf --output output/pdf/m19/native_inspection.json
# exit 0

pdftoppm -r 110 -png output/pdf/m19/m19_motivated_corrections.pdf tmp/pdfs/m19/page
# exit 0; tre PNG ispezionati, avvisi font sopra riportati

pdfinfo output/pdf/m19/m19_motivated_corrections.pdf
# exit 0; 3 pagine; PDF 1.7; non cifrato; nessun form o JavaScript

git diff --exit-code -- src pyproject.toml packaging PROJECT_SPEC.md ARCHITECTURE.md ACCEPTANCE_TESTS.md ROADMAP.md AGENTS.md
# exit 0; nessuna modifica

git diff --check
# exit 0
```

Per rigenerare, scegliere una **nuova directory** (per esempio
`--output-dir output/pdf/m19-rerun`); il generatore rifiuta quella già esistente.
La full suite è **NOT RUN per istruzione esplicita**, così come packaging.

## Acceptance status

| Gate | Stato di questa iterazione |
| --- | --- |
| Controlli tecnici M19 sui tre casi | PASS, inclusi i test negativi e due reopen |
| AT-EDIT-01..03 | PASS delle sole sottoprove di persistenza su questa fixture; non è una riesecuzione integrale dei gate con varianti geometriche |
| AT-CMD-01 | Riutilizzato il dominio e dispatcher/adattatore reali; gate completo GUI/IPC e sette strumenti NOT RUN |
| AT-EDIT-04 / lifecycle completo con risposte | NOT RUN; nessuna implementazione prodotto di history/delete/reader delle risposte |
| AT-ACROBAT-06..08 + presentazione delle motivazioni | NOT RUN, richiede il collaudo sotto |
| AT-ACROBAT-09 / ritorno in fuPDF | NOT RUN; reader delle risposte escluso dall'iterazione |
| AT-INDESIGN-01..04 | NOT RUN; fixture sintetica, nessuna coppia InDesign sorgente/export o prova Accept |
| Save/Save As di produzione e full suite | NOT RUN per perimetro |

## Collaudo manuale preciso in Adobe Acrobat Desktop

1. Registrare versione/build Acrobat, Windows, data e SHA-256 del campione.
   Aprire `output/pdf/m19/m19_motivated_corrections.pdf` direttamente in Acrobat
   Desktop e aprire il pannello **Commenti**. Disattivare filtri che nascondano
   risposte/autori; espandere i thread. Non usare il browser come gate.
2. Pagina 1: verificare tipo **Sostituisci testo / Replace Text**, range `sono`,
   payload esatto `è`. La motivazione A deve essere leggibile come **risposta
   figlia della stessa correzione**, autore `Revisione IA`. Nessun secondo
   commento indipendente per lo StrikeOut del gruppo o per la motivazione.
3. Pagina 2: verificare tipo **Barra/Elimina testo / Strikethrough Text**, solo
   il secondo `molto ` barrato. La motivazione B è una risposta, non il testo
   di un'operazione di sostituzione. Nessuna aggiunta testuale richiesta.
4. Pagina 3: verificare tipo **Inserisci testo / Insert Text**, confine subito
   prima di `chiara`, payload `è ` con spazio finale. Motivazione C leggibile
   nel thread e non incorporata nel testo da inserire.
5. Per tutti i casi controllare l'autore, la leggibilità della risposta nel
   pannello, il target e la selezionabilità della correzione nativa. Le tre
   radici devono avere una risposta ciascuna, non tre note autonome. Registrare
   screenshot dei thread espansi e le label effettive dei tipi riconosciuti.
6. Salvare con nome **`m19_acrobat_roundtrip.pdf`**, senza ricreare o riparare
   annotazioni. Chiudere il documento, riaprire quella copia in Acrobat e
   ripetere i controlli dei punti 2-5. Payload e motivazioni devono restare
   distinti e invariati, commenti selezionabili e thread ancora corretti.
7. Verificare che il testo originale della pagina sia ancora quello della
   colonna sorgente, non il testo corretto. Gli esiti editoriali richiesti
   sarebbero A/C `La relazione è chiara.` e B `Questo esempio è molto chiaro.`,
   ma **queste annotazioni non devono applicare tali modifiche al PDF**.
8. Opzionalmente, su un'ulteriore copia modificare una motivazione aggiungendo
   `[test manuale]`, salvare e riaprire: deve restare una risposta della stessa
   correzione e il payload editoriale non deve cambiare. Conservare separata
   questa copia dalle evidenze dei controlli senza modifiche.
9. Conservare il file Acrobat, screenshot, build e PASS/FAIL per caso; annotare
   se la risposta diventa autonoma, cambia padre, scompare o altera il payload.
   Un tipo sbagliato o un thread sbagliato è FAIL, anche se la pagina appare
   visivamente corretta. Non correggere il campione in Acrobat per farlo passare.

Per un'ispezione di sola lettura della copia salvata da Acrobat:

```powershell
.\.venv\Scripts\python.exe -m pdf_annotator.spikes.editorial_inspector output/pdf/m19/m19_acrobat_roundtrip.pdf --output output/pdf/m19/acrobat_inspection.json
```

Acrobat può riscrivere xref e metadati legittimamente: confrontare i nomi `/NM`,
il grafo IRT/RT, i tipi, il target e i testi, non numeri xref hardcoded o byte del
file. Le verifiche esatte del generatore qualificano solo i suoi save PyMuPDF.

## Known limitations / prossime dipendenze

Interoperabilità Acrobat delle risposte e persistenza dopo save Acrobat ancora
da confermare. Nessuna conclusione su InDesign/Accept o sul salvataggio del
campione attraverso fuPDF. Reader, capability policy, sidebar, history e ciclo
di vita delle risposte non sono implementati o qualificati qui. Le motivazioni
non entrano nel modello di revisione applicativo. Nessuna implementazione
definitiva deve iniziare senza valutare prima il risultato manuale.
