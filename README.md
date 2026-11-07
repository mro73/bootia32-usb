# bootia32 USB creator

Creates a Linux live USB stick that boots on 32-bit UEFI tablets,
using `bootia32.efi` blob. It automates the process from the How-To section of
[bootia32-efi](https://github.com/lamadotcare/bootia32-efi) project by creating a GPT partition table, FAT32
partition, copying ISO files, and moving `bootia32.efi` to `/EFI/boot`.

**Warning:** the selected USB stick gets completely erased.

This is an unofficial helper and is not affiliated with the bootia32-efi project.
Tested on LAMINA LT1024 tablet with Lubuntu ISO.

## Run
- Single file:   `python3 bootia32-usb.pyz`      (build it with `packaging/build-pyz.sh`)
- From source:   `./bootia32-usb.py`
- CLI:        `sudo ./bootia32-usb.py write --device /dev/sdX --iso file.iso --yes`

## Safety checks
Before the stick is touched (it stays intact if any of these fail):

- optional **SHA-256 check** of the ISO (paste the hash, a SHA256SUMS line, or pick the SHA256SUMS file)
- the ISO is mounted and inspected: files too big for FAT32 (4 GiB+), contents that
  don't fit, ISO stored on the stick being erased, and non-Debian/Ubuntu-style ISOs
  (refused unless "Allow unrecognized ISO" / `--allow-unsupported`)

After copying: the stick is unmounted, caches are dropped, and every file is read back
and compared with the ISO ("Verify" checkbox / `--no-verify` to skip).

## While it runs
- **Cancel** stops cleanly at the next safe point and tells you whether the stick was already
  changed. Closing the window mid-write asks first and cancels the same way (Ctrl+C on the CLI).
- **Eject when finished** (on by default) powers the stick off so you can unplug it at once.
- **Save log...** writes the whole session (with system info and the result) to a text file.

## Dependencies
python3 + tkinter (`sudo apt install python3-tk`), `parted`, `dosfstools`,
`polkit` (for the password prompt).

## License
The code in this repository is released under [CC0 1.0](LICENSE) (public domain).

The bundled `data/bootia32.efi` comes from
[lamadotcare/bootia32-efi](https://github.com/lamadotcare/bootia32-efi), which is released
under the Unlicense.
Its SHA-256 is listed in `data/CREDITS.txt`, so you can compare it with
the upstream file or build your own from upstream's sources and pick it in the app.
