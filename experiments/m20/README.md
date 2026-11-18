# fuPDF M20 — Revisione sperimentale di un PDF

La pipeline sperimentale usa tre CLI. `review` invia l'intervallo PDF selezionato alla Responses API e salva le proposte in `corrections.json`. [`localize`](LOCALIZATION.md) usa il PDF originale e le proposte per generare un rapporto con esiti e geometrie. [`apply`](APPLY.md) riconferma i bersagli nel worker fuPDF e salva le annotazioni native in una copia del PDF. I passaggi di localizzazione e applicazione funzionano offline. La pipeline è disponibile da riga di comando e la GUI di fuPDF continua a gestire la visualizzazione e l'editing dei PDF.

CLI autonoma per valutare la qualità delle proposte editoriali e il costo di una singola richiesta. L'esperimento usa PyMuPDF 1.28.2 per copiare in memoria le pagine selezionate. L'originale viene letto in sola lettura. L'estratto conserva i contenuti delle pagine, comprese le eventuali annotazioni e i widget copiati da `insert_pdf`. Link e strutture a livello di documento, come segnalibri e livelli opzionali, possono essere assenti nell'estratto. [Documentazione PyMuPDF](https://pymupdf.readthedocs.io/en/latest/document.html#Document.insert_pdf)

La CLI serializza l'estratto come `input_file` con `file_data` Base64 per `POST /v1/responses`. OpenAI fornisce al modello il testo estratto dal servizio e le immagini delle pagine. Il modello predefinito è `gpt-6.1-sol`, reasoning `medium` e tier Standard. Il JSON usa `text.format` con `type: json_schema` e `strict: true`. Il trasporto HTTPS usa la libreria standard di Python 3.11. L'SDK OpenAI è assente nella `.venv` del progetto. [Input PDF](https://developers.openai.com/api/docs/guides/file-inputs) · [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) · [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create) · [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

## Esecuzione da PowerShell

Dalla radice del repository, sostituisci il percorso con il PDF scelto. `--pages` usa gli indici fisici originali, da zero, con entrambi gli estremi inclusi. `12-14` seleziona tre pagine, dalla tredicesima alla quindicesima. Il limite predefinito è quattro pagine e si può cambiare con `--max-pages`.

```powershell
.\.venv\Scripts\python.exe -m experiments.m20.review 'C:\percorso\libro.pdf' --pages 12-14 --dry-run
```

Il dry-run è la modalità predefinita. Esegue in locale il ritaglio, i controlli di intervallo e dimensione e la serializzazione completa della richiesta. La verifica remota del modello e dello schema richiede la prima esecuzione reale.

### Contesto editoriale facoltativo (M20.4)

Passa `--context-file` seguito dal percorso di un oggetto JSON UTF-8. I soli campi ammessi sono `opera`, `destinatari` e `indicazioni_aggiuntive`. Tutti sono facoltativi e indipendenti e accettano stringhe, anche multilinea tramite `\n`. I campi assenti, vuoti o di soli spazi vengono omessi individualmente. Se l'oggetto risultante è vuoto, la richiesta contiene il prompt generale, la mappa e il PDF. La CLI verifica il file prima di preparare l'invio e indica eventuali errori in `run_metrics.json`. [Esempio modificabile](example_editorial_context.json)

La richiesta contiene il [prompt generale integrale](../../src/pdf_annotator/services/review_contract/prompt.txt) in `instructions`, seguito nel messaggio `user` dal contesto JSON (quando presente), dalla mappa delle pagine fisiche e dall'allegato PDF. Il prompt tratta contesto e PDF come dati. `text.format` continua a imporre lo stesso JSON delle correzioni.

`run_metrics.json` conserva `prompt_sha256` come SHA-256 UTF-8 delle sole istruzioni generali, include `editorial_context` con i valori usati e aggiunge `effective_prompt_sha256`. Quest'ultimo è calcolato sul JSON canonico UTF-8 di `instructions` e `input` con i soli blocchi testuali (`ensure_ascii=False`, `sort_keys=True`, separatori `,` e `:` senza spazi). Esclude i byte PDF e i parametri di trasporto. Il PDF e l'intervallo sono tracciati dai campi esistenti. Il contesto viene salvato in chiaro negli output locali esclusi da Git e inviato a OpenAI con `--execute`.

### Profili di revisione (M20.5)

`--prompt-profile schoolbook` seleziona Libri scolastici, il profilo predefinito. Puoi usare l'opzione con o senza `--context-file`. Un ID sconosciuto è un errore CLI. `run_metrics.json` registra l'ID in `prompt_profile` insieme ai campi di tracciabilità di M20.4.

Per aggiungere un profilo, crea il suo file di istruzioni in UTF-8 e registra una nuova voce in [`prompt_profiles.py`](prompt_profiles.py) con un ID stabile come chiave, un nome leggibile e il percorso del file. Conserva nel testo le istruzioni necessarie per citazioni letterali, contesti e ancore localizzabili. La CLI applica a tutti i profili la stessa mappa delle pagine, lo stesso schema Structured Outputs e la stessa validazione locale.

Il profilo seleziona le istruzioni editoriali. L'invio del documento usa un PDF per tutti i profili. Una futura modalità basata su testo estratto sarebbe una scelta distinta.

### Secondo esperimento

Prima dell'esecuzione, adatta l'esempio a titolo, anno di corso e prerequisiti effettivi. Con `--dry-run` puoi controllare preparazione e metadati. Per l'invio a pagamento, dalla radice del repository con `OPENAI_API_KEY` già impostata nella sessione:

```powershell
.\.venv\Scripts\python.exe -m experiments.m20.review 'C:\percorso\libro.pdf' `
  --pages 4-6 --model gpt-6.1-sol --reasoning medium `
  --context-file 'experiments\m20\example_editorial_context.json' --execute
```

Confronta `corrections.json`, `run_metrics.json` e `report.md` del nuovo run con gli omonimi in `output/m20/run-mk8fcguu/`. Abbina le proposte per pagina, bersaglio e problema. Segna separatamente precisazioni superflue eliminate, errori sostanziali mantenuti o persi, nuovi errori tecnici fondati e qualità delle motivazioni. Confronta numero di proposte, token e costo stimato. Il confronto misura l'effetto congiunto del prompt rivisto e del contesto. Una singola coppia di esecuzioni non isola il contributo dei due cambiamenti né la variabilità del modello.

Per una richiesta reale, imposta `OPENAI_API_KEY` nell'ambiente della sessione PowerShell e avvia esplicitamente il comando seguente. Ogni invocazione con `--execute` autorizza un tentativo a pagamento. Il trasporto effettua un solo POST, con timeout socket di 180 secondi e zero retry automatici. In caso di timeout il servizio potrebbe aver già elaborato la richiesta. `--model`, `--reasoning`, `--max-output-tokens` (8192 di default) e `--timeout` sono configurabili.

```powershell
.\.venv\Scripts\python.exe -m experiments.m20.review 'C:\percorso\libro.pdf' --pages 12-14 --execute
```

## Contratto JSON e prompt

La radice contiene un array `corrections`. Tutte le chiavi sono obbligatorie e gli oggetti rifiutano chiavi aggiuntive. `page_index` è un intero entro l'intervallo originale selezionato. La richiesta include la mappa tra posizione nell'estratto e indice originale. `context_before` e `context_after` sono stringhe letterali o `null`. `reason` è una stringa motivata. `category` ammette `orthography`, `grammar`, `syntax`, `terminology`, `technical`, `clarity` e `verification_needed`. Quest'ultima categoria richiede un `comment`.

| `operation` | `original_text` | `replacement_text` | `insert_anchor` |
| --- | --- | --- | --- |
| `replace` | Testo letterale | Sostituzione | `null` |
| `delete` | Testo letterale | `null` | `null` |
| `insert` | `null` | Testo da inserire | `{ "text": "ancora letterale", "side": "before" }` o `"after"` come valore di `side` |
| `comment` | Testo letterale o `null` | `null` | `null` |

[Schema generato e controlli locali](corrections.py) · [Prompt editoriale integrale](../../src/pdf_annotator/services/review_contract/prompt.txt) · [Esempio sintetico con tutte e quattro le operazioni](example_corrections.json)

## Output, consumo e conservazione

Ogni esecuzione con argomenti validi crea una directory `output/m20/run-*` esclusa da Git. `report.md` riepiloga l'esito e `run_metrics.json` registra intervallo, SHA-256 e dimensioni del PDF, configurazione, tempi, stato API, tentativi e usage. Lo SHA-256 permette di riconoscere la versione del documento a partire dal suo contenuto. La CLI omette dai log il percorso del PDF sorgente, il corpo della richiesta, le risposte grezze e la chiave API. `corrections.json` contiene estratti testuali e viene scritto soltanto dopo una risposta completa, accettata dallo schema e dai controlli locali. `{"corrections": []}` è un esito valido. Per valutare l'esito di una directory, leggi `run_metrics.json` e cerca `status: completed`.

La stima usa soltanto il campo `usage` della risposta, anche quando la risposta è incompleta. I valori mancanti sono `null`, compreso il costo quando manca un dato necessario. I token di reasoning sono già compresi nei token di output fatturabili. La formula moltiplica separatamente input non in cache, input letto dalla cache, input scritto nella cache e output per le rispettive tariffe per milione, poi somma. Le tariffe Standard predefinite di Sol in USD sono rispettivamente `2.00`, `0.10`, `2.50` e `10.00`. Oltre 272.000 token di input si applicano i moltiplicatori documentati di Sol, 2× all'input e 1,5× all'output. `--rates INPUT CACHED WRITE OUTPUT` cambia le quattro tariffe. Per un altro modello occorre fornirle per ottenere una stima. Per i modelli senza addebito distinto per le scritture cache, usa per `WRITE` la tariffa `INPUT`. L'importo è una stima Standard, espresso come stringa decimale in `estimated_cost_usd`. Condizioni del progetto, sconti, maggiorazioni o tariffe non rappresentate possono modificare la fattura. [Prezzi di GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol#pricing) · [Contabilità della cache](https://developers.openai.com/api/docs/guides/prompt-caching)

L'estratto risiede in memoria. L'invio inline con `store: false` non crea un oggetto nella Files API e quindi non dà un `file_id` eliminabile. `store: false` disattiva il normale salvataggio dello stato della risposta. La conservazione lato servizio può comunque dipendere dai controlli del progetto, dai log di monitoraggio degli abusi e dalle eccezioni documentate per file e immagini. I log di monitoraggio, se attivi, sono normalmente conservati fino a 30 giorni. La cancellazione degli output locali agisce solo sulla directory di esecuzione. [Controlli e conservazione dei dati OpenAI](https://developers.openai.com/api/docs/guides/your-data)

## Verifica del prototipo

```powershell
.\.venv\Scripts\python.exe -m pytest -q experiments/m20/test_review.py
git diff --check
```

I test usano PDF sintetici e HTTPS simulato. Il primo campione reale ha generato sei proposte, applicate come sei annotazioni native e verificate dall'utente in Acrobat. L'esperimento successivo ha confermato il funzionamento del contesto editoriale e un miglioramento della pertinenza sul campione. La qualità editoriale dipende dal documento e dal modello. La localizzazione conferma testo e geometria dove possibile. L'accesso al modello dipende dal progetto API associato alla chiave.
