# fuPDF

fuPDF è un'applicazione desktop per leggere e annotare PDF su Windows. Puoi
lavorare su più documenti in schede indipendenti e salvare i commenti come
annotazioni native, mantenendo il testo originale del PDF.

## Funzionalità

- Scorrimento verticale continuo, vista singola o affiancata anche con copertina,
  zoom e ricerca nel testo selezionabile.
- Penna, riquadro di testo, evidenziatore e richiamo (Callout). Il pannello dei
  commenti permette di passare alle annotazioni nel documento.
- Commenti editoriali per eliminare, inserire o sostituire testo. Il PDF conserva
  la richiesta di correzione come commento.
- Salva, Salva con nome, annulla e ripristina. La revisione con IA è facoltativa.

## Windows e avvio portabile

Il sistema di riferimento è **Windows 11 x64**. Il pacchetto portabile è stato
provato anche su Windows 10 Pro 22H2 x64 (build 19045). Il pacchetto include
Python e le librerie necessarie. La qualifica documenta le configurazioni
provate e i limiti delle prove DPI. [Qualifica Windows](tests/manual/windows_release_qualification.md)

1. Estrai tutto l'archivio ZIP in una cartella locale.
2. Mantieni insieme `fuPDF.exe` e l'intera cartella `_internal`, con tutti i file
   estratti e la struttura originale.
3. Avvia `fuPDF.exe` dalla cartella estratta e usa **File → Apri**.

La v0.18.0 portabile è **priva di firma digitale**. Su Windows 11, Smart App
Control può impedirne l'esecuzione se il binario è sconosciuto. [Documentazione Microsoft su Smart App Control](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/overview)

## Revisione con IA

Apri un PDF e scegli **File → Revisione con IA…**. In **Chiave API…**, inserisci
la tua chiave OpenAI e seleziona **Salva chiave**. Windows la conserva in Gestione
credenziali. Scegli profilo, modello e intervallo fisico, poi seleziona
**Avvia revisione**. L'uso dell'API può generare costi sul tuo account OpenAI.

Gli estratti inviati possono contenere pagine adiacenti di contesto, metadati
e annotazioni. Anche il contesto editoriale che inserisci viene inviato. Le
proposte utilizzabili possono diventare annotazioni nella scheda aperta. Salvi
le modifiche con i normali comandi di salvataggio. Lettura e annotazione manuale
funzionano offline. [Informativa sulla privacy](PRIVACY.md)

## Interoperabilità PDF

I collaudi manuali storici in Acrobat Desktop coprono il riconoscimento delle
correzioni editoriali e i round trip documentati per le annotazioni, inclusi
FreeText e Callout. I report registrano anche i limiti del profilo FreeText
nell'editor Acrobat. Il riferimento pubblico annotato in Acrobat Reader
copre i test automatici di importazione e preservazione. InDesign ha prove
separate per tipo, posizione e risultato esatto di `Accept`, da ripetere con una coppia sorgente/export
sintetica. [Report Acrobat M10](M10_REPORT.md) · [Report M15](M15_REPORT.md) ·
[Report M15.1](M15_1_REPORT.md) · [Procedura delle fixture Adobe](tests/manual/public_pdf_fixtures.md)

## Avvio da sorgente

Con **Python 3.11 x64**, esegui questi comandi in PowerShell dalla radice
del checkout.

~~~powershell
py -3.11 -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -e .
& $python -m pdf_annotator
~~~

Il progetto fissa PySide6 6.11.2 e PyMuPDF 1.28.2. Per eseguire i test, installa
`.[test]` con pip. Le fixture automatiche sono generate localmente.
[Istruzioni per contribuire](CONTRIBUTING.md)

### Compilare la cartella portabile

Usa lo stesso ambiente virtuale per installare le dipendenze di build e
avviare PyInstaller tramite lo script del progetto.

~~~powershell
& $python -m pip install -r packaging/windows/requirements-build.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/windows/build.ps1 -Mode Windowed
~~~

L'output è `dist/windows/Windowed/fuPDF/`. Per produrre la variante diagnostica,
usa `-Mode Console`. Per ambiente sanificato, smoke test e inventario nativo,
consulta le [istruzioni di packaging](tests/manual/windows_packaging.md) e la
[qualifica dell'ambiente](tests/manual/windows_build_environment.md).

## Licenza e contributi

Il codice di fuPDF è distribuito sotto **GNU AGPLv3** (`AGPL-3.0-only`).
I componenti di terze parti mantengono i propri termini.
[Testo della licenza](LICENSE) · [Avvisi e sorgenti delle
dipendenze](THIRD_PARTY_NOTICES.md)

Per bug e richieste di modifica, usa le issue del futuro repository pubblico
con esempi sintetici. Per le vulnerabilità è previsto il canale riservato.
[Contributi](CONTRIBUTING.md) · [Segnalazioni di sicurezza](SECURITY.md) ·
[Roadmap](ROADMAP.md) · [Note storiche di sviluppo](DEVELOPMENT_HISTORY.md)
