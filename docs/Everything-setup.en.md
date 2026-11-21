# Setting up Everything

[日本語](Everything導入手順.md) | English

VirtualDiskMokuroku reads file lists with **Everything** by voidtools and its command line interface **ES (es.exe)**.
Neither is bundled with this application, so set them up as follows.

> **If you are not sure about any of this, you can use the application as it is, without Everything.** It then
> scans drives directly through the Windows API (slow on large drives).

- If you do not have Everything yet → [1-A](#1-a-if-you-do-not-have-everything-yet)
- If you already have Everything → [1-B](#1-b-if-you-already-have-everything)

## 1-A. If you do not have Everything yet

1. Open <https://www.voidtools.com/> and click **"Download Portable ZIP 64-bit"** for version 1.4 (not the Beta) to
   download `Everything-1.4.1.1032.x64.zip` (the version number changes over time).
   - If you also want to use Everything outside this application, you can install it with
     "Download Installer 64-bit" instead.
2. Open <https://www.voidtools.com/downloads/> (the Downloads page; the top page offers only Everything itself) and
   download `ES-1.1.0.38.x64.zip` (the version number changes over time) under
   **"Download Everything Command-line Interface"**.
3. Extract both ZIP files and put their contents (`Everything.exe`, `Everything.lng` and `es.exe`) in the
   `everything_portable` folder next to the VirtualDiskMokuroku executable.
   - The link in Help → "How to set up Everything" in the application opens this folder in Explorer.
   - If you used the installer, put only `es.exe` there.
4. When a scan begins and you are asked whether to start Everything, choose "Yes".
   You are ready when the **Test connection** button under Settings → Application settings shows the versions of
   es.exe and Everything.

## 1-B. If you already have Everything

The command line interface **ES (es.exe)** is not part of Everything itself, so add it separately.

1. On <https://www.voidtools.com/downloads/>, download `ES-1.1.0.38.x64.zip` (the version number changes over time)
   under **"Download Everything Command-line Interface"** and extract `es.exe` from it.
   - For Everything 1.5 (alpha / beta), use ES 1.1.0.30 or later, which supports 1.5.
2. Put `es.exe` in one of the places listed in [section 3](#3-where-to-put-esexe) (the `everything_portable`
   folder, the folder of `Everything.exe`, and so on).
3. Then keep Everything running as described in section 2 and check with **Test connection** in Application
   settings.

## 2. Keep Everything running

- Everything must be running while scanning (sitting in the notification area is fine).
  - If `Everything.exe` is in the same folder as es.exe (or in `everything_portable\`) and is not running, a dialog
    asks whether to start it when a scan begins and when you press "Test connection". Choosing "Yes" starts it in the
    background (notification area) and the scan begins once it responds.
- Indexing NTFS drives needs **administrator rights** or the **Everything service**.
  Enable "Everything Service" under Tools → Options → General so the index is available even when Everything runs
  without elevation.
- The application still works without these rights, but it switches to a direct scan whenever the target drive is
  not in Everything's index.

## 3. Where to put es.exe

Use one of the following:

- A folder on `PATH`
- The folder of the VirtualDiskMokuroku executable (or source tree), or `everything_portable\` below it
- Anywhere else, specified under Settings → Application settings → "Location of es.exe"

You are ready when the **Test connection** button in Application settings shows the versions of es.exe and
Everything.

### Instance name of Everything 1.5 alpha

Everything 1.5 alpha installed with the default settings uses the instance name `1.5a`.
If the connection test reports that Everything is not running, enter `1.5a` as "Everything instance name" in
Application settings.

## 4. Indexing external drives

By default Everything indexes only internal NTFS volumes.

- **External NTFS drives**: enable "Include new removable volumes" under Options → Indexes → NTFS, or add the
  volume individually.
- **CD/DVD, FAT/exFAT USB sticks, SD cards, etc.**: Everything usually does not index these, so the application
  switches to a direct scan automatically. No special setup is needed.

## 5. Faster scans (recommended settings)

By default the application also reads **creation times** and **attributes**. If Everything does not index them,
every scan re-reads them from the disk, which takes time (measured: under 1 second → 40 to 80 seconds on a drive
with about 1.15 million entries).

Either of the following fixes this:

- Enable **"Index creation date"** and **"Index attributes"** under Options → Indexes in Everything
- Turn off **"Read creation times"** and **"Read attributes"** under Settings → Catalog settings → General in the
  application
