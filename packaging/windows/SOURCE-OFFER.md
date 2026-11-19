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

## Stato M22.7 e limiti M22.8

La build M22.7 usa l'export del commit pubblico
`07a992d46f8b8c0d1dd5b3c737e0922b85578a8a`.
Lo ZIP sorgenti esiste e contiene l'export applicativo esatto, gli otto
archivi upstream verificati, ricette, avvisi e ricevuta.
La futura offerta al pubblico si completa rendendo disponibili binario
e sorgenti con accesso equivalente senza costo aggiuntivo per il sorgente,
secondo AGPLv3 §6(d); la stessa GitHub Release è la modalità scelta.

Gli archivi PyMuPDF/MuPDF e Qt contengono i sorgenti delle versioni
identificate e ricette upstream: setup.py, Makefile/Makerules, CMake e
build_scripts. Non sono state applicate modifiche locali alle librerie.
Non è conclusa la valutazione della sufficienza del set rispetto a eventuali
modifiche o script ulteriori necessari ai binari upstream. Non è stata
identificata una patch concreta assente. Vedere la
[matrice M22.8](https://github.com/ErnestoGallio/fuPDF/blob/main/tests/manual/windows_redistribution.md#verifica-m228-degli-obblighi-effettivi).
Non sono richiesti di per sé SBOM, log originali, tutte le opzioni storiche
o binari riproducibili byte per byte. Restano invece necessari i file e
gli script che il §1 include effettivamente nel sorgente corrispondente.

Per Mesa/LLVM, MIT/NCSA richiedono la conservazione degli avvisi applicabili;
non impongono in generale sorgenti o patch per distribuire il solo binario.
Per i font OFL occorrono licenza e copyright e rispetto dei Reserved Font
Names; non è previsto un obbligo generale di consegnare il progetto sorgente.
I dubbi sulle parti incorporate non attribuite con certezza restano in matrice.

Per Qt conservare sorgenti pertinenti, testi GPLv3/LGPLv3 e istruzioni di
sostituzione e debug. La prova con il gruppo 6.11.3 è già superata;
la candidata mantiene 6.11.2. Il test non certifica da solo la completezza
del sorgente LGPL.

Gli ZIP M22.7 contengono i documenti precedenti e non vengono modificati.
Per ogni candidata successiva usare il nuovo commit pubblico e rigenerare
build, ZIP, ricevute e hash con la procedura esistente. Il commit effettivo
è registrato nelle ricevute generate, non fissato in questo documento.

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
