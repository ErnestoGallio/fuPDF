# fuPDF portabile per Windows x64

Estrai l'intera cartella e mantieni insieme `fuPDF.exe` e `_internal`.
fuPDF si avvia dalla cartella estratta e non installa il runtime di sistema.

## Prerequisito Microsoft Visual C++ x64

Serve il **Microsoft Visual C++ Redistributable x64 v14 più recente
supportato da Microsoft**. La versione verificata del download ufficiale
durante M22.6g è **14.52.37029.0**. La 14.44.35211.0 ha superato lo
smoke test su un host già configurato e non è una soglia di supporto.
Gli header PE indicano la famiglia del linker e gli import osservabili.
Non ricostruiscono la versione esatta di tutti gli strumenti usati per
i binari upstream.

Per un controllo in sola lettura usa PowerShell **x64** nella directory
in cui hai scaricato l'installer ufficiale. Il confronto usa la versione
del pacchetto Microsoft verificato anziché una soglia fissa.

```powershell
$installer = Get-Item -LiteralPath '.\vc_redist.x64.exe'
$firma = Get-AuthenticodeSignature -LiteralPath $installer.FullName
if ($firma.Status -ne 'Valid' -or $firma.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
    throw 'Usa il pacchetto ufficiale Microsoft firmato.'
}
$versioneRichiesta = [version]($installer.VersionInfo.FileVersion -replace '\s.*$', '')
$registro = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
$dll = 'MSVCP140.dll', 'MSVCP140_1.dll', 'MSVCP140_2.dll', 'VCRUNTIME140.dll', 'VCRUNTIME140_1.dll'
$daVerificare = foreach ($nome in $dll) {
    $file = Get-Item -LiteralPath (Join-Path "$env:SystemRoot\System32" $nome) -ErrorAction SilentlyContinue
    if (-not $file -or [version]($file.VersionInfo.FileVersion -replace '\s.*$', '') -lt $versioneRichiesta) { $nome }
}
if ($registro.Installed -ne 1 -or $daVerificare -or
    [version]($registro.Version -replace '^v', '') -lt $versioneRichiesta) {
    'Runtime x64 assente, da aggiornare o da riparare'
} else {
    'Runtime x64 presente alla versione del pacchetto verificato o successiva'
}
```

Se mancante o insufficiente, avvia il [redistributable x64
più recente da Microsoft](https://aka.ms/vc14/vc_redist.x64.exe).
Il pacchetto può chiedere l'elevazione o un riavvio. Se è già presente
una versione idonea non serve reinstallarla. [Documentazione Microsoft](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)

## Licenze, sorgenti e Qt

`LICENSE` e `THIRD_PARTY_NOTICES.md` descrivono fuPDF e le dipendenze.
Le librerie Qt, PySide6 e Shiboken 6.11.2 sono collegate dinamicamente.
Per modifiche a tali librerie, conserva l'intera struttura della cartella
e lavora su una copia scrivibile. Sostituisci insieme le DLL Qt 6 Core,
Gui, Widgets e Network, le estensioni `Qt*.pyd`, `pyside6.abi3.dll`,
`Shiboken.pyd`, `shiboken6.abi3.dll` e i relativi plugin. Mantieni
nomi, percorsi, architettura Windows x64 e interfacce compatibili.
Il gruppo va prodotto con toolchain e ABI coerenti. Confronta import
ed export, controlla i moduli caricati, quindi verifica apertura,
rendering e Salva con nome. FuPDF consente la sostituzione delle
librerie compatibili e il reverse engineering necessario al debug
delle modifiche nei termini della LGPLv3.

Per la distribuzione pubblica consulta gli asset della stessa release:
i sorgenti corrispondenti e la procedura sono disponibili accanto allo
ZIP, senza sovrapprezzo. Consulta `SOURCE-OFFER.md` nel pacchetto
sorgenti e le note della release per firma e sistemi collaudati.
