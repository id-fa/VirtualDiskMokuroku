# VirtualDiskMokuroku

[日本語](README.md) | English

A Windows tool that catalogs the file lists of any drive (CD/DVD, portable HDD/SSD, etc.) so you can browse and
search them in an Explorer-like window while the drive is disconnected (offline). File lists are read with
Everything (es.exe) by voidtools.

## Requirements

- Windows 10 / 11, Python 3.12 or later
- PySide6 (`pip install PySide6`)
- Only for encrypted catalogs: `pip install cryptography`
- Everything and es.exe (not bundled → [docs/Everything-setup.en.md](docs/Everything-setup.en.md))
- Only for extended context: `pip install Pillow tinytag charset-normalizer olefile py7zr rarfile pycdlib`
  (features whose library is missing are simply disabled)

## Starting

```
Double-click VirtualDiskMokuroku.pyw            (run directly from the source tree)
python VirtualDiskMokuroku.pyw [catalog.vdmoku]
```

After `pip install -e .`, the `virtualdiskmokuroku` command starts it as well.

## Usage

1. File → New catalog creates a catalog file (`.vdmoku`)
2. Drive → Add / update drive selects a drive and scans it
3. From then on you can browse its contents in the tree and the list even with the drive disconnected

| Feature | How |
|---|---|
| Folder size totals | "Size" column of folder rows in the list, "Folder total" in the status bar |
| Free space of a drive | Next to the drive name in the tree and at the right of the status bar (values at scan time) |
| Incremental filter | Input box above the list. Choose the scope from "This folder / Including subfolders / All drives". Space-separated terms are ANDed |
| Copy names / full paths | Select rows and press Ctrl+C / Ctrl+Shift+C, or right-click |
| Export the list | Ctrl+E (text or CSV; all rows are written even when the display is truncated at the limit) |
| Thumbnail view | "View" at the top right of the list (or the View menu) → "Tiles" / "Thumbnails with info". Available for catalogs that store image thumbnails |
| Open the real folder | Right-click in the list or the tree → "Open this folder in Explorer". Shown only while the same drive is connected (even under a different drive letter) |
| Update a drive | Right-click a drive in the tree → "Update this drive". The drive is identified by label and serial number, so a changed drive letter is fine |
| Drive groups | Right-click a drive in the tree → "Change group" (choose an existing group or type a new name; leave it empty to remove the drive from its group). Drives in the same group are shown together in the tree. Groups are one level deep, and a group with no drives disappears. Right-click a group to rename it |
| Organize drives | Drive → Organize drives. Reorder drives and move them between groups by drag & drop (or "Up / Down"), delete several drives at once, or remove only the extended context (thumbnails, text content, etc.). Order, group changes and deletions are written to the catalog together when you press OK |
| Copy drives to another catalog | Select drives in "Organize drives" and press "Copy to another catalog". Choosing an existing catalog adds them to it; a new file name creates a new catalog (works across encrypted and unencrypted catalogs; backups are not copied) |
| Backups | Keeps the previous database inside the catalog when a drive is updated (1 generation by default). Drive → Restore from backup |
| Catalog encryption | Choose it when creating a catalog, or Settings → Catalog settings → Encryption / password |
| Ignore list, extended context | Settings → Catalog settings (stored per catalog) |
| Migration from Virtual CD-ROM Case (alpha) | File → Import Virtual CD-ROM Case catalog and choose a `.cas` file (saved without compression; see [details](#importing-virtual-cd-rom-case-catalogs-alpha)) |

### Extended context

For the items enabled in the catalog settings, extra information is read from the file contents during a scan
and stored in the catalog. It is shown in the Properties panel when a file is selected, and becomes part of the
filter when "Search extended context too" is checked.

- EXIF (make, camera model, etc.) / Office document metadata / audio and video tags
- Content of small text files (if the encoding was misdetected, choose an encoding in the Properties panel and
  re-read it; the drive does not need to be reconnected)
- Image thumbnails / file lists inside archives (zip, 7z, rar, tar) and ISO images

### Thumbnail view

Catalogs that store image thumbnails can show the list as thumbnails (files without a thumbnail and folders are
shown as icons).

- **Tiles**: thumbnails laid out side by side. The captions under each thumbnail (file name, resolution, file
  size, modified time) can be toggled individually with View → "Captions under thumbnails in tile view"
- **Thumbnails with info**: name, resolution, size, modified time and type next to each thumbnail
- **Double size**: shows the stored thumbnails at twice their size (in both views)
- Sorting is available from View → "Sort by"

The resolution is the width x height of the original image, stored when the thumbnail is read. For catalogs created
before this feature existed, updating (rescanning) the drive re-reads the thumbnails and fills in the resolution.

A rescan reuses the results of files that have not changed since the last scan, so they are not read again.
If you cancel while the extended context is being read, what was read so far is registered; the next update of
that drive reads only the remaining files (cancelling while the file list is being read registers nothing).

### Catalog encryption

The contents of a catalog (file names, folder structure, thumbnails, text content and drive information) can be
encrypted with a password.

- **Setting it up**: choose encryption when creating a catalog, or enable / remove it later with Settings →
  Catalog settings → Encryption / password. Changing the password is instant (the contents are not re-encrypted)
- **Scheme**: AES-256-GCM, with scrypt for deriving the key from the password. Tampering and corruption are
  detected when the catalog is opened
- **No plaintext on disk**: while browsing, the decrypted contents are opened in memory, and scanning also works in
  memory. Only when a single database exceeds the limit (512 MB by default; changeable in Settings → Application
  settings) is it decrypted temporarily into `%LOCALAPPDATA%\VirtualDiskMokuroku\cache\session-*`, which is
  deleted when the catalog is closed
- **Other tools cannot open it.** File → "Export decrypted copy" writes an unencrypted catalog
- **If you forget the password, the catalog cannot be opened.** There is no way to recover it

What encryption does not protect:

- Everything's own index (Everything keeps the file names of connected drives)
- Exported files and anything copied to the clipboard
- Traces left on disk after the temporary files above are deleted, and the contents of memory or the page file
- Older versions of the application (without encryption support) cannot open it and report that it was created
  with a newer version

### Importing Virtual CD-ROM Case catalogs (alpha)

> **This feature is alpha.** Support for the Virtual CD-ROM Case format is still being investigated; the file
> structure was inferred from analysing real files. Some files may not be readable, and some items may be missed or
> imported incorrectly. Keep the original `.cas` files after importing.

All drives registered in a Virtual CD-ROM Case catalog (`.cas`) can be added to the open catalog at once, even if
the original media is no longer at hand.

The file must have been saved by Virtual CD-ROM Case with the following settings:

- In the save options, turn off "Compress the cas file when saving (Z) <compressed with Zlib>"
- In the save options, turn off "Always compress when saving (A) <caz format, requires UNLHA32.DLL>"
- In "Save as", choose the file type "Case file (*.cas)" ("Case compressed file (*.caz)" is not supported)

Virtual CD-ROM Case itself can be downloaded from the pages below and still runs on Windows 11. Open a compressed
file in Virtual CD-ROM Case and save it again with the settings above.

- <http://www.hi-ho.ne.jp/hiro30/soft.html>
- <https://www.vector.co.jp/soft/win95/util/se078992.html>

What is imported:

- Folder structure, sizes, modified and creation times, attributes; volume label, serial number, file system,
  capacity and free space
- Comments on files and folders become "text content"; categories, properties (HTML titles, version information
  of executables, PDF document information, AVI tags, etc.) and CRC32 become metadata, all searchable with "Search
  extended context too". Garbled comments can be re-read with an encoding chosen in the Properties panel. The
  category of a drive is shown in "Drive info"
- Archives that were registered with their contents expanded (ZIP, LZH, RAR, etc.) are imported as a single file,
  as this application does when scanning, and their contents go into "Files inside the archive / image" (name,
  size, modified time; searchable with "Search extended context too"). The CRC of each file inside the archive is
  not imported
- Drives registered inside a group (folder) are imported into a group of the same name. Groups are one level deep,
  so nested groups are flattened to the outermost one (the group comment is shown in "Drive info")
- `.cas` files do not record drive letters, so locations are shown starting with `?:\`
- Imported drives are always added as new drives. Connecting the real drive later and choosing "Update this drive"
  updates it as the same drive, matched by label and serial number (the imported comments are not kept after an
  update)
- The `.cas` format is not documented, so it is read based on the analysis of real files (format version 12).
  Items whose meaning is unknown are not imported. If a file cannot be read, an error is shown and the catalog is
  left unchanged

## Display language

The interface is Japanese when the Windows display language is Japanese, and English otherwise. You can fix the
language with "Language" in Settings → Application settings (takes effect the next time the application starts).
Setting the environment variable `VIRTUALDISKMOKUROKU_LANG` to `ja` or `en` overrides the setting (for testing).

## Command line

```
virtualdiskmokuroku volumes                         List the connected volumes
virtualdiskmokuroku scan E:\ my.vdmoku [--name NAME] Scan and add / update (the catalog is created if missing)
virtualdiskmokuroku scan E:\ my.vdmoku --encrypt --password ****   Create a new encrypted catalog
virtualdiskmokuroku list my.vdmoku                   List the drives in a catalog
virtualdiskmokuroku find my.vdmoku word1 word2       Search all drives by name
virtualdiskmokuroku import old.cas my.vdmoku         Import a Virtual CD-ROM Case catalog (alpha; the catalog is created if missing)
```

From the source tree, run `python VirtualDiskMokuroku.pyw scan ...`.

## Catalog file structure

A `.vdmoku` file is a ZIP archive. Each drive has its own SQLite database, which is rebuilt as a whole when the
drive is updated.

```
manifest.json                             Catalog settings and the list of drives
drives/<id>/files.db                      Basic file information
drives/<id>/context.db                    Extended context (only when enabled)
drives/<id>/backup/<timestamp>/...        Previous generations
```

`.pmcat` files created under the former name PyMediaCatalogue can still be opened.

While browsing, only the databases that are needed are extracted to `%LOCALAPPDATA%\VirtualDiskMokuroku\cache`.

## Limitations

- The "Password check only (no encryption)" protection is only checked when the catalog is opened in this
  application. The contents can be read by extracting the file as a ZIP, so use encryption to protect them.
- Case-insensitive filtering applies to ASCII letters only (full-width letters and accented characters are
  matched exactly).
- Scanning is slow when Everything does not index creation times and attributes → chapter 5 of
  [docs/Everything-setup.en.md](docs/Everything-setup.en.md).
- Comparing backup generations is not implemented yet (planned).

## Development

```
python -m pytest -q
```

## License

[MIT License](LICENSE)

The licenses of Qt / PySide6 (LGPL) and the other libraries bundled with the distributed executable are in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) and `licenses/`.
