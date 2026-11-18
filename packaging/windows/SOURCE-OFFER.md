# Sorgenti della distribuzione Windows x64

La procedura riguarda fuPDF portabile con CPython 3.14.10 e CRT
Microsoft di sistema. Gli otto archivi upstream verificati in M22.6e
e i loro SHA-256 costituiscono la base dello staging.

Nel pacchetto di release, RELEASE-SOURCE.json identifica il commit,
il tree e l'hash dell'archivio fuPDF da cui è stata eseguita la build.
RELEASE-ASSETS.json, allegato nella stessa pagina, abbina per hash la
ricevuta e i due ZIP. Per confronto, lo ZIP locale M22.6f contiene
BUILD-SOURCE.json e uno snapshot del working tree della candidata.

Per ripetere la build, estrarre l'archivio applicativo indicato nella
ricevuta e usare la cartella app. Creare il venv Windows
x64 e seguire il README in app. Usare lo script in
app/packaging/windows/build.ps1 con le opzioni
-Mode Windowed -DistributionCandidate -SystemVCRuntime.
Consultare app/tests/manual/windows_redistribution.md per ABI e
ricostruzione dei componenti e
app/packaging/windows/THIRD_PARTY_NOTICES.md per versioni e licenze.
Gli archivi upstream mantengono i byte originali. SHA256SUMS e
source-archives.json riportano verifica e origini. Nel pacchetto di
release sono nella cartella sources accanto all'archivio applicativo.

Questo set resta un candidato per la verifica del sorgente
corrispondente. Mancano la conferma delle configurazioni e patch
complete dei binari upstream, l'inventario definitivo delle
attribuzioni delle unità effettivamente incorporate e la risoluzione delle
risorse font MuPDF rimaste senza corrispondenza raw nell'inventario M22.6g. Nessuna patch è stata dedotta dai soli
numeri di versione.

Per la futura offerta AGPLv3, produrre il sorgente fuPDF dal commit
effettivamente usato per la build e registrare commit, hash, pin, script,
configurazioni e patch necessari. Completare le evidenze per gli
upstream inclusi o ricostruirli da sorgenti e configurazione registrati,
includendo gli avvisi applicabili. Pubblicare lo ZIP binario e gli asset
del sorgente corrispondente nella stessa GitHub Release, con accesso
equivalente e senza costi aggiuntivi per il sorgente. Conservare
l'abbinamento mediante gli SHA-256. Per Qt conservare le istruzioni
di sostituzione e le informazioni d'installazione applicabili nella
documentazione allegata al binario.

## Abbinamento al futuro commit di release

Preparare il set upstream e il sorgente applicativo con un unico
comando offline. Passare il commit pubblico completo e una directory
di output nuova:

```powershell
$commit = '<SHA Git pubblico di 40 cifre>'
& .\.venv\Scripts\python.exe -I -B packaging/windows/stage_source_archives.py --cache tmp/m226c-sources --cache tmp/m226e --output tmp/release-sources --release-commit $commit
```

Verificare con `Get-FileHash -Algorithm SHA256` l'archivio applicativo
indicato in `RELEASE-SOURCE.json`. La ricevuta registra commit, tree e
hash dell'esportazione `git archive`. Estrarre quell'archivio e compilare
nella directory `app` estratta, usando un ambiente separato. Il materiale
della release deve contenere la ricevuta, l'archivio effettivamente usato
come input e gli altri sorgenti corrispondenti. Il manifesto della
release registra infine insieme lo SHA-256 del binario definitivo,
l'hash della ricevuta e gli hash degli asset sorgente. Allegare tutto alla
stessa pagina di download. La procedura di preparazione degli ZIP è in app/tests/manual/windows_first_release.md.
