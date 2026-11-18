# Preparare la release portabile Windows x64

Eseguire questa procedura dopo la full suite manuale, il consolidamento
pubblico e la chiusura dei requisiti di licenza della
[matrice di redistribuzione](windows_redistribution.md). Gli ZIP locali
M22.6f sono prove di collaudo. La release usa il sorgente del commit
pubblico esatto e le istruzioni aggiornate per il CRT di sistema.

## Preparare il sorgente e verificare la ricevuta

Da PowerShell x64, nel checkout pubblico, usare lo SHA completo del commit
pubblico scelto per la release e verificarlo sul repository pubblico.
Gli otto archivi upstream devono essere
disponibili nelle cache passate allo script, insieme agli eventuali
materiali aggiuntivi richiesti dalla build effettiva.

```powershell
$ErrorActionPreference = 'Stop'
$commit = '<SHA pubblico di 40 cifre>'
if ($commit -notmatch '^[0-9a-f]{40}$') { throw 'Serve lo SHA completo del commit pubblico.' }
$release = Join-Path (Get-Location) "tmp\windows-release-$commit"
New-Item -ItemType Directory -Path $release | Out-Null
$stage = Join-Path $release 'sources'
& .\.venv\Scripts\python.exe -I -B packaging/windows/stage_source_archives.py --cache tmp/m226c-sources --cache tmp/m226e --output $stage --release-commit $commit
if ($LASTEXITCODE -ne 0) { throw 'Staging dei sorgenti fallito.' }

$receiptFile = Join-Path $stage 'RELEASE-SOURCE.json'
$receipt = Get-Content -LiteralPath $receiptFile -Raw | ConvertFrom-Json
$appArchive = Join-Path $stage $receipt.source_archive
$receiptHash = (Get-FileHash -LiteralPath $receiptFile -Algorithm SHA256).Hash.ToLowerInvariant()
$tree = git rev-parse "$commit^{tree}"
if ($LASTEXITCODE -ne 0 -or $receipt.commit -cne $commit -or $receipt.tree -cne $tree -or
    (Get-FileHash -LiteralPath $appArchive -Algorithm SHA256).Hash -ine $receipt.source_archive_sha256) {
    throw 'Ricevuta, commit, tree o archivio non corrispondenti.'
}
Expand-Archive -LiteralPath $appArchive -DestinationPath (Join-Path $release 'input')
$app = Join-Path $release 'input\app'
Copy-Item -LiteralPath (Join-Path $app 'packaging\windows\SOURCE-OFFER.md') -Destination $stage
```

Conservare nello staging anche i sorgenti, le patch, gli avvisi e le
istruzioni applicabili ai componenti realmente inclusi. La ricevuta lega
l'archivio applicativo a commit e tree. Gli hash in `SHA256SUMS` dello
staging verificano gli archivi upstream.

## Compilare dall'archivio estratto

Nella directory `$app`, creare un venv nuovo con **CPython 3.14.10
standard x64** secondo il README estratto. Installare i pin di build
prima del progetto e usare lo stesso ambiente per compilare. Conservare
l'inventario nativo di questa compilazione nella sola directory interna
`build/windows/`, insieme alla ricevuta e ai dati dei collaudi.

```powershell
Set-Location -LiteralPath $app
$python = Join-Path $app '.venv\Scripts\python.exe'
& $python -m pip install -r packaging/windows/requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Installazione dipendenze di build fallita.' }
& $python -m pip install --no-build-isolation -e .
if ($LASTEXITCODE -ne 0) { throw 'Installazione fuPDF fallita.' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/windows/build.ps1 -Mode Windowed -DistributionCandidate -SystemVCRuntime
if ($LASTEXITCODE -ne 0) { throw 'Build o verifica del pacchetto fallita.' }
$bundle = Join-Path $app 'dist\windows\Python-3.14.10-system-crt-candidate\fuPDF'
```

Il controllo esistente convalida provenienza, risorse e contenuti e
rifiuta copie del CRT app-local. Qualificare la cartella finale secondo
le [prove Windows](windows_release_qualification.md), inclusi Windows
pulito, prerequisito ufficiale x64 e avvio con le normali policy di
sicurezza. Eseguire le prove di salvataggio su una copia della cartella.
Concludere l'eventuale firma prima di calcolare gli hash finali.

## Creare e verificare gli asset

Dalla stessa sessione, archiviare la sola cartella portabile e il set
sorgenti già completato. La ricevuta deve restare nel pacchetto sorgenti.

```powershell
$version = & $python -I -B -c "from pathlib import Path; import tomllib; print(tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw 'Versione del progetto non leggibile.' }
$binaryZip = Join-Path $release "fuPDF-$version-windows-x64.zip"
$sourceZip = Join-Path $release "fuPDF-$version-windows-x64-sources.zip"
foreach ($zip in $binaryZip, $sourceZip) {
    if (Test-Path -LiteralPath $zip) { throw "Asset già esistente: $zip" }
}
& $python -I -B -m zipfile -c $binaryZip $bundle
if ($LASTEXITCODE -ne 0) { throw 'Creazione ZIP binario fallita.' }
& $python -I -B -m zipfile -c $sourceZip $stage
if ($LASTEXITCODE -ne 0) { throw 'Creazione ZIP sorgenti fallita.' }

@'
import hashlib
import json
import sys
from pathlib import PurePosixPath
from zipfile import ZipFile

binary, sources, commit, tree, receipt_hash = sys.argv[1:]
with ZipFile(binary) as z:
    names = set(z.namelist())
    assert len(names) == len(z.namelist())
    assert all(name.startswith("fuPDF/") for name in names)
    assert {"fuPDF/fuPDF.exe", "fuPDF/LICENSE", "fuPDF/THIRD_PARTY_NOTICES.md",
            "fuPDF/WINDOWS-PORTABLE.md"} <= names
    forbidden = {"environment.json", "native-inventory.json", "vc_redist.x64.exe",
                 "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll"}
    assert not {PurePosixPath(name).name.lower() for name in names} & forbidden
    assert z.testzip() is None
with ZipFile(sources) as z:
    receipt_bytes = z.read("sources/RELEASE-SOURCE.json")
    assert hashlib.sha256(receipt_bytes).hexdigest() == receipt_hash
    receipt = json.loads(receipt_bytes)
    assert (receipt["commit"], receipt["tree"]) == (commit, tree)
    with z.open("sources/" + receipt["source_archive"]) as app:
        assert hashlib.file_digest(app, "sha256").hexdigest() == receipt["source_archive_sha256"]
    sums = z.read("sources/SHA256SUMS").decode("ascii")
    published = {name: digest for digest, name in
                 (line.split("  ", 1) for line in sums.splitlines())}
    manifest = json.loads(z.read("sources/source-archives.json"))
    assert published == {entry["filename"]: entry["sha256"] for entry in manifest["archives"]}
    for name, expected in published.items():
        with z.open("sources/" + name) as upstream:
            assert hashlib.file_digest(upstream, "sha256").hexdigest() == expected
    assert z.testzip() is None
'@ | & $python -I -B - $binaryZip $sourceZip $commit $tree $receiptHash
if ($LASTEXITCODE -ne 0) { throw 'Verifica asset fallita.' }

$assets = @($binaryZip, $sourceZip)
$assets | Get-FileHash -Algorithm SHA256 | ForEach-Object {
    '{0}  {1}' -f $_.Hash.ToLowerInvariant(), (Split-Path -Leaf $_.Path)
} | Set-Content -LiteralPath (Join-Path $release 'SHA256SUMS.txt') -Encoding ascii

[ordered]@{
    commit = $commit
    tree = $tree
    build_input_sha256 = $receipt.source_archive_sha256
    source_receipt_sha256 = $receiptHash
    binary_zip_sha256 = (Get-FileHash -LiteralPath $binaryZip -Algorithm SHA256).Hash.ToLowerInvariant()
    source_zip_sha256 = (Get-FileHash -LiteralPath $sourceZip -Algorithm SHA256).Hash.ToLowerInvariant()
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $release 'RELEASE-ASSETS.json') -Encoding ascii
```

Pubblicare dalla stessa GitHub Release lo ZIP binario, lo ZIP sorgenti,
`SHA256SUMS.txt` e `RELEASE-ASSETS.json`, con riferimento al commit
compilato. Indicare il download dei sorgenti corrispondenti accanto al
binario, con accesso equivalente e senza sovrapprezzo. Dopo la
pubblicazione, verificare accesso e hash del download effettivo.
