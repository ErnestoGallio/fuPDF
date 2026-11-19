# M24.1c — Ricomposizione deterministica dei fine riga

## Implemented

Aggiunta esclusivamente alla normalizzazione M24 una regola lessicale:
lettera Unicode + trattino ASCII `-` + un accapo + lettera Unicode.
Sono consumati anche spazi/tab immediatamente prima e dopo l'accapo.
Il trattino e l'accapo vengono eliminati; gli accapo rimanenti vengono poi
sostituiti con spazi secondo il comportamento precedente.

Nessun dizionario, riconoscimento dei paragrafi, geometria o riordinamento.
Estrazione `sort=False`, `base_textpage` e relativi flag invariati;
TEXT_DEHYPHENATE resta disattivato. Nessuna modifica al localizzatore.
Il JSON resta esattamente `{"pages":[{"page":1,"text":"..."}]}`.

I controlli alfabetici usano `str.isalpha()`: numeri, underscore, operatori e
simboli ai due estremi non autorizzano una ricomposizione. Gli spazi interni
non adiacenti al punto di ricomposizione rimangono identici. Non vengono
unite righe attraverso righe vuote né pagine fisiche differenti.

## Documento scolastico

Verificato `C:/Users/matte/Desktop/0000.000_000_SezA_UDA1.pdf`, 19 pagine.
**55 ricomposizioni complessive: le tre richieste e altre 52.**

| Pagina fisica | Numero | Parole ottenute, nell'ordine del documento |
| --- | ---: | --- |
| 4 | 5 | analizzare, assemblati, semplici, approcci, programmazione |
| 5 | 5 | riguardano, chiamante, vengono, automaticamente, chiamata |
| 6 | 1 | grado |
| 8 | 7 | visibilità, essere, essere, essa, procedura, variabili, aggiunto |
| 9 | 4 | programma, parametri, rimangono, crescente |
| 10 | 4 | esecuzione, dato, carattere, differenti |
| 11 | 9 | infatti, possa, riferimento, possiamo, procedura, attraverso, restituiti, aggiunti, esercizi |
| 12 | 2 | chiamata, preoccuperà |
| 13 | 9 | restituisce, alcuni, determinata, restituisce, numero, stata, quadrato, completare, aggiunti |
| 14 | 7 | funzione, funzione, primo, divisore, coordinate, funzione, completare |
| 18 | 1 | pagina |
| 19 | 1 | pagina |

Esempi: `ana-\nlizzare` → `analizzare`, `riguar-\ndano` → `riguardano`,
`chia-\nmante` → `chiamante`, `visi-\nbilità` → `visibilità`,
`preoccu-\nperà` → `preoccuperà`.

Esaminati tutti i contesti: **nessun falso positivo individuato nei 55
interventi**. Alcune ricomposizioni appartengono a note redazionali presenti
nel contenuto delle pagine; non sono nuova estrazione di annotazioni.

Il confronto sull'intero documento, escluso il whitespace, mostra soltanto
55 cancellazioni del carattere `-`: nessun'altra cancellazione, inserimento o
inversione. I composti interni, fra cui `top-down`, `bottom-up` e `n-esima`,
rimangono invariati.

## Listati C++

Ispezionati i contesti dei 55 interventi e **900 span** con font
`LucidaSansTypewriterStd` e relativa variante. Nessun intervento improprio
nelle istruzioni dei listati.

Un solo intervento tocca testo con quel font: a pagina 13,
«assegnato alla variabile qua-\ndrato» → «assegnato alla variabile quadrato».
È un identificatore citato nella prosa, non un'istruzione del listato.

L'audit usa temporaneamente i font del PDF nei test, non nella normalizzazione
di produzione. Il bit monospazio non è affidabile in questo file. I primi
tentativi di test avevano equiparato linee/font da codice ai listati; il test
finale verifica le posizioni originali e caratterizza esplicitamente il caso
legittimo di `quadrato`, senza accostare righe originariamente non adiacenti.

## Composti spezzati e limiti inevitabili

`soffitto-\npavimento` diventa **`soffittopavimento`** nel test sintetico:
è il falso positivo intrinseco della regola richiesta. Un composto autentico
spezzato dopo il trattino è indistinguibile da una sillabazione senza ulteriori
analisi, che non sono state introdotte. `soffitto-pavimento` su una riga resta
invariato. Nessun caso equivalente problematico è emerso nei due PDF esaminati.

Analogamente, un'espressione con identificatori alfabetici impaginata come
`a-\nb` soddisfa la regola e diventa `ab`; non è possibile riconoscere una
sottrazione da queste sole sequenze. Gli operatori con estremi non alfabetici
e le sequenze numeriche sono invece conservati. Nei listati del PDF controllato
non sono stati trovati interventi impropri di questo tipo.
Si gestisce il trattino ASCII richiesto, senza aggiungere conversioni di altri
segni Unicode, normalizzazione delle lettere o ulteriori pulizie.

## Localizzazione esistente

Tre test sul PDF originale passano con
`review_localization.localize_document(..., base_only=True)`:

- `analizzare`, pagina fisica 4 → `ana-\nlizzare`;
- `riguardano`, pagina fisica 5 → `riguar-\ndano`;
- `chiamante`, pagina fisica 5 → `chia-\nmante`.

Per ciascuno: applicabile, un candidato univoco e due quad corrispondenti alle
righe originali. Nessun secondo motore o modifica generale del matcher.
Le regressioni mirate di applicazione multimodale e rifiuto dell'ambiguità
usano trasporti offline, senza chiamate OpenAI.

## Romanzo

Verificato `Due Ragni in un Boccale`, 44 pagine. **0 ricomposizioni**.
Il JSON completo prima/dopo è identico, non soltanto la sequenza dei caratteri.
Le spaziature espressive delle pagine 21–24 sono preservate integralmente;
la sequenza corretta di pagina 25 rimane invariata. Restano i composti interni
`non-locali` e `soffitto-pavimento`. Export del test: 117.430 byte.

## Files changed

- `domain/structured_text.py`: funzione pura `normalize_page_text`, con la
  regola di ricomposizione e il flattening già esistente.
- `worker/structured_text.py`: usa la funzione solo dopo l'estrazione M24;
  eliminato il flattening locale duplicato.
- `tests/test_m24_dehyphenation.py`: 20 casi sintetici e due audit dei PDF locali.
- `tests/test_structured_text.py`: aspettativa M24 aggiornata a `sillabazione`,
  mantenuto il confronto con il flattening storico per la regressione.
- `PROJECT_SPEC.md`, `ARCHITECTURE.md`, `ACCEPTANCE_TESTS.md`, `ROADMAP.md`:
  normalizzazione M24.1c e AT-TEXT-07.
- `M24_1C_NORMALIZATION_REPORT.md`: questo report. Il precedente
  `M24_1C_REPORT.md` rimane evidenza storica del tentativo con il flag nativo.

Il working tree conteneva già M24.1b non committato; queste modifiche proseguono
sul branch esistente senza alterare gli altri percorsi di estrazione.

## Tests

Comando finale mirato:

```text
.venv\Scripts\python.exe -m pytest tests/test_m24_dehyphenation.py tests/test_structured_text.py tests/test_structured_text_ui.py tests/test_review_application.py -q -s --tb=short
```

Verificati anche il binding documento/revisione, i limiti dei file privati,
l'export JSON, la conservazione delle pagine vuote, il lifecycle Qt e Save As.
Risultato finale: **53 passed in 33.52s**.
`git diff --check`: **PASS**.
AT-TEXT-07 e le porzioni pertinenti di AT-TEXT-01..06 sono coperti dai test
mirati. Gate manuali Adobe: NOT RUN, nessuna nuova compatibilità dichiarata.

Nessuna dipendenza, API, full suite, build, commit o push.
