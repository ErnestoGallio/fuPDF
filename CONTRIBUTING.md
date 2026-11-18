# Contribuire a fuPDF

## Segnalare un problema

Usa le issue del repository pubblico quando sarà disponibile. Indica la
versione di fuPDF, il sistema Windows, i passi per riprodurre il problema,
il risultato atteso e quello osservato. Per problemi grafici può essere utile
indicare la scala dello schermo e il layout del documento.

Usa un PDF sintetico e controlla testo, annotazioni, metadati, allegati e
screenshot prima di pubblicarli. Invia solo le righe di log pertinenti,
ripulite da chiavi, percorsi personali e contenuti dei documenti. Tieni i PDF
riservati, le credenziali, gli output della revisione e i dati personali fuori
da issue, pull request e commit. Per una vulnerabilità usa il
[canale riservato](SECURITY.md).

## Preparare una modifica

1. Leggi [specifica](PROJECT_SPEC.md), [architettura](ARCHITECTURE.md),
   [test di accettazione](ACCEPTANCE_TESTS.md), [roadmap](ROADMAP.md) e
   [istruzioni del repository](AGENTS.md).
2. Configura Python 3.11 x64 e l'ambiente virtuale come descritto nella
   [guida da sorgente](README.md#avvio-da-sorgente), poi installa i test.
3. Mantieni la modifica focalizzata. Scegli una fixture sintetica per
   riprodurre il caso e aggiungi o aggiorna i test pertinenti.
4. Nella pull request indica come riprodurre il comportamento, i controlli
   eseguiti e l'esito di eventuali verifiche manuali. Dichiara la provenienza
   e i termini dei materiali di terze parti che proponi di aggiungere.

~~~powershell
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -e '.[test]'
$env:QT_QPA_PLATFORM = 'offscreen'
& $python -m pytest -q tests/test_coordinates.py
git diff --check
~~~

Per la revisione con IA, usa le fixture e i trasporti simulati dei test. Per
l'interoperabilità Adobe, segui la [procedura delle fixture PDF](tests/manual/public_pdf_fixtures.md)
e indica quali gate sono stati eseguiti.
