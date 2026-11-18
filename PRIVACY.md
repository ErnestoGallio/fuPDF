# Privacy

Questa informativa descrive il comportamento di fuPDF v0.18.0.

## Lettura e annotazione locale

fuPDF apre e modifica i PDF sul computer dell'utente. Il file e le annotazioni
restano sul dispositivo finché non li condividi o avvii una revisione con IA.
**Salva** scrive sul file aperto e **Salva con nome** scrive nel percorso scelto.
I commenti fanno parte del PDF salvato. Le annotazioni manuali possono
conservare come autore il nome visualizzato o il nome di accesso di Windows.
Verifica commenti e metadati prima di condividere un PDF. L'editor funziona
offline e fuPDF non raccoglie telemetria.

## Revisione con IA

L'invio inizia quando selezioni **Avvia revisione**. fuPDF raggruppa le pagine
fisiche richieste e può aggiungere fino a una pagina adiacente per lato
per ogni gruppo. Gli estratti derivano dallo stato del documento aperto,
incluse eventuali modifiche in memoria. Possono conservare metadati e
annotazioni delle pagine incluse, anche con allegati associati. FuPDF
invia via HTTPS alla Responses API di OpenAI l'estratto PDF, la mappa delle
pagine, le istruzioni del profilo e l'eventuale contesto editoriale compilato
dall'utente. Un nuovo tentativo può inviare di nuovo un estratto. Usa la revisione
solo con contenuti che sei autorizzato a trasmettere.

La revisione usa la chiave API del tuo account o progetto OpenAI. Se la salvi
dalla finestra **Chiave API…**, fuPDF la custodisce in Gestione credenziali
Windows per l'utente corrente, nella voce `fuPDF/OpenAI`. I costi dell'API
ricadono sul relativo account OpenAI. Il costo visualizzato da fuPDF è una
stima. Negli ambienti di sviluppo, fuPDF può anche leggere `OPENAI_API_KEY`
dalle variabili d'ambiente quando manca una chiave salvata.

### Trattamento da parte di OpenAI

OpenAI riceve ed elabora i dati della richiesta secondo i termini e i
controlli del tuo account API. FuPDF imposta `store: false` per disabilitare
il salvataggio della risposta come stato applicativo di quella richiesta.
OpenAI documenta separatamente log di monitoraggio
degli abusi che possono contenere dati delle richieste e delle risposte e
che, per impostazione predefinita, sono conservati fino a 30 giorni, con
eccezioni. I controlli speciali dipendono dall'idoneità e dalle impostazioni
del progetto OpenAI. Secondo la documentazione del provider, i dati dell'API
sono esclusi dall'addestramento salvo adesione esplicita. Consulta i termini
aggiornati per eccezioni e controlli applicabili ai file. [Controlli dei dati dell'API OpenAI](https://developers.openai.com/api/docs/guides/your-data)

## Dati locali della revisione e rimozione

Ogni job salva in `%LOCALAPPDATA%\fuPDF\review-jobs` una sottocartella con il
proprio identificativo. Può contenere il manifesto e il contesto editoriale,
risultati, estratti testuali nelle proposte, correzioni, report e registri dei
tentativi con stato, uso e stime di costo. Lo snapshot PDF e gli estratti usati
durante l'analisi sono in una cartella di lavoro temporanea. FuPDF li rilascia
quando termina di usarli, per esempio alla chiusura regolare della scheda.
Un'interruzione può lasciare temporanei residui.

I log diagnostici generali della versione portabile senza console sono
separati, normalmente in `%LOCALAPPDATA%\fuPDF\logs`. Se la cartella non
è disponibile, fuPDF usa la directory temporanea dell'utente. I log possono
includere identificativi tecnici e percorsi dello stack.

1. Termina la revisione e chiudi fuPDF. Rimuovere i dati di un job impedisce
   di recuperarne i risultati locali.
2. Apri Esplora file e incolla `%LOCALAPPDATA%\fuPDF\review-jobs` nella barra
   degli indirizzi.
3. Identifica il job confrontando intervallo, modello e profilo nel suo
   `manifest.json`. Se disponibili, gli orari dei tentativi sono nei file
   della sottocartella `attempts`. Elimina solo la sottocartella del job
   confermato. Se il job non è identificabile con sicurezza, lasciala intatta.

Le annotazioni già salvate nei tuoi PDF e le copie esportate da te restano
nei percorsi in cui le hai salvate. La rimozione del job agisce solo sui
suoi file locali.

Per rimuovere la chiave salvata, usa **Chiave API… → Elimina chiave** nella
finestra di revisione. L'eliminazione locale della chiave e dei job è distinta
dalla revoca della chiave e dai controlli di trattamento sul tuo account OpenAI.
