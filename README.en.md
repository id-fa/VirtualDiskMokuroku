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
4. Changes such as adding or updating drives are written to the catalog with File → Save catalog (Ctrl+S)
   (you are asked whether to save or discard them when closing)

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
| Saving / discarding changes | Changes are written to a working copy (`<name>.vdmoku.editing`) in the catalog's folder and applied to the catalog with File → Save catalog (Ctrl+S). "Revert unsaved changes" returns to the last saved state. When closing or opening another catalog, you are asked whether to save or discard. Turn on "Save changes to the catalog immediately (auto-save)" in Settings → Application settings to write every change to the catalog right away |
| Catalog encryption | Choose it when creating a catalog, or Settings → Catalog settings → Encryption / password |
| Ignore list, extended context | Settings → Catalog settings (stored per catalog) |
| Migration from Virtual CD-ROM Case (alpha) | File → Import catalog from another application and choose a `.cas` / `.caz` file (compressed files are fine; see [details](#importing-virtual-cd-rom-case-catalogs-alpha)) |
| Migration from Media Stencil (alpha) | File → Import catalog from another application and choose a `.fsr` file (see [details](#importing-media-stencil-record-files-alpha)) |

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

All drives registered in a Virtual CD-ROM Case catalog (`.cas` / `.caz`) can be added to the open catalog at once,
even if the original media is no longer at hand. Compressed files (a `.cas` saved with "Compress the cas file when
saving (compressed with Zlib)" and a `.caz` saved with "Always compress when saving (caz format)") are imported as
they are; UNLHA32.DLL is not needed. A `.caz` is decompressed with [7-Zip](https://7-zip.org/) if it is installed, and
with the built-in decoder otherwise. The built-in decoder takes tens of seconds to minutes on large files (hundreds of
MB), so installing 7-Zip brings that down to a few seconds.

Virtual CD-ROM Case itself can be downloaded from the pages below and still runs on Windows 11.

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
- The `.cas` format is not documented, so it is read based on the analysis of real files (format version 12,
  and version 9 written by releases from around 2001). Items whose meaning is unknown are not imported. If a file cannot be read, an error is shown and the catalog is
  left unchanged

### Importing Media Stencil record files (alpha)

> **This feature is alpha.** Support for the Media Stencil format is based on a structure inferred from analysing
> real files recorded with Version 3.20. Some files may not be readable, and some items may be missed or imported
> incorrectly. Keep the original `.fsr` files after importing.

All media recorded in a Media Stencil (メディアステンシル, by IzumiYa) record file (`.fsr`) can be added to the
open catalog at once. Choose File → Import catalog from another application and select the `.fsr` file (the same
menu as for Virtual CD-ROM Case `.cas` files; the format is detected from the file contents).

- <http://www.eva.hi-ho.ne.jp/izumiya/stencil/index.html> (Japanese)

What is imported:

- Folder structure, sizes, modified times, attributes; volume label, serial number and drive type; the time of
  recording (becomes the drive's "scanned at" time) and the used space (metadata `stencil_used_bytes`). Capacity
  and free space are not recorded in the file
- A name given with "Rename" becomes the drive's display name, and the comment from "Properties" becomes the
  comment in "Drive info"
- File icons recorded with "Also read icon information" (16 colours, 32x32) are imported as the thumbnail of
  that file, visible in the Properties panel and in the thumbnail view. Files whose icon was not recorded (shown
  with the associated icon) get nothing
- The contents of CAB and LZH archives recorded with "Also read archive contents" are imported as a single file,
  as this application does when scanning, and their contents go into "Files inside the archive / image" (name,
  size, modified time; searchable with "Search extended context too")
- A folder recorded on its own becomes a drive whose locations start with `?:\path\to\folder` (`?:\` for a whole
  drive; `.fsr` files do not record drive letters)
- Imported drives are always added as new drives. Connecting the real drive later and choosing "Update this drive"
  updates it as the same drive, matched by label and serial number (the imported icons are not kept after an
  update)
- The `.fsr` format is not documented, so it is read based on the analysis of real files (format version 1.00,
  Media Stencil 3.20). Items whose meaning is unknown are not imported. If a file cannot be read, an error is
  shown and the catalog is left unchanged

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
virtualdiskmokuroku import old.cas my.vdmoku         Import a Virtual CD-ROM Case catalog (.cas / .caz) or a Media Stencil record file (.fsr) (alpha; the catalog is created if missing)
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
While auto-save is off, unsaved changes are written to `<name>.vdmoku.editing` in the same folder, and saving replaces
the catalog with that file (a `.editing` file left behind by a crash can simply be deleted).

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
