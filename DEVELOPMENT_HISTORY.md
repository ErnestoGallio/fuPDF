# Note storiche di sviluppo M0–M4

Queste note conservano le misure e il contesto delle prime milestone. Per installazione e stato corrente, consulta il [README](README.md).

## M4 — Layout continui

Il piccolo menu accanto allo zoom offre **Singola**, **Doppia** e **Doppia con
copertina**. La scelta vive in `DocumentSession.ui.layout`; ogni nuova apertura
parte da Singola. Zoom, posizione e layout restano indipendenti durante i cambi
tab; non c'è persistenza fra avvii.

`PageLayout` è l'unico motore geometrico. Singola mantiene le righe M3; Doppia
raggruppa `(1,2), (3,4), …`; con copertina raggruppa `(1), (2,3), (4,5), …`.
La copertina e le righe complete sono centrate; l'ultima pagina spaiata parte
dal margine sinistro dello spazio comune. Nessuna pagina fittizia. Ogni pagina
conserva le proprie dimensioni visuali dopo rotazione/CropBox; nello spread
l'allineamento è superiore e l'altezza è quella della pagina più alta.
Il gap orizzontale è il parametro interno `column_gap`, default **16 pixel logici**,
indipendente da zoom/DPR. Gap verticale 16, margini 20, come M3.

L'anchor è un punto canonico della pagina visibile più vicina al centro del
viewport (o al cursore per Ctrl+rotellina), limitato alla sua porzione visibile.
A parità di distanza si preferisce l'area visibile maggiore, poi la pagina
sinistra/precedente. Il punto scelto resta valido attraverso cambi geometrici
consecutivi: a basso zoom il gutter non deve spostarlo sulla pagina compagna.
Scroll e resize determinano un nuovo anchor. Le scrollbar possono limitarne
la posizione esatta ai bordi; non cambia la geometria PDF persistente.

La ricerca delle righe visibili usa bisezione, seguita dall'intersezione con i
rettangoli delle singole pagine, anche quando una pagina è più bassa dell'altra.
Il prefetch comprende le compagne non visibili e, per default, **una riga per
lato**, prioritizzando la direzione di scroll. Non ci sono widget per pagina.
Renderer, scheduler, mapper, worker/IPC e chiavi raster sono quelli M3: **il layout
non entra nella chiave**, non svuota la cache e non riavvia worker.
Restano **128 MiB globali / 32 MiB in transito e 2 slot / 16 MiB per raster**,
trimming delle tab inattive e rendering base `annots=False`.

### Verifica M4 e confronto Windows

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
& '.\.venv\Scripts\python.exe' -m pytest tests/test_virtual_viewer.py tests/test_raster_cache.py tests/test_coordinates.py -q
& '.\.venv\Scripts\python.exe' -m pytest -q
$env:QT_QPA_PLATFORM = 'windows'
& '.\.venv\Scripts\python.exe' -m tests.m3_benchmark --native --foreground
& '.\.venv\Scripts\python.exe' -m tests.m4_smoke
git diff --check
```

Baseline prima delle modifiche: **80 passed**, 126,94 s. I test M4 estendono la
suite viewer: pairing esatto da 1 a 5 pagine in tutti i modi, expected geometrici
numerici, 50/100/150/300%, anchor prima/ultima/sinistra/destra, CropBox e tutte le
rotazioni a DPR 2, cache riusata, 300 pagine virtualizzate, risultati obsoleti,
due tab indipendenti e degradazione entro un budget di soli 64 KiB.
Run conclusivo: **65 test mirati passati in 63,96 s**; suite completa M0–M4
**111 passed in 139,46 s**. `git diff --check`: nessun errore. Nessuna nuova
dipendenza o modifica ai documenti normativi.

Confronto con M3 originale (`509ab6e`), estratto in una directory ignorata senza
modificare il checkout. Stesso scenario M3, stesso runtime Windows, DPR 1,25,
finestra 1080×920 e finestra attiva nel 100% dei tick:

| Misura | M3 originale | M4, stesso scenario singolo |
| --- | ---: | ---: |
| Viewport→raster lungo P95 | 22,26 ms | 21,01 ms |
| Timer Qt 10 ms, P95 / max | 11,29 / 17,37 ms | 11,39 / 21,28 ms |
| Render medium + long | 318 | 318 |
| Cache hit / miss / eviction | 33 / 617 / 246 | 33 / 617 / 246 |
| Picco raster residente | 127,76 MiB | 127,76 MiB |
| Picco working set GUI campionato | 237,68 MiB | 238,27 MiB |
| Picco somma working set dei due worker | 101,24 MiB | 100,75 MiB |

La prima baseline aveva P95 viewport 19,57 ms e timer 12,81 ms. Le prove
intermedie senza controllo dell'attivazione arrivavano a 33–38 ms e timer circa
16 ms. L'ipotesi iniziale sul selettore UI non è stata confermata: attivando
esplicitamente la finestra nel **solo harness**, le misure tornano comparabili.
`--foreground` e i contatori dei tick attivi rendono esplicita questa condizione;
nessun timer di prodotto, priorità di processo o allocator viene forzato.
I valori non sono una garanzia per ogni PDF/hardware.

Smoke M4 nativo con fixture locali medium 60 pagine, long 300, odd 5 e mixed 8:
scroll di tutti i 150 spread, salto alla pagina 251, ritorni, layout ripetuti,
zoom fino a 300%, prima/ultima pagina e switching tra tab. Nove frame ispezionabili,
senza sovrapposizioni o raster di pagine errate nei frame controllati. Long:
viewport→raster P95 **33,55 ms** con due pagine per riga, request→visible P95
**18,20 ms**; timer P95 **13,43 ms**, max **22,54 ms**. Cambio geometria lungo
max **1,28 ms**. I cambi di layout sul recente intorno in cache richiedono
**zero nuovi render**. Picco raster **127,98 MiB**, transito **16 MiB**, zero
worker dopo lo shutdown. Working set GUI campionato fino a **226,24 MiB**, worker
insieme **101,90 MiB**, senza crescita sostenuta nel tratto finale.
Le immagini diagnostiche sono limitate a nove (fino a 65,66 MiB separati dal
budget raster, inclusi nel working set del processo di test) e scritte dopo
la chiusura. Non sono retention del viewer.

Report locali del confronto: `.m0-artifacts/m4-reference-96zgf75s/.m0-artifacts/m3-wc3n5tef/report.json`,
`.m0-artifacts/m3-5hroilcu/report.json`. Smoke e frame:
`.m0-artifacts/m4-native-fsxx9dxn/`. Sono prove native automatizzate con ispezione
visiva dei frame, non una sessione manuale dell'utente su PDF privati.

Restano fuori M4 annotazioni, command/service layer, testo, ricerca, sidebar,
undo/redo, salvataggio, fit e miniature. AT-DOC-06 e AT-PERF-10 restano parziali
per queste funzioni future; AT-VIEW-07/08 e i gate Adobe non sono eseguiti.

| Acceptance M4 | Esito |
| --- | --- |
| AT-VIEW-01/02/03/04/05/06 | PASS nel viewer base |
| AT-PERF-01/02/03/04/05/06/07/08/09/11/12/13 | PASS nelle prove e regressioni eseguite |
| AT-DOC-06, AT-PERF-10 | PARTIAL: stato layout/zoom/posizione e rendering verificati; ricerca/editor futuri |
| AT-COORD-01/02 | PARTIAL: regressione numerica/raster PASS; annotazioni interattive fuori scope |
| AT-VIEW-07/08, Acrobat/InDesign | NOT RUN, fuori M4 |

API del selettore verificate: [QToolButton](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QToolButton.html)
e [QActionGroup](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QActionGroup.html).

## M3 — Zoom, cache e priorità (storico)

### Modello di zoom

Ctrl+rotellina e i pulsanti −/+ modificano lo zoom tra **25% e 800%**, con valore
percentuale nella toolbar. La geometria cambia subito e il raster già disponibile
viene scalato da Qt. Il punto PDF sotto il cursore rimane l'anchor della rotellina;
i pulsanti usano il centro del viewport, salvo i normali limiti delle scrollbar.
Layout e mapper restano quelli a pagina singola continua di M2.

Il debounce è **120 ms dall'ultimo input**, controllato con `perf_counter` nei
tick asincroni: non si attende il worker. La scala fisica richiesta è
`ceil(zoom * DPR * 8) / 8`, poi limitata prima di allocare. Questa quantizzazione
in ottavi di pixel/punto evita nuovi render per piccole variazioni equivalenti;
non cambia lo zoom geometrico. Il payload distingue `geometric_zoom`, `dpr`,
`requested_scale` e `raster_scale`. Nessuna seconda applicazione del DPR a geometria
PDF o immagine: il paint usa il rettangolo logico esplicito.

### Cache e memoria

| Risorsa globale | Limite M3 |
| --- | --- |
| Raster residenti | 128 MiB (rifinitura M3, confronto sotto) |
| Payload logici in transito | 32 MiB / 2 slot |
| Singolo raster, incluso padding Qt | 16 MiB |

I limiti di trasporto M2 e il profilo ridotto M0 restano invariati. Non sono limiti
dell'RSS: pixmap MuPDF, frame IPC, buffer ricevuti e copia Qt possono coesistere.
Il numero/dimensione delle copie è limitato dal trasporto; i working set sono
misurati separatamente. Non sono introdotte cache applicative TextPage/DisplayList.

La cache possiede le QImage e conta `bytesPerLine * height`. Chiave: documento,
generazione, pagina, scala fisica effettiva e DPR. Eviction deterministica:
prima raster lontani, poi nearby, infine visibili quando necessario; a pari
pertinenza vale LRU. Il paint usa una lease: un'immagine rimossa dalla lookup ma
ancora presa in prestito resta contabilizzata fino al rilascio. Le copie QPixmap
non sono necessarie. Le varianti di zoom restano riutilizzabili entro lo stesso tetto.

Prima di richiedere un raster, la quota massima per pagina è il minimo tra 16 MiB
e `resident_budget / (numero_target + 1)`. La quota aggiuntiva permette di sostituire
una preview senza sforare. Anche un insieme di sole pagine visibili troppo oneroso
converge a risoluzione ridotta, senza eccezioni al budget. Un test con 64 KiB e
almeno quattro pagine visibili copre questo caso. Le lease non rilasciate impongono
backpressure, non nuove allocazioni.

### Prefetch e tab

Visibili prima di nearby; per default una pagina per lato, prima nella direzione
di scroll. Oltre 1500 pixel logici/s, nuovo prefetch attende 100 ms dall'ultimo
scroll rapido, senza ritardare i visibili. Esiste sempre un solo ticket per viewer:
le richieste obsolete in coda sono saltate, quelle già eseguite sono scartate.
Non si memorizza la sequenza dei viewport attraversati.

Una tab inattiva rilascia **tutti** i propri raster, mantiene widget, posizione,
zoom e worker, e non esegue prefetch. Al ritorno renderizza l'intorno corrente.
Il documento attivo viene servito a ogni tick del manager; fino a sette altri
documenti ricevono polling a rotazione, senza affamare chiusure o aperture.
Chiusura e failure rimuovono le entry del documento interessato.

### Rifinitura M3 — confronto 256 / 128 / 96 MiB

**Default attuale: 128 MiB globali.** La richiesta di rifinitura autorizza la
variazione della costante interna rispetto al target iniziale di 256 MiB nei
documenti normativi, che restano invariati. Singolo raster **16 MiB**, transito
**32 MiB / 2 slot**, priorità, trimming e worker/IPC non cambiano.

Confronto nativo del 28 agosto 2026 sulla stessa macchina descritta sotto,
backend `windows`, DPR **1,25**, finestra di 1080×920 pixel logici.
Fixture riutilizzate identiche per sei processi GUI freschi, in ordine
256/128/96 e poi 96/128/256. Medio: **60 pagine / 9.356.272 byte**; lungo:
**400 pagine / 61.512.139 byte**, testo/linee più un'immagine RGB distinta e
poco comprimibile per pagina. Sono fixture sintetiche, non il PDF reale
dell'utente, che non è stato fornito. Identità SHA-256 in `fixtures.json`.

Ogni run comprende 566 osservazioni di viewport: lettura del medio da solo,
chiusura, attraversamento delle 400 pagine del lungo da solo, ritorni recenti,
salti lontani, lettura e ritorno su 24 pagine al 150%, secondo documento e sei
cambi tab. Il percorso accelerato percorre pagine nuove con pausa 25–50 ms;
il percorso al 150% lascia 150 ms per la lettura/prefetch. Il test pilota la
finestra nativa; i frame sono stati ispezionati. Catture solo alla fine delle
misure: nessuna immagine di screenshot trattenuta durante il campionamento RAM.

| Budget | Picco working set GUI / worker più oneroso, MiB | Cache hit / miss per run | Viewport→raster P50 / P95, ms | Eviction / rerender per run |
| --- | --- | --- | --- | --- |
| 256 MiB | 329,4–329,5 / 181,3–181,5 | 168–171 / 1029–1031 | 21,43 / 32,28 | 347 / 23 |
| **128 MiB** | **201,6 / 181,4–181,6** | **164 / 1035** | **21,43 / 30,55** | **408 / 27** |
| 96 MiB | 172,8–174,6 / 182,0–192,1 | 147–148 / 1046–1047 | 21,55 / 31,66 | 436 / 33 |

Percentili calcolati sulle 1132 osservazioni delle due ripetizioni, inclusi
cache hit e tre cambi zoom per run: misurano la disponibilità del raster,
non l'istante di presentazione fisica sul display. Cache hit/miss sono i contatori del prodotto
per target/epoch, non un conteggio di letture utente. Rerender = nuova adozione
della stessa chiave documento/generazione/pagina/scala/DPR già adottata; include
il comportamento previsto dopo trimming, non soltanto lavoro evitabile.
Render richiesti/completati: **543–548 / 547–565 / 557–564** rispettivamente;
gli scarti stale dipendono anche dal timing del prefetch, non solo dal budget.

Il dato decisivo è il ritorno esteso al 150%: 256 e 128 MiB hanno **24/24 hit
visibili per run**, zero placeholder e P95 aggregato **0,69 / 0,37 ms**;
128 MiB rifà soltanto una nearby. A 96 MiB: **21/24 hit**, sei rerender,
tre placeholder oltre 50 ms in entrambe le ripetizioni; P95 **94,54 ms**.
I ritorni recenti brevi sono tutti hit con tutti i budget, senza rerender.
Non emerge thrashing durante la lettura ordinaria; 96 MiB riduce però il margine
utile per tornare indietro a zoom moderato. **128 MiB risparmia circa 128 MiB
nel GUI** rispetto al default precedente senza questa regressione; gli ulteriori
27–29 MiB risparmiati da 96 MiB non compensano i miss aggiuntivi.

Durante il percorso accelerato con due documenti, P95 aggregati **83/86/85 ms**:
esiste variabilità del timing con tutti i budget. Cambio tab: P50 **26/25/26 ms**;
P95 **31/76/40 ms**, con tre attese oltre 50 ms in una sola ripetizione a 128.
Un'ulteriore prova a 128 dà P95 **29,37 ms** e nessuna attesa oltre 50 ms nel
cambio tab. Non è dimostrato un peggioramento sistematico causato dal budget;
non si nascondono però questi picchi dietro il solo percentile complessivo.
Timer a 5 ms: P95 6,24–6,65 ms, massimi isolati 26–85 ms nei sei run.

Picchi residenti: **255,52 / 127,76 / 95,82 MiB**, rispettivamente
**114 / 57 / 42 raster**. Il transito resta entro il limite, picco 16 MiB.
Con un solo documento il picco totale campionato GUI+worker passa da circa
**511 a 383 MiB**. Nella fase con due documenti il totale è circa **399–400 MiB**
con tutti i budget: l'intorno corrente non riempie nemmeno 96 MiB e il worker
inattivo resta aperto. Non si promette un limite dell'RSS né un risparmio fisso
per ogni PDF o numero di tab.

**Retention:** revisione cache/paint/adoption/trasporto e weak reference sulle
QImage adottate: a ogni campione immagini vive = entry contabilizzate, nessuna
entry ritirata ancora viva fuori dal paint, zero immagini/worker/crediti dopo
chiusura. Nessuna retention applicativa superflua trovata o rimossa. Il vecchio
harness M3 tratteneva screenshot per la verifica visiva: le sue misure storiche
non sono direttamente confrontabili con quelle di questo harness.

Ulteriore prova 128 MiB con **due attraversamenti consecutivi**: nel secondo,
57 raster e 133.966.872 byte residenti costanti; GUI da 211.443.712 a
211.906.560 byte, worker da 190.619.648 a 190.623.744 byte. La crescita iniziale
comprende il riempimento della cache Qt e la memoria del motore PDF; non emerge
un leak dal riuso delle stesse pagine. La precisa soglia/discesa da circa 600 MB
osservata sul PDF reale non è stata riprodotta o attribuita con certezza.
Nessun `gc.collect`, trim del working set, reset del motore o forzatura allocator.

Comandi riproducibili (PowerShell, runtime già installato):

```powershell
$env:QT_QPA_PLATFORM = 'windows'
& '.\.venv\Scripts\python.exe' -m tests.m3_budget_benchmark --compare
# Sostituire DIR con la directory BUDGET_COMPARISON stampata dal comando:
& '.\.venv\Scripts\python.exe' -m tests.m3_budget_benchmark --fixtures DIR --budget 128 --passes 2 --output DIR/retention-128
```

Evidenze locali: `.m0-artifacts/m3-budgets-k5u4o_8x/`, sei `run-*/report.json`,
`comparison.json`, `retention-128/report.json`, manifest e frame nativi.
La sola modifica al codice prodotto di questa rifinitura è `RESIDENT_BYTES`;
test e harness non introducono opzioni o diagnostica runtime per l'utente.

Verifiche della rifinitura: baseline **78 PASS in 107,95 s**; test mirati
`python -m pytest tests/test_raster_cache.py tests/test_virtual_viewer.py -q`
→ **32 PASS in 42,24 s**; suite `python -m pytest -q`
→ **80 PASS in 121,88 s**. Sei confronti nativi più il doppio attraversamento
a 128 MiB: PASS, zero risorse residue alla chiusura. `git diff --check`: PASS.
Restano preservate tutte le modifiche M3 preesistenti; nessun commit/push,
nessuna modifica normativa o dipendenza nuova, M4 non iniziata.

### Misure iniziali M3 (budget 256 MiB, storiche)

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_raster_cache.py tests/test_virtual_viewer.py -q
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' -m tests.m3_benchmark --display-list
$env:QT_QPA_PLATFORM = 'windows'
& '.\.venv\Scripts\python.exe' -m tests.m3_benchmark --native
```

Ogni benchmark crea una directory `.m0-artifacts/m3-*` con PDF sintetici da 60 e
300 pagine: 52 righe di testo per pagina, caratteri piccoli, immagine RGB e linee
sottili. La generazione avviene prima del GUI in un processo separato. Lo smoke
nativo esercita zoom/rotellina, scroll durante zoom, salto lontano, attraversamento
di tutte le 300 pagine, ritorno su pagine in cache, cambio tab e riattivazione.
Le otto catture sono diagnostiche, bounded, salvate dopo lo shutdown e non fanno
parte della cache raster del prodotto. Non vengono usati PDF privati dell'utente.
Gli input sono pilotati dal test mediante eventi Qt nella finestra nativa esposta;
i frame sono stati ispezionati visivamente. Non è una sessione con mouse fisico
condotta da una persona.

Hardware: **AMD Ryzen 5 230 / Radeon 760M, 6 core / 12 thread**, RAM rilevata
16.405.442.560 byte (circa 15,28 GiB); Windows build 26200, backend Qt **windows**,
PySide6 6.11.2, Python 3.11.9, PyMuPDF 1.28.2. DPR nativo **1,25**; DPR 2 è
coperto anche da test controllati, con rotazione/CropBox e zoom fino a 800%.

Run nativo con rotellina del 28 agosto 2026, `.m0-artifacts/m3-atm052f2/report.json`:

- richiesta → raster visibile sul lungo: P50 **10,48 ms**, P95 **14,54 ms**,
  massimo **20,79 ms**; include trasporto e adozione;
- viewport → raster pronto: P95 **16,62 ms**; dopo zoom, la convergenza include
  il debounce (fino a circa **151 ms** nel campione del documento medio);
- timer Qt a 10 ms: P95 **11,41 ms**, massimo **17,03 ms**;
- 318 render richiesti/completati/adottati; zero obsoleti nel percorso nativo
  già coalescente. I test con renderer rallentato verificano scarti effettivi;
- cache **33 hit / 617 miss** nei lookup per target/epoch (non nel paint),
  189 eviction; i miss possono includere prefetch poi annullato prima dell'invio;
- picco residente **267.933.744 byte / 114 immagini**, circa **255,52 MiB**;
  plateau dopo circa 114 pagine, non 300 raster residenti;
- transito: picco riservato **16 MiB** nel run, tetto **32 MiB** provato dai test;
- nel primo run, working set GUI circa **365,5 MiB** al plateau (include anche
  catture/test/Qt), worker circa **49–52 MiB**. Da pagina 150 a 299 la memoria
  raster rimane costante; al cambio tab scende all'intorno del nuovo documento;
- zero worker figli e zero crediti raster/transito dopo la chiusura.

I frame nativi mostrano testo più nitido dopo la preview, immagini/linee corrette,
e a 800% il previsto compromesso di nitidezza imposto dal cap. Non è una prova
di prestazioni universali su qualsiasi PDF o una verifica Acrobat.

Conferma sul codice finale, `.m0-artifacts/m3-_i7zgqtv/report.json`: PASS; sul lungo
richiesta → raster P50 **10,78 ms**, P95 **12,05 ms**, massimo **20,16 ms**;
viewport → pronto P95 **20,99 ms**. Timer P95 **11,39 ms**, massimo **29,54 ms**,
nessun intervallo oltre 40 ms. Geometria dello zoom completa sotto **0,37 ms**
nel campione medio; convergenza con debounce entro **163 ms**. Conteggi cache,
render e picchi raster invariati. Working set GUI al plateau circa **365,7 MiB**,
worker circa **48,6 / 52,2 MiB**; dopo riattivazione restano due raster,
4.700.592 byte. Zero worker/crediti dopo shutdown.

Verifiche finali: test mirati cache/viewer **30 PASS in 41,37 s**; suite completa
**78 PASS in 86,04 s** (baseline prima delle modifiche: **63 PASS in 91,34 s**).
`python -m pdf_annotator --smoke` con backend offscreen: exit 0, due worker chiusi;
`python -m pip check`: nessuna dipendenza incoerente. `git diff --check`: PASS.
Documenti normativi e dipendenze invariati; nessun commit/push.

### DisplayList e indagine sui picchi M2

Il confronto alternato su sei pagine dense, tre scale e otto ripetizioni utili
per campione ha misurato un risparmio mediano DisplayList di **0,67 ms (18,8%)**.
Report: `.m0-artifacts/m3-op7i43x7/report.json`. Il guadagno assoluto è piccolo
rispetto ai circa 10–15 ms di latenza completa già ottenuta; non giustifica qui
un nuovo ciclo di vita/cache worker. Il renderer continua con `annots=False`,
senza DisplayList, TextPage, shared memory, render pool o tiling.

La baseline M2 è stata ripetuta prima delle modifiche: timer P95 16 ms, massimo
32 ms, senza riprodurre il precedente picco di 516 ms (`m2-native-agua1cob`).
Il suo clock `monotonic` usa qui GetTickCount64 con risoluzione 15,625 ms: questo
spiega la quantizzazione delle vecchie misure, **non** il picco da 516 ms.
M3 misura con QueryPerformanceCounter (`perf_counter`, risoluzione 100 ns).

Sono separati timer, scheduler, adozione, paint, callback del test, campionamento
memoria, GC e heartbeat di un thread indipendente. Le tracce bounded dei picchi
includono CPU del GUI e span recenti. Il primo run M3 ha avuto un intervallo
isolato di 65,3 ms, non attribuito con certezza; le ripetizioni hanno massimi
15,0 e 17,0 ms. GC sotto 1 ms, salto in loop di 241 pagine circa 3 ms, paint sotto
6 ms nei run osservati. Non risultano freeze sostenuti riproducibili; nessuna
causa app dimostrata richiede una modifica di architettura. La causa del vecchio
picco resta non determinata, non viene dichiarata risolta né attribuita all'OS.

### Scope e acceptance M3

Scroll senza Ctrl invariato: la sensazione a scatti della rotellina resta alla
rifinitura M17. Non sono introdotti altri layout, annotazioni o relativo service,
selezione, ricerca, sidebar, undo/redo, Save/Save As o UI definitiva.

AT-VIEW-06 e le parti M3 di AT-PERF-01..09/11..13 sono verificate; AT-DOC-05/08
restano coperte. AT-DOC-06, AT-VIEW-04/07 e AT-PERF-10 sono **PARTIAL** rispetto
all'intera v1: isolamento posizione/zoom, geometria e reattività sono provati,
ma altri layout, annotazioni e ricerca restano nelle rispettive milestone.
Nessuna nuova dichiarazione di compatibilità Acrobat/InDesign.

API consultate: [QWheelEvent](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QWheelEvent.html),
[QImage](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QImage.html),
[PyMuPDF DisplayList](https://pymupdf.readthedocs.io/en/latest/displaylist.html).

## Evidenze M2 — Viewer continuo virtualizzato (storico)

Un `PdfView` (`QAbstractScrollArea`) per tab conserva posizione e scrollbar;
non viene ricreato cambiando documento. `SingleLayout` contiene solo rettangoli
e indici verticali: nessun widget o raster per ogni pagina. La ricerca delle
pagine visibili usa gli estremi verticali ordinati; il paint disegna solo quelle
che intersecano il viewport. Margine 20 e intervallo 16 pixel logici.

La scala base è **1 pixel logico Qt per punto PDF**. Non è un fit automatico.
Rotazione e CropBox riusano il contratto M0; il DPR cambia soltanto la densità
raster. Geometria, altezza totale e posizione di scroll non dipendono dai pixel
ricevuti. Sono disponibili scroll verticale continuo, scrollbar, Page Up/Down
Qt e scroll orizzontale quando la pagina è più larga del viewport.

### Richieste, priorità e durata dei raster

- L'apertura del solo `DocumentManager` continua a produrre **zero raster**.
  Solo l'attivazione di un viewer avvia rendering.
- Visibili = intersezione effettiva con il viewport; nearby = una pagina subito
  dopo e una prima, configurabili da 0 a 4 per lato nel costruttore del viewer.
- Visibili prima delle nearby. Nessun altro lavoro speculativo, nessuna LRU,
  DisplayList, cache worker applicativa o coda di tutti i punti attraversati.
- C'è al massimo **un ticket render per viewer**. A ogni tick si seleziona dal
  viewport corrente; un rifiuto per backpressure si riprova senza attendere.
- Identità: documento, generazione, request id, pagina, scala, DPR ed epoch.
  Cambiamento dell'insieme richiesto, DPR, generazione o scheda invalida il
  ticket. Il lavoro già in MuPDF termina, ma il risultato obsoleto viene scartato.
  Quello ancora in coda viene saltato dal trasporto M0 condiviso.
- I raster fuori dall'intorno vengono rilasciati; disattivazione/chiusura/failure
  rilasciano tutti i raster della tab. Il worker rimane vivo durante inattività.
  Una tab inattiva drena soltanto eventuali risposte cancellate, senza nuovi render.

Percorso: viewer → scheduler → `WorkerClient` → helper IPC → worker proprietario
→ `render_page` → RGB888 → `QImage.copy()` → paint del viewport. Il manager
notifica tick READY con lavoro limitato; l'apertura mantiene il proprio consumer
IPC fino a READY. Nessuna chiamata PyMuPDF o send/recv/join nel thread GUI.

### Limiti minimi M2

| Risorsa | Limite |
| --- | --- |
| Singolo raster prodotto | 16 MiB, inclusa stima del padding RGB di Qt |
| Payload logici riservati in transito, globali | 32 MiB / 2 slot |
| QImage residenti, globali | 64 MiB |
| Metadati per messaggio / richieste per client | 64 KiB / 4, invariati |

Il profilo predefinito dello spike M0 resta 4 MiB singoli / 8 MiB in transito;
protocollo e renderer sono gli stessi, con limiti configurati e validati.
Una pagina 600×800 a DPR 2 ottiene 1200×1600 RGB senza la degradazione imposta
dal vecchio cap M0. Per raster oversized si riduce la risoluzione prima
dell'allocazione, mantenendo inalterate le dimensioni logiche della pagina.

La quota residente viene ripartita conservativamente sull'intorno corrente.
Le sole immagini conservate dal paint appartengono allo scheduler, senza copie
QPixmap aggiuntive. I byte residenti usano `bytesPerLine × height`; il credito è
prenotato prima della copia Qt. I limiti **non equivalgono alla memoria totale dei
processi**: pixmap MuPDF, frame serializzati, copia ricevuta e QImage possono
coesistere. Queste copie sono limitate per dimensione e numero; working set e
cache interne del motore richiedono le misure rappresentative di M3.

### Verifica M2

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_virtual_viewer.py -q
& '.\.venv\Scripts\python.exe' -m pytest -q
$env:QT_QPA_PLATFORM = 'windows'
& '.\.venv\Scripts\python.exe' -m tests.m2_helpers
```

Lo smoke nativo usa l'entry point normale, genera esclusivamente fixture locali,
inserisce 250 ms di ritardo per render nel solo worker di test e salva sette
frame e `report.json` in una nuova directory ignorata `.m0-artifacts/m2-native-*`.
Le immagini vengono scritte dopo lo shutdown; non fanno parte della retention
del viewer. Sono osservabili placeholder, pagine reali, scroll, salto alla pagina
251, seconda tab e ritorno alla posizione precedente, senza riavviare i worker.

Run conclusivo del 28 agosto 2026: **63 passed in 113,33 s** (24 M0 + 23 M1 +
16 M2); suite mirata viewer **16 passed in 29,89 s**. Smoke dell'entry point
installato M0: exit 0; `pip check`: nessuna incompatibilità. Nessun nuovo pacchetto.

Smoke Windows nativo: exit 0, due worker distinti e zero figli dopo la chiusura.
Ispezionati i frame reali di placeholder, pagine 1/2/3, pagina 251 e cambio tab.
Picco residente del viewer **9.008.000 byte (~8,6 MiB)**; riserva in transito
massima osservata **16 MiB**. Richieste render: 12 sul documento lungo, 3 sull'altro,
incluso il ritorno alla scheda precedentemente svuotata. Il salto lontano termina
in circa 1,28 s nel test che aggiunge intenzionalmente 250 ms a ogni render.

Il timer Qt ha P95 circa 16 ms, ma picchi fino a **516 ms**. Le misure separate di
polling, scheduler, paint e acquisizione frame sono entro circa 16 ms nel run;
non è stata stabilita la causa dei picchi fra eventi. Lo smoke verifica il
funzionamento nativo, **non certifica la latenza su PDF rappresentativi**. Questa
misura e i working set restano da approfondire in M3. Report locale completo:
`.m0-artifacts/m2-native-3y61xbh7/report.json`.

I test M1 che pretendevano un QLabel senza raster sono aggiornati alla UI M2;
restano invariati i controlli metadata-only, identità, dirty veto e cleanup.
Il numero pagine ha un campo separato nella barra di stato per non coprire gli
errori di apertura quando viene ripristinata la tab precedente.

Non sono implementati zoom interattivo, altri layout, cache definitiva,
prefetch adattivo, ricerca, annotazioni, sidebar, undo/redo o salvataggio.
Il rendering base conserva `annots=False`: la visualizzazione/interazione delle
annotazioni preesistenti aspetta il loro layer, non viene simulata né appiattita.
Le verifiche Acrobat sono **NOT RUN** e non sono richieste per questo viewer base.
Geometrie oltre il range intero delle scrollbar Qt vengono rifiutate con errore
controllato e cleanup; non viene introdotto un secondo sistema di coordinate.

| Acceptance | Stato M2 |
| --- | --- |
| AT-DOC-01/02/03/05/08/10, AT-VIEW-01, AT-PERF-01/09/11 | PASS nel perimetro attuale |
| AT-PERF-04/06 | PASS nei test M2; zoom e qualificazione prestazionale finale esclusi |
| AT-DOC-06, AT-VIEW-04 | PARTIAL: posizione indipendente e layout singolo |
| AT-PERF-02/03/05/08/10/12/13 | PARTIAL: limiti/retention/IPC minimi; politiche e prove complete future |
| Zoom, altri layout, annotazioni, ricerca, undo, Save e Acrobat | NOT RUN |

API consultate: [Qt QAbstractScrollArea](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QAbstractScrollArea.html)
e [PyMuPDF Page](https://pymupdf.readthedocs.io/en/latest/page.html).

## Evidenze M1 — Lifecycle e sessioni (storico)

Avvio normale su Windows, senza modalità offscreen:

```powershell
$env:QT_QPA_PLATFORM = 'windows'
& '.\.venv\Scripts\python.exe' -m pdf_annotator
```

Ogni tab pronta mostra nome e numero di pagine. Non vengono mostrati raster né
placeholder per singole pagine. L'apertura usa un dialogo Qt asincrono, così i
timer di IPC restano serviti anche mentre si selezionano i file.

`DocumentManager` possiede le sessioni e l'identità delle fonti. Ogni
`DocumentSession` passa per `OPENING → READY → CLOSING → CLOSED`; un worker
interrotto dopo l'apertura porta a `FAILED`, conservando una tab identificabile e
chiudibile. Un'apertura fallita viene invece ripulita interamente: niente tab,
registrazioni duplicate o worker residui. La rimozione dal registro avviene solo
dopo la terminazione del worker. Aprire lo stesso file durante `CLOSING` non
crea un secondo proprietario; si può riprovare dopo la chiusura.

Lo stato UI è distinto per sessione: reading anchor, zoom, layout, search state
e sidebar hanno solo valori iniziali/contenitori. Il dirty plumbing usa pending,
confirmed e saved revision, inizialmente a zero. Il titolo deriva da `dirty`, ma
la chiusura controlla il modello: una sessione dirty simulata nei test può porre
il veto alla chiusura della tab o dell'app. Non esistono Save/Discard/Cancel fittizi,
mutazioni o undo/redo.

### Identità e apertura

- Un helper senza PyMuPDF esegue `Path.resolve(strict=True)` e `stat()` fuori dal
  thread GUI. Ci sono al massimo 16 richieste di identificazione pendenti.
- Si registrano percorso canonico, chiave normalizzata per case su Windows,
  dimensione, `mtime_ns` e `(st_dev, st_ino)` quando disponibile. La deduplicazione
  avviene prima di spawn e include le sessioni ancora in apertura.
- Sono provati stesso percorso, `.`/`..`, case variant Windows e hard link.
  Gli alias risolvibili seguono `resolve()`/identità file; non è presente un
  filesystem watcher. Provider senza identità affidabile restano limitati al
  percorso canonico; non si promette riconoscimento universale di alias remoti.
- Il worker ricontrolla la firma prima/dopo l'apertura. Un handle `rb` con `fstat`
  distingue gli errori di permesso/condivisione dai PDF invalidi e protegge il
  passaggio all'apertura PyMuPDF dalla sostituzione del percorso su Windows.
- Si rifiutano file non PDF, danneggiati/riparati e PDF cifrati anche con password
  utente vuota: `needs_pass` da solo non basta, si verifica anche `metadata.encryption`.
- Nessuna chiamata di salvataggio o modifica è disponibile nel worker M1. Gli
  spike M0 restano in un entry point worker separato e non sono abilitati sui PDF aperti.

### Metadati, IPC e cleanup

`OPEN_DOCUMENT` restituisce numero pagine/formato; `PAGE_GEOMETRY` restituisce
blocchi di **massimo 64 pagine**, mantenendo il limite M0 di **64 KiB** per i
metadati IPC. I DTO riusano `PageGeometry` con dimensioni canoniche non ruotate,
CropBox, MediaBox e rotation. Non cambiano mapper, limiti raster o pin M0.

Il test da 300 pagine misura **zero richieste render** e cinque richieste di
geometria. Un worker di test rende inoltre indisponibili le API per pixmap, testo,
TextPage, DisplayList, disegni e annotazioni: l'apertura riesce comunque.
Si leggono solo metadati del documento e geometrie delle pagine, senza parsing
applicativo dei content stream o inventario annotazioni.

Chiusura tab/app e rilevazione guasti riusano il trasporto asincrono M0. Nessun
send/recv/join viene aggiunto al GUI thread. Aperture in attesa vengono annullate
alla chiusura dell'app; i risultati tardivi non creano sessioni. Il bootstrap IPC
gestisce ora anche errori di creazione della pipe prima di spawn, necessari per
evitare sessioni bloccate durante un'apertura reale.

Gli errori UI sono brevi e senza traceback. Il log contiene identificatori e
tipi/codici di errore; i messaggi grezzi del parser MuPDF non vengono stampati.

### Verifica M1

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_document_lifecycle.py -q
& '.\.venv\Scripts\python.exe' -m pytest -q
```

Il gruppo M1 comprende 23 test, incluse protezione con password vuota, vera
negazione di lettura Windows, cambiamento della fonte durante open, saturazione
dell'helper, errore di bootstrap, apertura/chiusura concorrenti, tre tab, crash,
dirty veto e chiusura applicazione senza figli residui. Lo smoke dell'entry point
normale è incluso nei test; usa due fixture già create e il vero event loop Qt.
È stato eseguito anche con backend **Windows nativo**, verificando visivamente
font/tab/placeholder e uscita senza processi figli. Il backend offscreen locale
è adatto ai test logici ma qui non rende correttamente i font di sistema.

Run conclusivo M1 del 28 agosto 2026: **23 test M1 passati**; suite completa
**47 passed in 59,69 s** (24 M0 + 23 M1). `pip check` senza incompatibilità.
L'entry point installato con `--smoke` M0 termina ancora con exit 0. Lo smoke
dell'entry point normale M1, anche sul backend Windows nativo, termina con exit 0,
due worker distinti, zero render requests e zero processi figli dopo la chiusura.

| Acceptance | Stato M1 | Confine |
| --- | --- | --- |
| AT-DOC-01 | PARTIAL | Open, page count e geometrie; prima pagina visibile a M2 |
| AT-DOC-02 | PARTIAL | 300 pagine, zero rendering; visible/nearby a M2 |
| AT-DOC-03 | PASS | Errori controllati e cleanup; input invalido, cifrato e non leggibile |
| AT-DOC-05 | PASS nel perimetro M1 | Tab, identità, page count e switching senza reopen |
| AT-DOC-06 | PARTIAL | Contenitori isolati e dirty plumbing; le feature restano future |
| AT-DOC-08 | PARTIAL | Tre documenti, chiusura della tab centrale e risorse worker; cache non presente |
| AT-DOC-10 | PASS | Duplicati, percorsi equivalenti e hard link senza secondo worker |
| AT-PERF-11 | PASS nel perimetro M1 | Crash A confinato, B resta aperto/usabile, A chiudibile |

Restano fuori scope: viewer, rendering di prodotto, cache/prefetch, scroll, zoom,
layout, ricerca, sidebar, annotazioni, Save/Save As, monitoraggio conflitti,
undo/redo e packaging. Le firme sono preparazione per M12, non un controllo
continuo né una garanzia contro modifiche concorrenti esterne. I gate Acrobat
non sono stati eseguiti in M1. Nessun documento normativo è stato modificato.

Riferimenti verificati per M1:
[PyMuPDF Document](https://pymupdf.readthedocs.io/en/latest/document.html),
[Python 3.11 stat](https://docs.python.org/3.11/library/os.html#os.stat_result),
[Qt QFileDialog](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QFileDialog.html),
[PyMuPDF Tools](https://pymupdf.readthedocs.io/en/latest/tools.html).

## Evidenze riproducibili M0 (storico)

| Test | Evidenza M0 |
| --- | --- |
| `test_protocol.py` | DTO request/response, identità, errori, serializzazione e limiti dei frame |
| `test_ipc_raster.py` | Due processi Windows spawn, routing, risultati obsoleti, crash isolato, saturazione, shutdown occupato, heartbeat Qt, lifetime QImage, limite raster |
| `test_coordinates.py` | Otto pagine: 0/90/180/270 gradi, con e senza CropBox decentrata; attesi numerici indipendenti e landmark nel contenuto |
| `test_annotations_save.py` | FreeText/Callout nativi, testo/appearance/colori/geometria dopo riapertura, full-save e failure injection, vero lock Windows sulla destinazione |

Le fixture vengono generate dal worker proprietario nella directory temporanea
del test. La creazione esclusiva rifiuta percorsi già esistenti. Le due fixture
base hanno cinque pagine e marcatori distinti; la pagina grande misura
10.000 × 12.000 punti. Non sono inclusi PDF reali né fixture pesanti nel repository.

Run conclusivo di M0 del 28 agosto 2026, nell'ambiente sopra:

- `python -m pytest tests/test_annotations_save.py -q`: **8 passed**;
- `python -m pytest tests/test_protocol.py tests/test_coordinates.py tests/test_ipc_raster.py -q`: **16 passed**;
- `python -m pytest -q`: **24 passed**, 25,23 s;
- `python -m pip check`: nessun requisito incompatibile;
- `python -m pdf_annotator --smoke` e `pdf-annotator --smoke`, con backend Qt
  `offscreen`: **exit 0**, due worker chiusi. Nessun test visivo Acrobat eseguito.

Nei comandi sopra `python` indica `.venv/Scripts/python.exe` e l'entry point
installato è `.venv/Scripts/pdf-annotator.exe`.

### Processi e trasporto

Ogni `WorkerClient` possiede un processo persistente dedicato a un solo documento
e un helper di trasporto che non importa PyMuPDF. Solo il thread principale del
worker usa il motore. L'unica eccezione è il validator del candidato temporaneo.
`freeze_support()` e gli entry point protetti evitano bootstrap ricorsivi.

L'IPC applicativo usa JSON tipizzato nell'envelope e byte RGB, senza pickle di
oggetti applicativi. Limiti dello spike:

- metadati: **64 KiB** per frame;
- raster singolo: **4 MiB**, controllato prima dell'allocazione e alla ricezione;
- payload raster ammessi: **due**, con **8 MiB** di riserva globale condivisa;
- richieste pendenti: **quattro per worker**, comprese risposte non consumate.

Ogni raster riserva conservativamente 4 MiB prima dell'invio; `submit()` restituisce
`None` in caso di saturazione. La riserva dura fino a scarto/rilascio/adoption,
anche se il worker è già chiuso. Il protocollo valida anche stride e lunghezza.
L'identità comprende documento, generazione e request id; l'ultima richiesta
raster rende obsolete quelle precedenti.

Gli 8 MiB sono il budget dei **payload logici**, non l'RSS totale: pixmap del
worker, serializzazione, buffer ricevuto e copia Qt possono coesistere. Le copie
sono limitate dai due slot e dal limite singolo. Il raster Qt adottato diventa
memoria residente del consumatore; M0 non implementa una cache residente. I test
ne misurano `sizeInBytes()` e rilasciano esplicitamente le immagini. Il budget
cache/scene di produzione e le misure RSS appartengono a M3.

Spawn, send, recv e join avvengono nell'helper. La chiusura scarta il lavoro
accodato, attende con deadline e forza la terminazione solo se necessario; i test
coprono anche un worker occupato e un raster non adottato, mantenendo attivo Qt.
Un crash viene esposto come errore serializzato e non riavvia automaticamente il
documento. Non esistono ancora tab, dialoghi di errore o politiche dirty di prodotto.

### Coordinate e raster

Coordinate persistenti in punti PyMuPDF non ruotati, locali alla CropBox visibile:
origine in alto a sinistra, X a destra, Y in basso. CropBox e MediaBox restano
metadati distinti. Non si riapplica l'offset della CropBox a coordinate già
restituite dalle API di pagina. Rotazione, posizione nella scena e zoom sono
trasformazioni separate; il DPR entra soltanto nella scala del raster.

`Page.get_pixmap(colorspace=csRGB, alpha=False, annots=False)` produce RGB888.
Una richiesta eccessiva riduce la scala raster prima dell'allocazione, conservando
zoom geometrico e DPR nel DTO. Qt usa `QImage(...).copy()` prima di liberare il
payload. Non ci sono cache, prefetch, DisplayList o file immagine intermedi.

### FreeText e Callout: percorso verificato in PyMuPDF 1.28.2

Entrambi usano `Page.add_freetext_annot()` con `richtext=True`, markup fisso
generato con `html.escape`, stile fisso nero, riempimento giallo pallido e bordo
blu indipendente. Il testo comprende accenti, newline, virgolette, `<` e `&`.
Non esiste un editor rich text.

Particolarità osservate e coperte dai test:

- la creazione rich text non applica il colore richiesto al bordo tramite `/DA`:
  si imposta `/DA` con `Document.xref_set_key()` e `get_pdf_str()`, poi `Annot.update()`;
- `set_info(content=...)` rimuove `/RC`: il plain text esatto viene scritto solo
  in `/Contents` con la stessa API pubblica, preservando `/RC` e `/DS`;
- il motore inizializza `/CL` anche senza callout: si elimina quella chiave dalla
  nuova Text Box ordinaria prima di rigenerarne l'appearance;
- `Annot.get_pixmap()` ha restituito RGBA/colori convertiti anche chiedendo RGB
  senza alpha: il controllo visivo automatico usa il raster RGB della pagina,
  ritagliato sull'annotazione, insieme agli operatori del suo appearance stream.

Queste operazioni riguardano **solo annotazioni nuove dello spike**, mai annotazioni
esterne. Il Callout passa tre punti a `callout=` e `PDF_ANNOT_LE_OPEN_ARROW` a
`line_end=`. Sono verificati `/Subtype /FreeText`, `/IT /FreeTextCallout`, `/CL`,
`/LE`, geometria, testo esatto, riempimento, bordo/linea blu e testo nero dopo
salvataggio e riapertura. I content stream originari delle pagine restano identici.
Non c'è flattening. Le legature nell'estrazione dell'appearance sono confrontate
con NFKC, senza normalizzare il testo persistito in `/Contents`.

La fixture testuale fissa entra nel rettangolo. Overflow generale, modifica,
ridimensionamento, font/testi arbitrari e riconoscimento delle annotazioni esterne
restano a M7/M8/M9. **La riapertura PyMuPDF non prova l'editabilità in Acrobat.**

### Full-save sacrificabile

Il worker salva un candidato sibling con `incremental=False, garbage=0,
clean=False`. Il validator one-shot legge il file tramite `rb`, chiude l'handle,
apre i byte in PyMuPDF senza mutarli e verifica apertura senza riparazione,
numero di pagine e inventario minimo delle annotazioni. Il suo limite di lettura
è **16 MiB**, intenzionalmente limitato alle fixture M0.

Solo dopo l'uscita del validator il proprietario chiude il documento live,
esegue `os.replace()` e riapre il target, aggiornando generazione e saved revision.
Gli errori in scrittura, candidato invalido e validator terminato lasciano
l'originale byte-identico e il documento modificato ancora aperto. Un errore di
replace conserva il candidato validato e lo riapre come backing recuperabile,
mantenendo dirty. Quest'ultimo caso è provato sia per injection sia tenendo un
vero handle Windows aperto sul target.

Non è il servizio Save di produzione: non include incremental save, Save As,
firme del file/conflitti esterni, undo/clean point, crash-durability del filesystem,
recovery completo da errore dell'ultima riapertura o chiusura forzata durante il
validator. Questi casi richiedono gli acceptance gate successivi, soprattutto M12.

## Acceptance e limiti M0 (storico)

M0 verifica gli spike sopra. I test tecnici di routing, isolamento failure,
raster oversized e IPC occupato corrispondono alle parti M0 di AT-PERF-04/11/12/13.
Le parti non interattive di AT-ANN-03/08 e AT-SAVE-05 sono coperte; le prime prove
numeriche supportano AT-COORD-01/02. **Nessuno di questi risultati dichiara completa
la procedura di prodotto** quando questa richiede viewer, tab, gesture, overflow,
recovery finale o annotazioni sulle pagine ruotate.

AT-DOC-05/08 completi, AT-ANN-03/08 completi, AT-COORD-01/02 completi,
AT-SAVE-05 completo e tutti i gate Acrobat restano **NOT RUN**. Non sono presenti
UI definitiva, ricerca, Highlight/Ink interattivi, toolbar, sidebar, undo/redo,
cache/prefetch di produzione o packaging Windows.

Riferimenti API ufficiali consultati: [PyMuPDF Page](https://pymupdf.readthedocs.io/en/latest/page.html),
[Annot](https://pymupdf.readthedocs.io/en/latest/annot.html),
[Document](https://pymupdf.readthedocs.io/en/latest/document.html),
[multiprocessing PyMuPDF](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html),
[Python 3.11 multiprocessing](https://docs.python.org/3.11/library/multiprocessing.html),
[Qt QImage](https://doc.qt.io/qt-6/qimage.html).
