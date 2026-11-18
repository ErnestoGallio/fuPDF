# Fixture PDF pubblicabili e collaudo Adobe

I test automatici generano i PDF in una directory temporanea. Il generatore
M10 prepara un grafo esterno di Delete, Insert e Replace con popup, array
Annots indiretto e raggruppamento Replace. Il generatore M15.1 prepara
FreeText e Callout importati, compreso un Callout parzialmente fuori pagina,
appearance Form con risorse proprie, Ink e risposte su più pagine.

## Riprodurre le prove sintetiche

Da PowerShell, scegliere directory di output nuove.

~~~powershell
$python = '.\.venv\Scripts\python.exe'
& $python -m tests.m10_probe output/m10/probe
New-Item -ItemType Directory -Force output/m15_1
& $python -m tests.m151_fixtures output/m15_1/imported-example.pdf
& $python -m tests.m151_probe output/m15_1/imported-example.pdf output/m15_1/imported-example-reviewed.pdf
& $python -m tests.m151_verify_artifact output/m15_1/imported-example.pdf output/m15_1/imported-example-reviewed.pdf
~~~

Il confronto sintetico verifica struttura, geometria e preservazione.
I PDF prodotti da questi comandi sono fixture PyMuPDF.


## Preparare il nuovo riferimento Acrobat

**Stato M22.3a** — Il candidato sintetico è stato annotato e salvato con
**Adobe Acrobat Reader 2026.002.22031, 64 bit, Continuous Release**.
Il file distribuito con i test è
[`acrobat-reader-editorials.pdf`](../fixtures/m10/acrobat-reader-editorials.pdf).
I cinque casi `[acrobat]` usano questo file per impostazione predefinita.

### Esito del candidato

SHA-256 `138f808fa32eb6d322219d54413c8e4804bacf92df2f7d6ad4dc7c9c7234d883`.
Il PDF conserva testo e stream di pagina del sorgente. `/Producer` conserva
`PyMuPDF 1.28.2` dal sorgente, mentre i Caret riportano `Acrobat:26.2.0`
nel rich text. L'autore delle quattro annotazioni primarie è `Example Reviewer`.
L'array `/Annots` è indiretto. I quattro popup hanno riferimenti reciproci
con le primarie, che hanno `/NM` distinti e appearance `/AP /N` non vuoti.
Replace conserva il membro StrikeOut con `/RT /Group` e `/IRT` verso il Caret.

L'unico font incorporato corrisponde byte per byte a Source Sans 3 Regular
del progetto. Helvetica e ZapfDingbats compaiono come risorse Base-14 senza
file font incorporati. L'AcroForm aggiunto da Reader è privo di campi.
Il controllo di Info, XMP, oggetti e stream non ha rilevato dati identificativi
reali o allegati.

### Confronto della geometria

Entrambi i quad contengono i centri degli stessi cinque caratteri di `brown`.
La pagina è senza rotazione, ha CropBox e MediaBox coincidenti con origine
zero e altezza di 640 pt. In entrambi i file la conversione da coordinate
PDF a coordinate canoniche è `(x, 640 - y)` e riproduce i vertici letti
dall'API. Il test verifica anche i vertici scritti dal worker rispetto al
target confermato.

In coordinate PDF native, il bordo alto del quad è a `565.000` pt per
fuPDF e `564.370` pt per Reader. Il bordo basso è a `545.110` pt per
fuPDF e `545.575` pt per Reader. Gli scarti verticali di 0,630 e 0,465 pt
riflettono i diversi limiti verticali del testo. Il confronto tra produttori
ammette 0,75 pt in verticale, con 0,120 pt di margine rispetto allo scarto
maggiore osservato. Sull'asse orizzontale, sui vertici rispetto al target del
worker e sul confronto sintetico resta 0,002 pt. I test negativi rifiutano
una traslazione di 0,1 pt in orizzontale o 1 pt in verticale.

### Copertura da preservare

I cinque casi usano la fixture parametrica `external_editorials` con un
ramo sintetico e uno Acrobat.

| Test | Cosa verifica |
| --- | --- |
| `test_editorial_persistence_m10.py::test_reference_and_worker_have_same_semantic_graph` | Confronta subtipi, intent, `/RT`, `/IRT`, payload, numero di componenti, popup e quad. Per la barratura autonoma confronta tutti i valori di `/QuadPoints` con le tolleranze sopra e verifica che entrambi i quad selezionino `brown`. L'aggiunta di annotazioni fuPDF preserva chiavi, riferimenti e stream della baseline, testo, stream di pagina e raster senza annotazioni. L'hash della reference resta identico. |
| `test_annotation_inventory_m12.py::test_external_replace_is_one_logical_record` | Riconosce Delete, Insert e Replace, espone un solo Replace con almeno due componenti e capability di gruppo. L'autore appare nella sidebar e il dettaglio contiene esattamente `sleepy`. |
| `test_annotation_inventory_m12.py::test_external_replace_delete_is_atomic_and_keeps_no_child_record` | Replace è eliminabile. Dopo la cancellazione spariscono il record logico e ogni record Replace residuo. |
| `test_save_m15.py::test_external_editorial_graph_appearance_preserved` | Confronta il digest del PDF e dell'inventario prima e dopo Save As e Save. Controlla testo, stream, annotazioni e capability di modifica previste dal profilo. |
| `test_save_m15.py::test_seven_tools_rich_unicode_replace_history_roundtrip` | Esercita i sette strumenti, conserva testo e stream originali e gli autori delle annotazioni importate per `/NM`. Verifica autore e payload Unicode delle annotazioni aggiunte, Undo/Redo, ripristino del grafo Replace dopo Save As e riapertura. |

Il riferimento storico esercita quattro oggetti primari. Conservare questa
struttura e i rispettivi appearance nativi nell'output di Acrobat.

- **Delete** — `/StrikeOut` senza intent editoriale esplicito né payload,
  un quad sul solo `brown` e un popup.
- **Insert** — `/Caret` senza `/IT`, al confine dopo `The` e prima
  dello spazio originale. Payload ` very` con uno spazio iniziale e un popup.
- **Replace** — `/Caret` con `/IT /Replace` e payload `sleepy`,
  più `/StrikeOut` con `/IT /StrikeOutTextEdit`, `/RT /Group` e
  `/IRT` verso il Caret. Il quad copre solo `lazy`. Entrambi gli
  oggetti hanno un popup.

Verificare `/Popup` e `/Parent` reciproci, `/AP /N` e identificativi
`/NM` distinti sulle primarie. Il riferimento storico usa un array `/Annots`
indiretto. Registrare la forma osservata e mantenere la copertura di questo
percorso, già presente anche nel test sintetico. Numeri di oggetto, UUID,
timestamp e coordinate assolute appartengono al nuovo file. La geometria si
confronta sul nuovo testo e sul nuovo font.

### Sessione locale in Acrobat Desktop

1. Genera il PDF di partenza con PowerShell. La pagina contiene una frase
   inventata e incorpora il TTF Source Sans 3 del progetto, distribuito
   con la [licenza OFL](../../src/pdf_annotator/assets/fonts/SOURCE_SANS_3_LICENSE.md).

~~~powershell
$python = '.\.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force output/pdf/m22_3a
& $python -m tests.m10_helpers --acrobat-source output/pdf/m22_3a/acrobat-source.pdf
~~~

2. Apri `acrobat-source.pdf` in **Acrobat Desktop** e lavora in locale.
   Nelle preferenze dei commenti disattiva l'uso automatico del nome di
   accesso come autore e, se attiva, la copia del testo selezionato nei
   popup delle barrature. Nelle proprietà dei commenti imposta l'autore
   fittizio **Example Reviewer**. Verificalo per tutte e tre le correzioni.
   [Impostare il nome autore in Acrobat](https://helpx.adobe.com/ee/acrobat/desktop/share-and-review-documents/manage-comment-preferences/comments-author-name.html)
3. Usa gli strumenti **Commento** sulla frase «The brown rover sketches a
   map for a lazy comet.». Barra esattamente `brown` e lascia vuoto il
   commento. Con **Inserisci testo al cursore**, colloca il Caret subito
   dopo `The` e inserisci ` very`. Con **Sostituisci testo selezionato**,
   seleziona solo `lazy` e inserisci `sleepy`. Usa le correzioni
   come annotazioni e lascia intatto il testo della pagina.
   [Strumenti Adobe per le correzioni testuali](https://helpx.adobe.com/in_hi/acrobat/how-to/gather-feedback-pdf-comments.html)
4. Usa **Salva con nome** e salva localmente come
   `output/pdf/m22_3a/acrobat-reference.pdf`. Riapri in Acrobat e verifica
   le tre correzioni e gli autori. Registra versione/build di Acrobat e
   SHA-256 della copia. Lascia intatti i dizionari nativi prodotti da Acrobat.

### Qualifica della copia salvata

Controllare `/Producer`, `/Creator`, XMP ed eventuali marcatori Adobe
nelle annotazioni, insieme alla provenienza della sessione. Il produttore di
origine può rimanere nei metadati anche dopo il salvataggio di commenti in
Acrobat. Verificare assenza di nomi, email, percorsi personali, allegati e
dati di revisione estranei. Identificare tutti i font incorporati, anche
negli appearance, e verificarne la redistribuibilità prima dell'inclusione.
Rifiutare copie con dati identificativi e rigenerarle da un profilo neutro
senza riscrivere il grafo per far passare i test.

~~~powershell
Get-FileHash -Algorithm SHA256 output/pdf/m22_3a/acrobat-reference.pdf
$env:FUPDF_TEST_ACROBAT_REFERENCE = (Resolve-Path output/pdf/m22_3a/acrobat-reference.pdf).Path
& $python -m pytest -q tests/test_editorial_persistence_m10.py tests/test_annotation_inventory_m12.py tests/test_save_m15.py -k 'reference_and_worker or external_replace or external_editorial_graph or seven_tools'
Remove-Item Env:FUPDF_TEST_ACROBAT_REFERENCE
~~~

Per usare la fixture pubblica, eseguire gli stessi test senza impostare la
variabile. `FUPDF_TEST_ACROBAT_REFERENCE` resta disponibile per qualificare
un nuovo candidato o eseguire separatamente il confronto storico locale. I risultati di questi test coprono
l'importazione e la preservazione. I gate manuali Acrobat e InDesign
dell'app restano distinti.

Per i Callout e FreeText, ripetere il gate con commenti creati in Acrobat
su una prova sintetica, compresi testo misto in grassetto, ritorni di paragrafo,
leader obliquo e un box al margine della pagina. Verificare modifica,
salvataggio, riapertura in Acrobat e ritorno in fuPDF secondo AT-ACROBAT-03/04/05.

Per AT-INDESIGN-01..04, creare l'export da un sorgente InDesign sintetico
e conservarlo invariato. Importare le annotazioni, verificare tipo e target,
scegliere Accept e confrontare il testo finale esatto. Ripetere i round trip
previsti da [ACCEPTANCE_TESTS.md](../../ACCEPTANCE_TESTS.md).
