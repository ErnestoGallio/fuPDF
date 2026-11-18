# fuPDF M21.2 — Snapshot e batch offline

## Contratto operativo

- `REVIEW_SNAPSHOT`, con payload vuoto lato client, entra nella FIFO dello stesso `WorkerClient` delle mutazioni. Il dispatcher fornisce la revisione al momento dell'esecuzione. Le mutazioni già accodate precedono la cattura e le successive seguono. L'operazione restituisce `snapshot` con ID, documento, generazione, revisione, conteggio pagine, SHA-256, byte e percorso temporaneo. Il worker confronta inoltre l'identità dell'origine prima e dopo la scrittura. La cattura non passa per Save e non aggiorna i suoi checkpoint.
- `plan_batches(snapshot, first=0, last=None)` produce il manifesto iniziale senza I/O su PDF. `first` e `last` sono inclusivi e zero-based. Ogni assegnazione contiene `target_pages`, `context_pages`, `batch_id` e una `page_map` con indici fisici e posizioni zero-based nell'estratto. `validate_correction_pages` rifiuta qualsiasi pagina estranea ai target, anche quando presente come contesto.
- `REVIEW_EXTRACT` prende `snapshot_id`, `first_page`, `last_page` e, opzionalmente, `max_bytes` entro 20 MiB. Accetta un gruppo di massimo quattro target. Restituisce il relativo manifesto adattato, con percorsi, byte e SHA-256 per gli estratti pronti e `unprocessable_pages` per quelli non elaborabili. Un batch `too_large` non ha file pronto. Il manifesto iniziale copre l'intera richiesta. Ogni manifesto materializzato sostituisce la relativa assegnazione, mantenendone l'intervallo e la copertura. La scelta del gruppo successivo resta al chiamante futuro.
- `REVIEW_RELEASE`, con `snapshot_id`, rilascia lo snapshot e gli estratti residui. Il trasporto crea la directory privata al primo uso e la rimuove alla chiusura del worker. È disponibile un solo snapshot per worker. I byte PDF restano su disco e l'IPC porta soltanto metadati. Il client ammette un'operazione di revisione alla volta e una nuova estrazione solo con coda libera.

### Durata degli estratti da M21.2a

- Ogni batch `ready` ha un `extract_id` opaco e un percorso univoco per materializzazione, oltre a `batch_id`, dimensione e SHA-256. Ripetere l'estrazione mantiene il `batch_id` logico e produce un nuovo `extract_id`. I file già pubblicati restano disponibili e il worker non li riscrive. Un batch `too_large` ha `extract_id` e percorso nulli.
- Il worker possiede i file e il chiamante li apre in sola lettura. Una volta terminate le letture di A, `REVIEW_RELEASE_EXTRACT` con `snapshot_id` ed `extract_id` elimina A senza interferire con B. La risposta include `released: true` alla prima rimozione e `released: false` per un ID già rilasciato o assente nello snapshot attivo. Uno snapshot sconosciuto o scaduto produce `REVIEW_SNAPSHOT_UNKNOWN`.
- Il worker pubblica il manifesto dopo la scrittura e l'hash di tutti gli estratti pronti del gruppo. Se la materializzazione fallisce, rimuove solo i file della richiesta fallita. Un errore di rimozione restituisce `REVIEW_CLEANUP_FAILED` e il worker conserva i percorsi residui per il rilascio dello snapshot. Un rilascio fallito può essere riprovato. Il trasporto rimuove la directory alla chiusura regolare e tenta la stessa pulizia se deve terminare il processo worker.
- Il futuro scheduler chiude i lettori prima del rilascio o della chiusura della scheda, soprattutto su Windows, dove un handle aperto può impedire la rimozione. Rilascia ogni estratto appena finisce di usarlo e lo snapshot a fine job. Questa responsabilità limita l'occupazione su disco. Un gruppo adattato può produrre fino a quattro estratti pronti. Il limite di due richieste in volo compete allo scheduler.

## Scrittura e adattamento

Con PyMuPDF 1.28.2 il wrapper `Document.save()` chiama `JM_embedded_clean()` sul catalogo del documento vivo. Su una fixture con allegato ha cambiato `/PageMode` da `/UseNone` a `/UseAttachments`. La cattura usa il writer MuPDF sottostante con ID e metadati conservati, senza la pulizia del wrapper. L'accesso al binding è isolato in `write_snapshot()`. La variante ha conservato catalogo, xref, dirty e possibilità di scrittura incrementale nella prova preliminare. I test di integrazione verificano anche creazione, spostamento e cancellazione non salvati, mutazioni prima e dopo la barriera, Undo/Redo, byte dell'origine e riuso dello snapshot attraverso Save As.

Per l'estrazione, `insert_pdf()` del percorso M20 ha eliminato popup, motivazione e StrikeOut raggruppato in una prova sul Replace nativo. M21.2 apre una sola istanza temporanea dallo snapshot, seleziona le pagine nella sua copia in memoria e salva su un file separato. Così rimangono i legami delle annotazioni interne all'estratto. Una risposta con padre fuori dall'estratto conserva il testo della nota e perde il riferimento al padre escluso. Lo snapshot conserva il grafo originale. Questo è il motivo tecnico per cui l'estrazione M20 non viene riusata. Il test offline verifica che il builder M20.1 incapsuli gli stessi byte dell'estratto come `input_file` inline. Formato, prompt, profili e indici originali restano quelli di M20.

Per ogni gruppo si misura prima il file serializzato con entrambi i contesti disponibili, poi si prova il contesto precedente da solo, poi il successivo da solo, infine i soli target. Le combinazioni identiche vengono saltate. Solo se i target senza contesto superano il limite si prova con gruppi da due e poi da una pagina, ricalcolando le vicine per ogni figlio. Dopo ogni prova troppo grande si elimina il temporaneo. L'esito di una pagina ancora troppo grande è esplicito. La misura usa 20 × 1024² byte PDF, prima della codifica Base64.

## Riproduzione

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=short tests/test_review_batches.py tests/test_review_snapshot.py `
  tests/test_protocol.py tests/test_save_m15.py::test_save_checkpoint_generation_and_undo_redo `
  experiments/m20/test_review.py
git diff --check
.\.venv\Scripts\python.exe -m experiments.m21_snapshot_benchmark --repeats 5 --sample 'C:\percorso\campione.pdf'
```

`--sample` è facoltativo. Il benchmark genera due PDF deterministici, avvia il vero worker, fa warm-up e misura baseline e richieste accodate immediatamente dopo cattura ed estrazione. Misura wall time e working set Win32 del processo. I PDF sintetici e i temporanei vengono eliminati al termine. Il tempo dell'indice testuale caldo include anche il render deliberatamente in coda prima del testo. Il p95 su cinque prove è il massimo del campione e non una stima affidabile delle code su altre macchine.

## Misure locali su Windows

Cinque ripetizioni per documento. Tutti i tempi sono in millisecondi. Il picco di memoria è il working set dell'intero processo worker durante la sua vita, in MiB. I risultati includono l'I/O temporaneo e il carico effettivo della macchina durante la prova.

| PDF | Pagine | Origine (MiB) | Serializzazione mediana | SHA-256 mediano¹ | Estratto mediano | Picco worker (MiB) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Sintetico testuale | 48 | 0,019 | 2,2 | 10,2 | 4,7 | 54,3 |
| Sintetico con immagini | 96 | 175,85 | 255,0 | 136,6 | 255,8 | 60,7 |
| Campione reale M20 | 19 | 2,47 | 11,5 | 16,5 | 25,3 | 69,0 |

¹ Include rilettura del file e controlli d'identità dopo la scrittura.

| PDF | Render p95 a riposo / dopo cattura / dopo estratto | Indice testuale caldo p95 a riposo / dopo cattura e render / dopo estratto e render |
| --- | ---: | ---: |
| Sintetico testuale | 2,6 / 22,3 / 15,0 | 2,6 / 22,3 / 14,9 |
| Sintetico con immagini | 5,0 / 477,7 / 269,6 | 2,5 / 479,8 / 271,4 |
| Campione reale M20 | 39,5 / 74,0 / 72,3 | 2,5 / 74,2 / 73,9 |

Sul PDF con immagini, il massimo osservato dell'indice testuale in coda è circa 480 ms dietro allo snapshot. Il render ha raggiunto 270 ms dietro un estratto pronto al primo tentativo. Con più tentativi adattivi lo stesso comando può occupare il worker più a lungo. Il benchmark tiene attivo un solo documento.

## Verifiche e prerequisiti per M21.3

- Esito della selezione `pytest` riportata sopra: 87 test passati in 20,85 s. `git diff --check` e il controllo degli spazi finali nei nuovi file M21.2 sono passati. Suite completa non eseguita su richiesta.
- Dopo M21.2a, la stessa selezione ha dato 90 test passati in 26,45 s. La prova con due lettori verifica rilascio indipendente, hash, modifiche successive al PDF vivo, pulizia alla chiusura e rilascio dello snapshot con più estratti. La simulazione di errore verifica la rimozione dei file parziali, la conservazione degli estratti precedenti e il nuovo tentativo di rilascio dopo un errore di accesso.
- Le regressioni automatiche coprono il checkpoint di Save e Save As, incluso il riuso dello snapshot dopo il cambio di generazione. Sulla fixture con allegato e `/UseOutlines`, un tentativo con il percorso Save As esistente è stato respinto dal suo validatore per il cambio di catalogo. Quel percorso resta fuori dal perimetro di M21.2.
- `AT-DOC-04`, `AT-SAVE-02`, `AT-PERF-05` e `AT-PERF-10` hanno copertura mirata del worker o misure su un singolo documento. I collaudi manuali completi, compreso quello multi-documento, sono da eseguire prima dell'integrazione interattiva. Le verifiche Adobe restano quelle del collaudo M20.
- M21.2a conserva SHA-256 nel worker. Spostarlo richiederebbe coordinare la pubblicazione, la verifica d'identità e la pulizia con una nuova attività in volo. Le misure M21.2 restano il riferimento. La gestione degli estratti aggiunge un aggiornamento del registro dopo la scrittura e l'hash e sposta la rimozione al rilascio. Un'estrazione con più tentativi continua a occupare la coda del worker fino al termine.
- Prima di usare gli estratti fuori dal processo, lo scheduler deve rispettare la durata dei file e usare `page_map` per inviare e validare gli indici fisici originali. Le esclusioni dal manifesto contano nella copertura e vanno riportate come pagine non elaborabili. Gli estratti includono di proposito le annotazioni delle pagine selezionate.
