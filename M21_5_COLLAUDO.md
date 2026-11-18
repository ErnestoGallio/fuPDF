# fuPDF M21.5 — Collaudo manuale della revisione

Stato `NOT RUN`. Usare la versione di sviluppo del checkout e una copia di un
PDF che si è autorizzati a trasmettere a OpenAI. Le prove da Avvia revisione
possono comportare un costo. Iniziare con un intervallo ridotto.

```powershell
.\.venv\Scripts\python.exe -m pdf_annotator
```

## Configurazione e avvio

1. Aprire il PDF di prova e un secondo PDF in un’altra scheda. Nel menu **File**
   scegliere **Revisione con IA…**. Verificare che la finestra riporti il primo
   documento e che il profilo predefinito sia **Libri scolastici**.
2. Aprire **Chiave API…**, immettere la propria chiave e salvarla. Il campo
   deve mascherare i caratteri durante la digitazione e svuotarsi al salvataggio.
   FuPDF indica solo la provenienza della configurazione. Sostituzione e
   rimozione usano Gestione credenziali Windows per l’utente corrente.
3. Verificare la precedenza chiudendo e riaprendo la finestra di revisione.
   FuPDF usa la chiave salvata in Windows. Quando il vault è accessibile e la
   voce è assente, può usare `OPENAI_API_KEY` dall’ambiente di sviluppo. Un
   errore di accesso al vault impedisce l’avvio. Salvare una chiave comporta
   soltanto la validazione locale del campo.
4. Scegliere **Intervallo** e indicare prima e ultima pagina fisica, contando
   dalla prima pagina del PDF. Verificare che, se si supera l’altro estremo,
   entrambi si allineino alla pagina appena scelta. Compilare facoltativamente
   un campo di contesto con più righe e
   caratteri accentati. Selezionare **Avvia revisione** per autorizzare l’invio
   delle pagine selezionate e delle adiacenti usate come contesto.

## Elaborazione e chiusura

1. Verificare lo stato dell’analisi, i conteggi di pagine e batch e il costo
   stimato. Il costo rimane indisponibile finché fuPDF non dispone di dati
   sufficienti. Se interviene un limite API, verificare l’indicazione di attesa.
2. Scorrere e cambiare zoom durante l’analisi. Passare alla seconda scheda.
   Chiudere la finestra di revisione e riaprirla dalla barra di stato. Il monitor
   deve continuare a mostrare il job del primo documento. Durante l’analisi
   provare anche un **Salva con nome…** esplicito sul documento originario.
3. Se ci sono batch falliti, verificare i numeri di pagina nel riepilogo.
   Se lo snapshot è disponibile, selezionare soltanto i batch da inviare di
   nuovo e premere **Riprova selezionati**. Verificare che la cronologia e le
   annotazioni dei batch precedenti non si duplichino.
4. In una prova separata, premere **Interrompi** durante l’analisi. Verificare
   che non vengano avviate nuove richieste e che i risultati validi restino
   nell’archivio. Provare la chiusura della scheda durante un job, annullare
   una volta e poi confermare. Ripetere con la chiusura di fuPDF. Una richiesta
   già in volo può terminare o raggiungere il proprio timeout prima del
   completamento della chiusura.

## Annotazioni e Adobe

1. Attendere il passaggio dall’analisi all’applicazione. Verificare che solo
   il PDF iniziale cambi e diventi dirty. Confrontare le annotazioni nel viewer
   e nella sidebar con il riepilogo. Provare Undo e Redo per una correzione.
2. Controllare le correzioni con motivazione collegata e le Sticky Notes
   effettivamente prodotte dal campione. Registrare i tipi che il campione
   contiene. Aprire l’archivio dal monitor per verificare il report di analisi
   e quello per proposta.
3. Usare **Salva con nome…** e riaprire la copia con Adobe Acrobat Desktop.
   Verificare i tipi nativi, le motivazioni e l’autore **Revisione IA** secondo
   i casi applicabili di AT-ACROBAT-06–09. Annotare separatamente i tipi assenti
   dal campione. Per InDesign eseguire separatamente AT-INDESIGN-01–04 con la
   coppia sorgente/esportazione prevista e verificare il testo dopo **Accept**.

## Verifica M21.5a senza API

1. Avviare la versione di sviluppo con il comando sopra e aprire il PDF già
   annotato durante il primo collaudo. Aprire **File → Revisione con IA…** e
   scegliere **Intervallo**. Provare le frecce di entrambi i campi, digitare un
   numero e premere Invio, poi usare Su e Giù. Verificare i limiti 1 e ultima
   pagina. Lasciare **Avvia revisione** inutilizzato.
2. Minimizzare con il pulsante della barra del titolo e ripristinare dal
   taskbar o da **File → Revisione con IA…**. Con il monitor aperto, portare
   davanti l’editor e scorrere o cambiare zoom. Richiamare il monitor e
   verificare che indichi ancora il documento di origine.
3. Dal PDF già annotato, raggiungere la nota nel margine in alto a destra.
   Controllare icona e clic a due zoom diversi. Verificare il testo e l’autore
   nel lettore read-only. Usare **Salva con nome…** su una copia di prova,
   chiuderla e riaprirla. Ripetere il clic e confrontare la copia in Acrobat.

## Verifica manuale M21.5b con Terra

1. Avviare la versione di sviluppo con il comando sopra e scegliere
   **File → Revisione con IA…**. Verificare che **Modello IA** preselezioni
   **GPT-6.1 Terra** e che **GPT-6.1 Sol** sia disponibile come alternativa.
2. Selezionare un piccolo intervallo di un PDF autorizzato alla trasmissione.
   Premere **Avvia revisione** soltanto per eseguire il collaudo reale a pagamento.
3. Aprire l’archivio e controllare `gpt-6.1-terra` nel manifesto, nei tentativi
   e nei report. Il manifesto deve indicare reasoning `medium` e tariffe
   Standard pari a 1,00 / 0,05 / 1,25 / 5,00 USD per milione di token.
   Se un batch è recuperabile, verificare che il nuovo tentativo usi ancora Terra
   anche dopo aver selezionato Sol per la prossima revisione.
