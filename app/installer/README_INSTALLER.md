# DedSec CCTV — Windows installer

Polished Inno Setup 6 wizard (modern style, dark DedSec sidebar art). Default install is **per-user** under `%LOCALAPPDATA%\Programs\DedSec CCTV` so the built-in updater can replace files without admin rights.

## Quick build (on your Windows PC)

1. Build the app (from `dedsec-app-source`):

   ```bat
   python build.py
   ```

   Output: `DedSecCCTV\DedSecCCTV.exe` plus dependencies.

2. (Recommended) Build the updater and place it in the dist folder:

   ```bat
   python -m nuitka --onefile --windows-console-mode=disable --output-filename=DedSecUpdater.exe updater.py
   copy DedSecUpdater.exe DedSecCCTV\
   ```

3. Install Inno Setup 6 if needed:

   ```bat
   winget install --id JRSoftware.InnoSetup -e
   ```

4. Copy this `installer\` folder next to `app.py` / `build.py` (same level as the `DedSecCCTV\` output folder), then:

   ```bat
   powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
   ```

5. Setup exe:

   `installer\Output\DedSecCCTV-Setup-<version>.exe`

Optional: regenerate wizard images after changing the logo:

```bat
python installer\make_images.py
```

Override the version without editing `app.py`:

```bat
powershell -File installer\build_installer.ps1 -AppVersion 1.0.3
```

## What the wizard does

| Item | Behaviour |
|------|-----------|
| Style | `WizardStyle=modern`, custom dark sidebar + header icons |
| Default dir | `%LOCALAPPDATA%\Programs\DedSec CCTV` (`PrivilegesRequired=lowest`, override dialog allowed) |
| License | MIT (`LICENSE.txt`) |
| Info page | Public cameras only, account required, VPN recommended |
| Tasks | Desktop shortcut (checked by default); Start Menu always |
| Finish | Optional “Launch DedSec CCTV” |
| Upgrade | In-place over the previous install; refuses if a *newer* version is already present |
| Close apps | Asks to close `DedSecCCTV.exe` / `DedSecUpdater.exe` (`CloseApplications` + `AppMutex`) |
| Uninstall | Optional removal of `%APPDATA%\DedSecCctv` settings; **does not** wipe Windows Credential Manager |
| OS | Windows 10 or later, 64-bit (`x64compatible`) |

## SmartScreen (“Windows protected your PC”)

Unsigned installers trigger Microsoft Defender SmartScreen. Users can choose **More info → Run anyway**. That is expected until the setup binary is code-signed.

### Code-signing options (nothing is purchased for you here)

- **Azure Trusted Signing** — cloud signing suitable for ongoing releases; Microsoft’s modern replacement for many EV workflows.
- **Standard / EV code-signing certificate** — from a public CA (Sectigo, DigiCert, SSL.com, etc.). EV usually gets reputation faster but costs more and often needs a hardware token or cloud HSM.
- **SignPath.io (free tier for open source)** — CI-oriented signing for public open-source projects; worth applying if the GitHub repo qualifies.
- **Self-signed** — does **not** help with SmartScreen; skip for releases.

After you have a cert, sign the setup (and ideally `DedSecCCTV.exe` / `DedSecUpdater.exe`) with `signtool` before publishing. Example shape:

```bat
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
  installer\Output\DedSecCCTV-Setup-1.0.2.exe
```

(Exact flags depend on your cert provider.)

## Layout of this folder

```
installer/
  DedSecCCTV.iss          Inno script
  build_installer.ps1     Finds ISCC, reads version, compiles
  version.inc             APP_VERSION define (auto-written by the ps1)
  LICENSE.txt             MIT
  InfoBefore.txt          “Before you start” page
  make_images.py          Pillow generator for wizard art
  preview_welcome.png     Mockup of the welcome page
  assets/
    dedsec_logo.ico
    dedsec_logo.png
    wizard_image.png / @2x
    wizard_small.png / @2x
  Output/                 Created by the build (setup exe)
```

## App / updater notes

- `install_dir()` in `app.py` is the folder containing the exe — `%LOCALAPPDATA%\Programs\DedSec CCTV` works with the updater as-is (no admin).
- User settings stay in `%APPDATA%\DedSecCctv\` (unchanged).
- The installer declares `AppMutex=DedSecCCTV_SingleInstance`. The app does **not** create that mutex yet; adding it avoids file-in-use upgrades. See the report from the packaging agent for a suggested snippet.
