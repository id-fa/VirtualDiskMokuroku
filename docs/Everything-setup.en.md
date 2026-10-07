# Setting up Everything

[日本語](Everything導入手順.md) | English

VirtualDiskMokuroku reads file lists with **Everything** by voidtools and its command line interface **ES (es.exe)**.
Neither is bundled with this application, so set them up as follows.

> The application also works without Everything. In that case it scans drives directly through the Windows API
> (slow on large drives).

## 1. Download

1. Download Everything from <https://www.voidtools.com/downloads/> and install it (or extract the portable version).
2. On the same page, download **ES** from "Download Everything Command-line Interface" and take `es.exe` out of it.
   - For Everything 1.5 (alpha), use ES 1.1.0.30 or later, which supports 1.5.

## 2. Keep Everything running

- Everything must be running while scanning (sitting in the notification area is fine).
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
