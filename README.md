# ⬛ iXFS Explorer

> A dark-themed GUI tool for reading, extracting, editing, and rebuilding `.xfs` archive files — built with Python and Tkinter.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=flat-square&logo=python)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey?style=flat-square)
![GUI](https://img.shields.io/badge/GUI-Tkinter-orange?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

---

## 📖 Overview

**iXFS Explorer** is a standalone Python GUI application for working with XFS archive files (a custom compressed container format). It lets you inspect, extract, add, delete, and re-pack files inside an `.xfs` archive — while preserving the original sign block bytes exactly as-is.

---

## ✨ Features

- 📂 **Open & parse** `.xfs` files via file dialog or drag-and-drop
- ⬇ **Extract** individual files or the entire archive contents
- ➕ **Add / inject** new files into an existing XFS archive
- 🗑 **Delete** files from the archive
- 💾 **Save / Save As** — rebuild and write the modified XFS back to disk
- 🔍 **Search / filter** files by name in real time
- 🔃 **Sort** by name, size, packed size, or offset
- ✎ **Edit version number** stored in the sign block
- 🔤 **Edit magic bytes** of the sign block (text or raw hex mode), with presets like `XFS2`, `TSG2`, `\x00×4`
- 📊 **Info panel** — displays version, description, magic bytes (hex + ASCII), file count, total/packed sizes
- 🖱 **Drag-out** rows from the file table to extract directly to your file manager
- 🖱 **Drag-in** files onto the table to inject them into the archive
- ⚠ **Non-destructive** — sign block bytes are always written back verbatim (never re-compressed unless explicitly edited)

---

## 🖥 Screenshot

> *(drop your screenshot here)*

---

## 🚀 Getting Started

### Requirements

- Python **3.8+**
- Standard library only (`tkinter`, `struct`, `zlib`, `zipfile`, `threading`, etc.)
- **No third-party packages required**

> ⚠ On some minimal Linux installs, Tkinter may need to be installed separately:
> ```bash
> sudo apt install python3-tk
> ```

### Installation

```bash
git clone https://github.com/your-username/ixfs-explorer.git
cd ixfs-explorer
python ixfs_explorer_v1_5.py
```

No virtual environment or `pip install` needed.

---

## 🗂 Usage

### Opening a file
- Click **📂 Open XFS** in the toolbar, or
- Drag and drop a `.xfs` file onto the window

### Extracting files
- Select one or more rows → click **⬇ Extract Sel**
- Or click **⬇ Extract All** to dump everything to a folder
- You can also drag a row directly out of the window to extract it

### Adding files
- Click **➕ Add File** and choose a file from disk, or
- Drag a file from your file manager onto the table

### Deleting files
- Select one or more rows → click **🗑 Delete Selected**

### Editing the sign block
| Button | What it does |
|---|---|
| `✎ Set Ver` | Edit the version integer in the sign block |
| `🔤 Edit String` | Change the 4-byte magic string (text or hex mode) |

Presets available: `XFS2`, `TSG2`, null bytes.

### Saving
- **Ctrl+S** / **💾 Save** — overwrite the original file
- **Ctrl+Shift+S** / **💾 Save As...** — write to a new path

---

## 🔬 XFS Format (Brief)

```
[4 bytes]  → offset to packet tail
[N bytes]  → packed file data blobs (zlib-compressed chunks)
[1 byte]   → sign block length
[N bytes]  → compressed sign block (magic, version, description)
[3 bytes]  → compressed metadata length (LE)
[N bytes]  → compressed file metadata (128 bytes per entry)
```

Each file entry in the metadata is `0x80` bytes:
- `0x00–0x6F` — filename (null-terminated, latin-1)
- `0x70` — data offset (uint32 LE)
- `0x74` — status flags (uint32 LE)
- `0x78` — unpacked size (uint32 LE)
- `0x7C` — packed size (uint32 LE)

---

## 📁 Project Structure

```
ixfs-explorer/
└── ixfs_explorer_v1_5.py   # Single-file application
```

---

## 🛠 Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+O` | Open XFS file |
| `Ctrl+S` | Save |
| `Ctrl+Shift+S` | Save As |

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.
