#!/usr/bin/env python3
"""
bootia32-usb - make an IA32-UEFI-bootable USB stick from a Linux ISO.

Automates the manual procedure from the bootia32-efi README:
  1. new GPT partition table on the stick
  2. one FAT32 partition
  3. copy ALL files from the ISO (including dot files; symlinks are skipped)
  4. copy bootia32.efi into /EFI/boot

bootia32.efi is bundled (data/bootia32.efi); --efi / the GUI field only
override it.

Usage
  GUI (default):   ./bootia32-usb.py
  List USB disks:  ./bootia32-usb.py list
  CLI write:       sudo ./bootia32-usb.py write --device /dev/sdX \
                        --iso lubuntu.iso --sha256 SHA256SUMS --yes

Cancel: the GUI's Cancel button (Ctrl+C on the command line) stops cleanly and
tells you whether the stick was already modified. After a successful write the
stick is ejected (--no-eject to keep it attached).

Safety: the ISO is checksummed (if you give a hash) and inspected BEFORE the
stick is touched; after copying, the stick is read back and compared with the
ISO (skip with --no-verify).

Requires: python3, python3-tk (GUI only), util-linux (lsblk, mount, umount,
wipefs), parted, dosfstools (mkfs.vfat), udev, polkit (pkexec, GUI only).

WARNING: the selected device is ERASED.
"""
import argparse
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

FAT32_MAX_FILE = 4 * 1024**3 - 1
REQUIRED_TOOLS = ["lsblk", "mount", "umount", "wipefs", "parted", "mkfs.vfat"]
BUNDLED = "(bundled bootia32.efi)"      # shown in the GUI field
CHUNK = 4 * 1024 * 1024
SHA_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])")


# --------------------------------------------------------------------------
# Bundled files: work from a plain folder, a .pyz single-file bundle, or an
# AppImage.
# --------------------------------------------------------------------------
def _here():
    try:
        return Path(__file__).resolve()
    except NameError:                   # running via `python -c` (worker)
        return None


def read_resource(rel):
    """Bytes of a bundled file, e.g. read_resource('data/bootia32.efi')."""
    here = _here()
    if here is None:
        raise RuntimeError("Bundled files are not available in this mode.")
    f = here.parent / rel
    if f.is_file():
        return f.read_bytes()
    if zipfile.is_zipfile(here.parent):                 # .pyz bundle
        with zipfile.ZipFile(here.parent) as z:
            return z.read(rel)
    raise FileNotFoundError(rel)


def have_bundled_efi():
    try:
        read_resource("data/bootia32.efi")
        return True
    except Exception:
        return False


def stage_bundled_efi(into=None):
    """Write the bundled EFI file to a private temp dir (readable by root)."""
    d = into or tempfile.mkdtemp(prefix="bootia32-efi-")
    p = os.path.join(d, "bootia32.efi")
    with open(p, "wb") as f:
        f.write(read_resource("data/bootia32.efi"))
    return p


def own_source():
    """Source code of this program, so root runs exactly this text and not a
    file that another process could swap after you authenticated."""
    here = _here()
    if here.is_file():
        return here.read_text()
    with zipfile.ZipFile(here.parent) as z:             # .pyz bundle
        return z.read(here.name).decode()


def missing_tools():
    path = os.environ.get("PATH", "") + ":/usr/sbin:/sbin"
    return [t for t in REQUIRED_TOOLS if not shutil.which(t, path=path)]


def worker_python():
    """Interpreter for the root worker. Inside an AppImage the bundled Python
    lives on a FUSE mount that root cannot read, so use the system one."""
    if os.environ.get("APPIMAGE"):
        py = shutil.which("python3", path="/usr/bin:/bin:/usr/local/bin")
        if not py:
            raise RuntimeError("System python3 not found (needed to run the "
                               "privileged step). Install python3.")
        return py
    return sys.executable


# --------------------------------------------------------------------------
# Device discovery (works unprivileged)
# --------------------------------------------------------------------------
def _truthy(v):
    return v in (True, 1, "1", "true", "True")


def _mountpoints(node):
    mps = node.get("mountpoints") or [node.get("mountpoint")]
    out = [m for m in mps if m]
    for child in node.get("children", []) or []:
        out += _mountpoints(child)
    return out


def list_devices(include_fixed=False):
    """Return removable / USB whole-disk devices."""
    cols = "NAME,PATH,SIZE,MODEL,VENDOR,TRAN,RM,HOTPLUG,TYPE,MOUNTPOINTS"
    try:
        raw = subprocess.run(["lsblk", "-J", "-b", "-o", cols],
                             capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:  # older lsblk: no MOUNTPOINTS column
        raw = subprocess.run(
            ["lsblk", "-J", "-b", "-o", cols.replace("MOUNTPOINTS", "MOUNTPOINT")],
            capture_output=True, text=True, check=True).stdout
    devs = []
    for d in json.loads(raw)["blockdevices"]:
        if d.get("type") != "disk":
            continue
        removable = (_truthy(d.get("rm")) or _truthy(d.get("hotplug"))
                     or d.get("tran") == "usb")
        if not removable and not include_fixed:
            continue
        mps = _mountpoints(d)
        d["system"] = any(m == "/" or m.startswith("/boot") or m == "/home"
                          for m in mps)
        d["mounts"] = mps
        d["size"] = int(d.get("size") or 0)
        devs.append(d)
    return devs


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000


def device_label(d):
    name = " ".join(x for x in ((d.get("vendor") or "").strip(),
                                (d.get("model") or "").strip()) if x) or "Unknown device"
    return f"{d['path']}  -  {name}  ({human(d['size'])})"


# --------------------------------------------------------------------------
# Privileged worker
# --------------------------------------------------------------------------
def emit(kind, msg=""):
    print(f"{kind} {msg}".rstrip(), flush=True)


def run(cmd):
    emit("LOG", "$ " + " ".join(cmd))
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"Required tool not found: {cmd[0]}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"{cmd[0]} failed: {(e.stderr or e.stdout or '').strip()}")
    return r.stdout


def partition_path(dev):
    out = run(["lsblk", "-nrpo", "PATH,TYPE", dev])
    parts = [l.split()[0] for l in out.splitlines() if l.split()[-1] == "part"]
    if not parts:
        raise RuntimeError("Partition did not appear after partitioning.")
    return parts[0]


CANCEL_FILE = None          # worker stops cleanly as soon as this file exists
STATE = {"modified": False, "written": False}


class Cancelled(Exception):
    pass


def check_cancel():
    if CANCEL_FILE and os.path.exists(CANCEL_FILE):
        raise Cancelled()


def _on_term(signum, frame):
    raise Cancelled()


def step(msg):
    check_cancel()
    emit("STEP", msg)
    emit("PROGRESS", "0")


class Progress:
    def __init__(self, total):
        self.total, self.done, self.last = max(total, 1), 0, 0.0

    def add(self, n):
        check_cancel()
        self.done += n
        now = time.time()
        if now - self.last > 0.3:
            emit("PROGRESS", f"{self.done * 100 / self.total:.1f}")
            self.last = now

    def finish(self):
        emit("PROGRESS", "100")


def resolve_sha256(arg, iso_path=""):
    """Find the expected SHA-256. `arg` may be a bare hash, a line from a
    SHA256SUMS file, or the path of a SHA256SUMS file. Returns None if no
    (unambiguous) hash for this ISO is found."""
    arg = (arg or "").strip()
    if not arg:
        return None
    if os.path.isfile(arg):
        with open(arg, errors="replace") as f:
            arg = f.read(1024 * 1024)
    name = os.path.basename(iso_path)
    found = []
    for line in arg.splitlines():
        m = SHA_RE.search(line)
        if m:
            found.append((m.group(1).lower(), line))
    for h, line in found:
        if name and name in line:
            return h
    return found[0][0] if len(found) == 1 else None


def sha256_file(path, prog):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(CHUNK)
            if not buf:
                return h.hexdigest()
            h.update(buf)
            prog.add(len(buf))


def walk_files(root):
    """Yield (absolute, relative) paths of regular files; symlinks are skipped."""
    for dp, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(dp, f)
            if not os.path.islink(p):
                check_cancel()
                yield p, os.path.relpath(p, root)


def _find(root, *parts):
    """Case-insensitive path lookup; returns the real path or None."""
    cur = root
    for part in parts:
        try:
            match = next(n for n in os.listdir(cur) if n.lower() == part.lower())
        except (OSError, StopIteration):
            return None
        cur = os.path.join(cur, match)
    return cur


def inspect_iso(root):
    """Pre-flight look at a mounted ISO.
    Returns (total_bytes, files_too_big_for_fat32, kind, warnings)."""
    total, too_big = 0, []
    for p, rel in walk_files(root):
        size = os.path.getsize(p)
        total += size
        if size > FAT32_MAX_FILE:
            too_big.append((rel, size))
    if _find(root, "casper") or _find(root, "live"):
        kind = "debian"
    elif (_find(root, "LiveOS") or _find(root, ".treeinfo")
          or _find(root, "images", "pxeboot")):
        kind = "redhat"
    else:
        kind = "unknown"
    warnings = []
    if kind == "debian" and not (_find(root, "boot", "grub", "grub.cfg")
                                 or _find(root, "EFI", "boot", "grub.cfg")):
        warnings.append("No boot/grub/grub.cfg found on the ISO; the stick "
                        "may not boot.")
    return total, too_big, kind, warnings


KIND_TEXT = {
    "redhat": "It looks like a Red Hat/Fedora-style ISO",
    "unknown": "It has no casper/ or live/ folder, so it does not look like "
               "a Debian/Ubuntu live ISO",
}


def copy_tree(src, dst, total):
    prog = Progress(total)
    for dp, dirs, files in os.walk(src):
        rel = os.path.relpath(dp, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for f in files:
            sp = os.path.join(dp, f)
            if os.path.islink(sp):          # README: symlinks are not required
                continue
            with open(sp, "rb") as fi, open(os.path.join(target, f), "wb") as fo:
                while True:
                    buf = fi.read(CHUNK)
                    if not buf:
                        break
                    fo.write(buf)
                    prog.add(len(buf))
    prog.finish()


def compare_files(a, b, label, prog):
    if not os.path.isfile(b):
        raise RuntimeError(f"Verification failed: {label} is missing on the stick.")
    if os.path.getsize(a) != os.path.getsize(b):
        raise RuntimeError(f"Verification failed: {label} has the wrong size on the stick.")
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(CHUNK), fb.read(CHUNK)
            if x != y:
                raise RuntimeError(f"Verification failed: {label} differs from "
                                   "the ISO. The stick may be faulty or fake.")
            if not x:
                return
            prog.add(len(x))


def drop_caches(part):
    """Make the next read come from the stick, not from RAM."""
    os.sync()
    try:
        subprocess.run(["blockdev", "--flushbufs", part], capture_output=True)
    except OSError:
        pass
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
    except OSError:
        emit("LOG", "(could not clear the page cache; the read-back may be "
                    "served from memory)")


def eject_device(dev):
    """Best-effort 'safely remove'. Returns True if the stick was powered off
    or detached."""
    os.sync()
    for cmd in (["udisksctl", "power-off", "-b", dev], ["eject", dev]):
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            emit("LOG", "$ " + " ".join(cmd))
            return True
        emit("LOG", f"{cmd[0]} failed: {(r.stderr or r.stdout or '').strip()}")
    name = os.path.basename(os.path.realpath(dev))
    try:                                   # ask the kernel to detach the disk
        with open(f"/sys/block/{name}/device/delete", "w") as f:
            f.write("1\n")
        emit("LOG", f"Detached {dev} via sysfs")
        return True
    except OSError:
        return False


def worker_write(dev, iso, efi, label, force, sha256=None, verify=True,
                 allow_unsupported=False, eject=True):
    if os.geteuid() != 0:
        raise RuntimeError("Writing requires root.")
    STATE.update(modified=False, written=False)
    for p, what in ((iso, "ISO"), (efi, "bootia32.efi")):
        if not os.path.isfile(p):
            raise RuntimeError(f"{what} file not found: {p}")

    known = {d["path"]: d for d in list_devices(include_fixed=True)}
    if dev not in known:
        raise RuntimeError(f"{dev} is not a whole-disk block device.")
    d = known[dev]
    if d["system"]:
        raise RuntimeError(f"{dev} holds system mounts (/, /boot, /home). Refusing.")
    removable = _truthy(d.get("rm")) or _truthy(d.get("hotplug")) or d.get("tran") == "usb"
    if not removable and not force:
        raise RuntimeError(f"{dev} does not look removable. Use --force to override.")
    if os.path.getsize(iso) > d["size"]:
        raise RuntimeError("The ISO is larger than the device.")
    real_iso = os.path.realpath(iso)
    if any(real_iso.startswith(mp.rstrip("/") + "/") for mp in d["mounts"]):
        raise RuntimeError("The ISO file is stored on the device you are about "
                           "to erase. Copy it somewhere else first.")

    label = re.sub(r"[^A-Z0-9_-]", "", label.upper())[:11] or "LINUXLIVE"

    # ---- Everything up to "point of no return" leaves the stick untouched ----
    if sha256:
        step("Verifying ISO checksum")
        prog = Progress(os.path.getsize(iso))
        actual = sha256_file(iso, prog)
        prog.finish()
        if actual != sha256.lower():
            raise RuntimeError(
                "ISO checksum does not match - the download is damaged or "
                f"incomplete. Download it again.\n  expected {sha256.lower()}\n"
                f"  actual   {actual}\nNothing was changed on the stick.")
        emit("LOG", "ISO checksum OK")

    tmp = tempfile.mkdtemp(prefix="bootia32-")
    usb_mp, iso_mp = os.path.join(tmp, "usb"), os.path.join(tmp, "iso")
    os.makedirs(usb_mp)
    os.makedirs(iso_mp)
    mounted = []
    try:
        step("Checking the ISO")
        run(["mount", "-o", "loop,ro", iso, iso_mp])
        mounted.append(iso_mp)
        total, too_big, kind, warns = inspect_iso(iso_mp)
        if too_big:
            names = ", ".join(f"{r} ({human(sz)})" for r, sz in too_big[:3])
            raise RuntimeError(
                "FAT32 cannot hold files of 4 GiB or more, and this ISO has "
                f"{len(too_big)}: {names}.\nNothing was changed on the stick.")
        if total > d["size"] - 2 * 1024 * 1024:
            raise RuntimeError("The ISO contents do not fit on this device.\n"
                               "Nothing was changed on the stick.")
        if kind != "debian":
            msg = (KIND_TEXT[kind] + ". The bundled loader is only known to "
                   "work with Debian/Ubuntu-based live ISOs.")
            if not allow_unsupported:
                raise RuntimeError(msg + "\nNothing was changed on the stick. "
                                   "Enable 'Allow unrecognized ISO' "
                                   "(--allow-unsupported) to write it anyway.")
            warns.append(msg)
        for w in warns:
            emit("WARN", w)
        emit("LOG", f"ISO looks fine: {human(total)} of files ({kind} live ISO)"
             if kind == "debian" else f"ISO accepted: {human(total)} of files")

        # ---- Point of no return: from here on the stick is modified ----
        step("Unmounting existing partitions")
        for mp in sorted(d["mounts"], key=len, reverse=True):
            run(["umount", mp])

        step("Creating GPT partition table")
        STATE["modified"] = True            # from here on the stick is changed
        run(["wipefs", "-a", dev])
        run(["parted", "-s", dev, "mklabel", "gpt",
             "mkpart", "ESP", "fat32", "1MiB", "100%", "set", "1", "esp", "on"])
        subprocess.run(["partprobe", dev], capture_output=True)
        subprocess.run(["udevadm", "settle"], capture_output=True)
        time.sleep(1)
        part = partition_path(dev)

        step("Formatting FAT32")
        run(["mkfs.vfat", "-F", "32", "-n", label, part])
        subprocess.run(["udevadm", "settle"], capture_output=True)

        run(["mount", part, usb_mp])
        mounted.append(usb_mp)
        st = os.statvfs(usb_mp)
        if total > st.f_bavail * st.f_frsize:
            raise RuntimeError("Not enough free space on the USB stick.")

        step("Copying ISO contents (this takes a while)")
        copy_tree(iso_mp, usb_mp, total)

        step("Installing bootia32.efi")
        boot_dir = os.path.join(usb_mp, "EFI", "boot")   # FAT is case-insensitive
        os.makedirs(boot_dir, exist_ok=True)
        shutil.copyfile(efi, os.path.join(boot_dir, "bootia32.efi"))
        STATE["written"] = True

        if verify:
            step("Verifying: reading everything back from the stick")
            os.sync()
            run(["umount", usb_mp])
            mounted.remove(usb_mp)
            drop_caches(part)
            run(["mount", "-o", "ro", part, usb_mp])
            mounted.append(usb_mp)
            prog = Progress(total + os.path.getsize(efi))
            for p, rel in walk_files(iso_mp):
                if rel.lower() == "efi/boot/bootia32.efi":
                    continue            # replaced by the bundled loader
                compare_files(p, os.path.join(usb_mp, rel), rel, prog)
            compare_files(efi, os.path.join(usb_mp, "EFI", "boot", "bootia32.efi"),
                          "EFI/boot/bootia32.efi", prog)
            prog.finish()
            emit("LOG", "Verification OK: every file on the stick matches the ISO.")
    finally:
        if usb_mp in mounted:
            emit("STEP", "Flushing data and unmounting (do not unplug yet)")
            os.sync()
        for mp in reversed(mounted):
            subprocess.run(["umount", mp], capture_output=True)
        for path in (usb_mp, iso_mp, tmp):   # rmdir only: never deletes contents
            try:
                os.rmdir(path)
            except OSError:
                pass
    if eject:
        emit("STEP", "Ejecting the stick")
        if eject_device(dev):
            emit("DONE", "USB stick is ready and has been ejected. You can unplug it now.")
            return
        emit("WARN", "Could not eject automatically. Use your file manager's "
                     "'Safely remove' before unplugging.")
    emit("DONE", "USB stick is ready. You can remove it safely.")


# --------------------------------------------------------------------------
# GUI (unprivileged; spawns the worker through pkexec)
# --------------------------------------------------------------------------
def run_gui():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        sys.exit("tkinter is missing. Install it with:\n"
                 "  Debian/Ubuntu: sudo apt install python3-tk\n"
                 "  Fedora:        sudo dnf install python3-tkinter\n"
                 "  Arch:          sudo pacman -S tk")

    root = tk.Tk()
    root.title("bootia32 USB creator")
    root.geometry("720x700")
    pad = {"padx": 8, "pady": 4}

    devices = []
    dev_var, iso_var, efi_var, label_var, sha_var = (tk.StringVar() for _ in range(5))
    verify_var = tk.BooleanVar(value=True)
    eject_var = tk.BooleanVar(value=True)
    allow_var = tk.BooleanVar(value=False)
    warns, log_lines = [], []
    run_state = {"running": False, "cancel_file": None, "closing": False,
                 "info": "", "status": ""}
    label_var.set("LINUXLIVE")
    if have_bundled_efi():
        efi_var.set(BUNDLED)

    frm = ttk.Frame(root)
    frm.pack(fill="x", **pad)
    frm.columnconfigure(1, weight=1)

    ttk.Label(frm, text="USB device:").grid(row=0, column=0, sticky="w", **pad)
    combo = ttk.Combobox(frm, textvariable=dev_var, state="readonly")
    combo.grid(row=0, column=1, sticky="ew", **pad)

    def refresh():
        nonlocal devices
        try:
            devices = [d for d in list_devices() if not d["system"]]
        except Exception as e:
            messagebox.showerror("Error", f"Could not list devices:\n{e}")
            return
        combo["values"] = [device_label(d) for d in devices]
        if devices:
            combo.current(0)
        else:
            dev_var.set("")

    ttk.Button(frm, text="Refresh", command=refresh).grid(row=0, column=2, **pad)

    last_dir = {"v": os.path.expanduser("~")}

    def pick(var, title, types):
        p = filedialog.askopenfilename(title=title, filetypes=types,
                                       initialdir=last_dir["v"])
        if p:
            var.set(p)
            last_dir["v"] = os.path.dirname(p) or last_dir["v"]

    ttk.Label(frm, text="ISO file:").grid(row=1, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=iso_var).grid(row=1, column=1, sticky="ew", **pad)
    ttk.Button(frm, text="Browse...",
               command=lambda: pick(iso_var, "Select ISO",
                                    [("ISO images", "*.iso"), ("All", "*")])
               ).grid(row=1, column=2, **pad)

    ttk.Label(frm, text="bootia32.efi:").grid(row=2, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=efi_var).grid(row=2, column=1, sticky="ew", **pad)
    ttk.Button(frm, text="Browse...",
               command=lambda: pick(efi_var, "Select bootia32.efi",
                                    [("EFI files", "*.efi"), ("All", "*")])
               ).grid(row=2, column=2, **pad)

    ttk.Label(frm, text="ISO SHA-256 (optional):").grid(row=3, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=sha_var).grid(row=3, column=1, sticky="ew", **pad)
    ttk.Button(frm, text="Browse...",
               command=lambda: pick(sha_var, "Select the SHA256SUMS file",
                                    [("All files", "*")])
               ).grid(row=3, column=2, **pad)
    ttk.Label(frm, foreground="gray",
              text="Paste the hash (or its line from SHA256SUMS), or browse to the SHA256SUMS file."
              ).grid(row=4, column=1, sticky="w", padx=8)

    ttk.Label(frm, text="Volume label:").grid(row=5, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=label_var, width=14).grid(row=5, column=1, sticky="w", **pad)

    opts = ttk.Frame(frm)
    opts.grid(row=6, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(opts, variable=verify_var,
                    text="Verify the stick after copying (recommended, takes longer)"
                    ).pack(anchor="w")
    ttk.Checkbutton(opts, variable=eject_var,
                    text="Eject the stick when finished").pack(anchor="w")
    ttk.Checkbutton(opts, variable=allow_var,
                    text="Allow ISOs that don't look like Debian/Ubuntu live images"
                    ).pack(anchor="w")

    step_var = tk.StringVar(value="Idle")
    ttk.Label(root, textvariable=step_var).pack(anchor="w", **pad)
    bar = ttk.Progressbar(root, maximum=100)
    bar.pack(fill="x", **pad)

    log = tk.Text(root, height=15, state="disabled", wrap="word")
    log.pack(fill="both", expand=True, **pad)

    def log_add(s):
        line = time.strftime("%H:%M:%S ") + s
        log_lines.append(line)
        log.configure(state="normal")
        log.insert("end", line + "\n")
        log.see("end")
        log.configure(state="disabled")

    q = queue.Queue()
    btns = ttk.Frame(root)
    btns.pack(pady=8)
    btn = ttk.Button(btns, text="Create bootable USB")
    btn.pack(side="left", padx=6)
    cancel_btn = ttk.Button(btns, text="Cancel")
    cancel_btn.pack(side="left", padx=6)
    cancel_btn.state(["disabled"])
    save_btn = ttk.Button(btns, text="Save log...")
    save_btn.pack(side="left", padx=6)

    def finish(kind, msg):
        """Common end-of-run handling. Returns True if the window was closed."""
        run_state["running"] = False
        run_state["status"] = f"{kind}: {msg}"
        btn.state(["!disabled"])
        cancel_btn.state(["disabled"])
        refresh()
        if run_state["closing"]:
            root.destroy()
            return True
        return False

    def pump():
        try:
            while True:
                kind, msg = q.get_nowait()
                if kind == "STEP":
                    step_var.set(msg)
                    log_add(f"== {msg}")
                elif kind == "PROGRESS":
                    bar["value"] = float(msg)
                elif kind in ("LOG", "RAW"):
                    log_add(msg)
                elif kind == "WARN":
                    warns.append(msg)
                    log_add("WARNING: " + msg)
                elif kind == "DONE":
                    step_var.set(msg)
                    log_add(msg)
                    if finish("DONE", msg):
                        return
                    extra = ("\n\nWarnings:\n- " + "\n- ".join(warns)) if warns else ""
                    messagebox.showinfo("Done", msg + extra)
                elif kind == "CANCELLED":
                    step_var.set("Cancelled")
                    bar["value"] = 0
                    log_add(msg)
                    if finish("CANCELLED", msg):
                        return
                    messagebox.showinfo("Cancelled", msg)
                elif kind == "ERROR":
                    step_var.set("Failed")
                    log_add("ERROR: " + msg)
                    if finish("ERROR", msg):
                        return
                    messagebox.showerror("Failed", msg)
        except queue.Empty:
            pass
        root.after(100, pump)

    def reader(proc, cleanup):
        ended = False
        for line in proc.stdout:
            line = line.rstrip("\n")
            kind, _, msg = line.partition(" ")
            if kind in ("STEP", "PROGRESS", "LOG", "WARN", "DONE", "ERROR", "CANCELLED"):
                ended = ended or kind in ("DONE", "ERROR", "CANCELLED")
                q.put((kind, msg))
            else:
                q.put(("RAW", line))
        proc.wait()
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
        if not ended:
            if proc.returncode in (126, 127):
                q.put(("ERROR", "The password prompt was dismissed or "
                                "authentication failed. Nothing was changed."))
            else:
                q.put(("ERROR", "The write process ended unexpectedly "
                                f"(exit code {proc.returncode})."))

    def request_cancel():
        cf = run_state["cancel_file"]
        try:
            open(cf, "w").close()
        except (OSError, TypeError) as e:
            log_add(f"Could not send the cancel request: {e}")
            return False
        cancel_btn.state(["disabled"])
        step_var.set("Cancelling... (waiting for the current step to stop)")
        log_add("Cancel requested")
        return True

    def on_cancel():
        if not run_state["running"]:
            return
        if messagebox.askyesno(
                "Cancel", "Stop now?\n\nIf the stick has already been erased it "
                "will be left incomplete until you run this tool again.",
                icon="warning"):
            request_cancel()

    def on_close():
        if not run_state["running"]:
            root.destroy()
            return
        if messagebox.askyesno("Write in progress",
                               "A write is in progress. Cancel it and close the window?",
                               icon="warning"):
            run_state["closing"] = True
            if not request_cancel():
                run_state["closing"] = False
                return
            step_var.set("Cancelling... the window closes when it is safe.")

    def save_log():
        if not log_lines:
            return messagebox.showinfo("Save log", "There is nothing to save yet.")
        path = filedialog.asksaveasfilename(
            title="Save log", defaultextension=".txt",
            initialfile=time.strftime("bootia32-usb-log-%Y%m%d-%H%M%S.txt"),
            filetypes=[("Text files", "*.txt"), ("All files", "*")])
        if not path:
            return
        try:
            system = platform.platform()
        except Exception:                   # never let the header block a save
            system = "unknown"
        header = ["bootia32 USB creator - log",
                  "Saved:   " + time.strftime("%Y-%m-%d %H:%M:%S"),
                  "System:  " + system,
                  "Python:  " + platform.python_version(),
                  "Session: " + (run_state["info"] or "(no write started)"),
                  "Result:  " + (run_state["status"] or "(not finished)"),
                  "-" * 60]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(header + log_lines) + "\n")
        except OSError as e:
            return messagebox.showerror("Save log", f"Could not save the log:\n{e}")
        messagebox.showinfo("Save log", f"Log saved to\n{path}")

    def start():
        if run_state["running"]:
            return
        i = combo.current()
        if i < 0:
            return messagebox.showwarning("Missing", "Select a USB device.")
        if not iso_var.get() or not os.path.isfile(iso_var.get()):
            return messagebox.showwarning("Missing", "Select a valid ISO file.")
        efi = efi_var.get()
        if efi != BUNDLED and not os.path.isfile(efi):
            return messagebox.showwarning("Missing", "Select a valid bootia32.efi.")
        sha = None
        if sha_var.get().strip():
            sha = resolve_sha256(sha_var.get(), iso_var.get())
            if not sha:
                return messagebox.showwarning(
                    "Checksum",
                    "Could not find a SHA-256 hash for "
                    f"{os.path.basename(iso_var.get())} in what you entered.\n"
                    "Paste the 64-character hash, or leave the field empty.")
        miss = missing_tools()
        if miss:
            return messagebox.showerror(
                "Missing tools",
                "These programs are required but not installed:\n  "
                + ", ".join(miss) + "\n\nDebian/Ubuntu: sudo apt install "
                "parted dosfstools util-linux")
        d = devices[i]
        if not messagebox.askyesno(
                "Confirm - data will be erased",
                f"ALL DATA on\n\n{device_label(d)}\n\nwill be permanently erased.\n\nContinue?",
                icon="warning"):
            return
        stage = None
        try:
            stage = tempfile.mkdtemp(prefix="bootia32-run-")
            if efi == BUNDLED:
                efi = stage_bundled_efi(stage)
            cancel_file = os.path.join(stage, "cancel")
            cmd = [worker_python(), "-c", own_source(), "write",
                   "--device", d["path"], "--iso", iso_var.get(),
                   "--efi", efi, "--label", label_var.get(), "--yes",
                   "--cancel-file", cancel_file]
            if sha:
                cmd += ["--sha256", sha]
            if not verify_var.get():
                cmd.append("--no-verify")
            if not eject_var.get():
                cmd.append("--no-eject")
            if allow_var.get():
                cmd.append("--allow-unsupported")
            if os.geteuid() != 0:
                if not shutil.which("pkexec"):
                    raise RuntimeError("Install polkit (pkexec) or run this "
                                       "app with sudo.")
                cmd = ["pkexec"] + cmd
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except Exception as e:
            if stage:
                shutil.rmtree(stage, ignore_errors=True)
            return messagebox.showerror("Error", str(e))
        if log_lines:
            log_add("=" * 40)
        run_state.update(
            running=True, cancel_file=cancel_file, status="",
            info=f"device={device_label(d)}; iso={iso_var.get()} "
                 f"({human(os.path.getsize(iso_var.get()))}); "
                 f"sha256 check={'yes' if sha else 'no'}; "
                 f"verify={verify_var.get()}; eject={eject_var.get()}; "
                 f"allow unrecognized={allow_var.get()}")
        warns.clear()
        bar["value"] = 0
        btn.state(["disabled"])
        cancel_btn.state(["!disabled"])
        step_var.set("Waiting for authentication...")
        threading.Thread(target=reader, args=(proc, stage), daemon=True).start()

    btn.configure(command=start)
    cancel_btn.configure(command=on_cancel)
    save_btn.configure(command=save_log)
    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    pump()
    root.mainloop()


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="launch the GUI (default)")
    sub.add_parser("list", help="list removable devices")
    w = sub.add_parser("write", help="write the ISO to a device (root)")
    w.add_argument("--device", required=True)
    w.add_argument("--iso", required=True)
    w.add_argument("--efi", help="custom bootia32.efi (default: bundled)")
    w.add_argument("--label", default="LINUXLIVE")
    w.add_argument("--force", action="store_true",
                   help="allow non-removable devices")
    w.add_argument("--sha256", help="expected ISO SHA-256: a hash, a SHA256SUMS "
                   "line, or the path of a SHA256SUMS file")
    w.add_argument("--cancel-file", help="stop cleanly as soon as this file exists "
                   "(used by the GUI's Cancel button)")
    w.add_argument("--no-eject", action="store_true",
                   help="do not power off the stick when finished")
    w.add_argument("--no-verify", action="store_true",
                   help="skip reading the stick back after copying")
    w.add_argument("--allow-unsupported", action="store_true",
                   help="write ISOs that don't look like Debian/Ubuntu live images")
    w.add_argument("--yes", action="store_true",
                   help="confirm that the device will be erased")
    a = ap.parse_args()

    if a.cmd in (None, "gui"):
        return run_gui()
    if a.cmd == "list":
        for d in list_devices():
            print(device_label(d) + ("   [system - hidden in GUI]" if d["system"] else ""))
        return
    if not a.yes:
        ap.error("--yes is required: this ERASES the device")
    global CANCEL_FILE
    CANCEL_FILE = a.cancel_file
    signal.signal(signal.SIGTERM, _on_term)
    tmp_efi = None
    try:
        efi = a.efi
        if not efi:
            efi = tmp_efi = stage_bundled_efi()
        sha = None
        if a.sha256:
            sha = resolve_sha256(a.sha256, a.iso)
            if not sha:
                raise RuntimeError("No SHA-256 hash for this ISO found in --sha256.")
        worker_write(a.device, a.iso, efi, a.label, a.force, sha256=sha,
                     verify=not a.no_verify, allow_unsupported=a.allow_unsupported,
                     eject=not a.no_eject)
    except (Cancelled, KeyboardInterrupt):
        if STATE["written"]:
            what = "All files were copied but not verified."
        elif STATE["modified"]:
            what = ("The stick had already been erased and is incomplete: it "
                    "will not boot. Run the tool again to rewrite it.")
        else:
            what = "Nothing was changed on the stick."
        emit("CANCELLED", "Cancelled. " + what)
        sys.exit(2)
    except Exception as e:
        emit("ERROR", str(e))
        sys.exit(1)
    finally:
        if tmp_efi:
            shutil.rmtree(os.path.dirname(tmp_efi), ignore_errors=True)


if __name__ == "__main__":
    main()
