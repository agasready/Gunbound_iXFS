#!/usr/bin/env python3
"""
iXFS Explorer — Python Edition
Supports: open, extract, add file, delete file, save XFS
Fix: save preserves original sign block bytes (edited string) exactly as-is
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import struct, zlib, os, zipfile, io, threading, tempfile

# ─────────────────────────────────────────────
#  XFS PARSER / BUILDER
# ─────────────────────────────────────────────

def zlib_decompress(data: bytes) -> bytes:
    return zlib.decompress(data)

def zlib_compress(data: bytes) -> bytes:
    return zlib.compress(data, level=6)

def parse_xfs(data: bytes):
    """
    Returns dict:
      files        : list of dicts {name, pos, status, unpacked, packed}
      sign_raw     : raw bytes of sign block (length-prefixed, NOT decompressed)
                     — stored verbatim so save never touches it
      sign_dec     : decompressed sign block bytes (for info display)
      version      : int or None
      description  : str or None
      magic_bytes  : bytes (first 4 of decompressed sign block)
      magic_str    : str representation of those 4 bytes
      magic_hex    : hex string "XX XX XX XX"
      magic_is_printable : bool — whether magic looks like normal ASCII text
    """
    if len(data) < 4:
        raise ValueError("File too small")

    packet_tail_offset = struct.unpack_from('<I', data, 0)[0]
    if packet_tail_offset >= len(data):
        raise ValueError("Corrupt XFS: bad packet tail offset")

    sign_block_len = data[packet_tail_offset]
    packet_info_offset = packet_tail_offset + sign_block_len + 1

    if packet_info_offset + 3 > len(data):
        raise ValueError("Corrupt XFS: truncated")

    # Store the ENTIRE sign block raw (1-byte length + compressed data)
    # This is what we ALWAYS write back unchanged on save
    sign_raw = bytes(data[packet_tail_offset : packet_tail_offset + 1 + sign_block_len])

    # meta length (3 bytes LE)
    meta_len = (data[packet_info_offset]
                | (data[packet_info_offset + 1] << 8)
                | (data[packet_info_offset + 2] << 16))
    meta_start = packet_info_offset + 3

    compressed_meta = data[meta_start : meta_start + meta_len]
    meta = zlib_decompress(compressed_meta)

    # Parse sign block for display
    version = None
    description = None
    magic_bytes = b'\x00' * 4
    sign_dec = b''
    try:
        sign_data = bytes(data[packet_tail_offset + 1 : packet_tail_offset + 1 + sign_block_len])
        sign_dec = zlib_decompress(sign_data)
        magic_bytes = sign_dec[:4]
        if len(sign_dec) >= 8:
            version = struct.unpack_from('<I', sign_dec, 4)[0]
        if len(sign_dec) > 0x1C:
            end = 0x1C
            while end < len(sign_dec) and sign_dec[end] != 0:
                end += 1
            try:
                description = sign_dec[0x1C:end].decode('latin-1')
            except Exception:
                description = None
    except Exception:
        pass

    # Build human-readable magic info
    magic_hex = ' '.join(f'{b:02X}' for b in magic_bytes)
    try:
        magic_str = magic_bytes.decode('ascii')
        magic_is_printable = all(0x20 <= b < 0x7F for b in magic_bytes)
    except Exception:
        magic_str = repr(magic_bytes)[2:-1]
        magic_is_printable = False

    FILE_INFO_LEN = 0x80
    num_files = len(meta) // FILE_INFO_LEN
    files = []
    for i in range(num_files):
        off = i * FILE_INFO_LEN
        name_end = 0
        while name_end < 0x70 and meta[off + name_end] != 0:
            name_end += 1
        name = meta[off:off + name_end].decode('latin-1', errors='replace')
        pos      = struct.unpack_from('<I', meta, off + 0x70)[0]
        status   = struct.unpack_from('<I', meta, off + 0x74)[0]
        unpacked = struct.unpack_from('<I', meta, off + 0x78)[0]
        packed   = struct.unpack_from('<I', meta, off + 0x7C)[0]
        files.append({'name': name, 'pos': pos, 'status': status,
                      'unpacked': unpacked, 'packed': packed,
                      'new_data': None, 'modified': False, 'deleted': False})

    return {
        'files': files,
        'sign_raw': sign_raw,       # ← verbatim, never re-compressed
        'sign_dec': sign_dec,
        'version': version,
        'description': description,
        'magic_bytes': magic_bytes,
        'magic_str': magic_str,
        'magic_hex': magic_hex,
        'magic_is_printable': magic_is_printable,
    }


def decompress_file_data(data: bytes, pos: int, unpacked: int, packed: int) -> bytes:
    chunk = data[pos:pos + packed]
    if len(chunk) == 0:
        return b''

    dv = memoryview(chunk)
    init_size = struct.unpack_from('<H', chunk, 0)[0]
    src = 3 if (unpacked == init_size) else 0

    output_parts = []
    current_size = 0

    while current_size < unpacked:
        if src + 5 > len(chunk):
            break

        header_size = None
        for hdr in range(5, 9):
            if src + hdr + 1 < len(chunk):
                b0, b1 = chunk[src + hdr], chunk[src + hdr + 1]
                if b0 == 0x78 and b1 in (0x01, 0x9C, 0xDA):
                    header_size = hdr
                    break
        if header_size is None:
            break

        if header_size == 5:
            chunk_len = (chunk[src] | (chunk[src+1] << 8) | (chunk[src+2] << 16))
        else:
            chunk_len = len(chunk) - src - header_size

        if chunk_len <= 0 or src + header_size + chunk_len > len(chunk):
            chunk_len = len(chunk) - src - header_size

        try:
            dec = zlib_decompress(bytes(chunk[src + header_size: src + header_size + chunk_len]))
            output_parts.append(dec)
            current_size += len(dec)
        except Exception:
            break

        src += header_size + chunk_len

    return b''.join(output_parts)


def compress_new_file(raw: bytes) -> bytes:
    """Compress raw bytes into a single XFS chunk."""
    compressed = zlib_compress(raw)
    chunk_len = len(compressed)
    header = bytes([
        chunk_len & 0xFF,
        (chunk_len >> 8) & 0xFF,
        (chunk_len >> 16) & 0xFF,
        0, 0
    ])
    return header + compressed


def build_xfs(original_data: bytes, xfs_info: dict, files: list,
              progress_cb=None) -> bytes:
    """
    Rebuild XFS.
    Key rule: xfs_info['sign_raw'] is written VERBATIM — never re-compressed.
    This preserves any edited magic string / bytes exactly as found.

    progress_cb(pct: int, label: str) — optional, called during build.
    """
    def _prog(pct, label):
        if progress_cb:
            progress_cb(pct, label)

    active = [f for f in files if not f.get('deleted', False)]
    total  = len(active)

    # ── 1. lay out file data
    _prog(0, 'Collecting file data...')
    file_data_list = []
    offset = 4  # first 4 bytes = pointer to packet tail

    for i, f in enumerate(active):
        if f['new_data'] is not None:
            chunk = f['new_data']
        else:
            chunk = bytes(original_data[f['pos'] : f['pos'] + f['packed']])
        file_data_list.append({'file': f, 'chunk': chunk, 'offset': offset})
        offset += len(chunk)
        _prog(int(i / max(total, 1) * 55), f'Packing  {f["name"]}  ({i+1}/{total})')

    packet_tail_offset = offset

    # ── 2. build file metadata
    _prog(60, 'Building metadata...')
    FILE_INFO_LEN = 0x80
    meta_buf = bytearray(FILE_INFO_LEN * len(active))
    for i, item in enumerate(file_data_list):
        off = i * FILE_INFO_LEN
        name_bytes = item['file']['name'].encode('latin-1', errors='replace')
        meta_buf[off:off + min(len(name_bytes), 0x70)] = name_bytes[:0x70]
        struct.pack_into('<I', meta_buf, off + 0x70, item['offset'])
        struct.pack_into('<I', meta_buf, off + 0x74, item['file'].get('status', 1))
        struct.pack_into('<I', meta_buf, off + 0x78, item['file']['unpacked'])
        struct.pack_into('<I', meta_buf, off + 0x7C, len(item['chunk']))

    _prog(72, 'Compressing metadata...')
    compressed_meta = zlib_compress(bytes(meta_buf))
    meta_len = len(compressed_meta)

    # ── 3. sign block: use VERBATIM original bytes (never re-compress)
    sign_raw = xfs_info['sign_raw']

    # ── 4. assemble output
    _prog(80, 'Assembling output...')
    out = bytearray()
    out += struct.pack('<I', packet_tail_offset)

    total_chunks = len(file_data_list)
    for i, item in enumerate(file_data_list):
        out += item['chunk']
        _prog(80 + int(i / max(total_chunks, 1) * 15),
              f'Writing  {item["file"]["name"]}  ({i+1}/{total_chunks})')

    _prog(96, 'Writing sign block...')
    out += sign_raw

    _prog(98, 'Finalising...')
    out += bytes([meta_len & 0xFF, (meta_len >> 8) & 0xFF, (meta_len >> 16) & 0xFF])
    out += compressed_meta

    _prog(100, 'Done')
    return bytes(out)


# ─────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────

BG       = '#0a0c10'
BG2      = '#0f1218'
BG3      = '#151a22'
BG4      = '#1c2230'
BORDER   = '#1e2a3a'
BORDER2  = '#263344'
ACCENT   = '#00d9ff'
GREEN    = '#00ff88'
RED      = '#ff4466'
YELLOW   = '#ffcc00'
TEXT     = '#c8d8ea'
TEXT2    = '#7a9ab8'
TEXT3    = '#4a6a88'
FONT_M   = ('Courier New', 10)
FONT_MB  = ('Courier New', 10, 'bold')
FONT_UI  = ('Segoe UI', 10)
FONT_UIB = ('Segoe UI', 10, 'bold')
FONT_SM  = ('Courier New', 9)


class IXFSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('iXFS Explorer v1.5')
        self.geometry('1100x640')
        self.configure(bg=BG)
        self.minsize(1000, 500)

        # state
        self.xfs_data    = None    # original bytes
        self.xfs_info    = None    # parsed info dict
        self.files       = []      # working list
        self.file_path   = None
        self.modified    = False
        self._sort_col   = 'name'
        self._sort_asc   = True
        self._filter     = ''
        # Drag-out state
        self._drag_start_iid = None
        self._drag_start_xy  = (0, 0)
        self._drag_active    = False

        self._build_ui()
        self._set_status('Ready — drop or open a .xfs file')
        self._setup_drag_in()

        # Keyboard shortcuts
        self.bind('<Control-s>', lambda e: self._save_xfs())
        self.bind('<Control-S>', lambda e: self._save_xfs_as())  # Ctrl+Shift+S
        self.bind('<Control-o>', lambda e: self._open_file())

    # ── UI BUILD ──────────────────────────────

    def _build_ui(self):
        self._build_titlebar()
        self._build_toolbar()
        self._build_main()
        self._build_statusbar()

    def _build_titlebar(self):
        bar = tk.Frame(self, bg=BG2, height=44)
        bar.pack(fill='x')
        bar.pack_propagate(False)

        tk.Label(bar, text='⬛ iXFS Explorer v1.5', bg=BG2, fg=ACCENT,
                 font=('Courier New', 13, 'bold')).pack(side='left', padx=16)

        self._lbl_file = tk.Label(bar, text='', bg=BG2, fg=TEXT3, font=FONT_SM)
        self._lbl_file.pack(side='left')

        right = tk.Frame(bar, bg=BG2)
        right.pack(side='right', padx=16)

        self._lbl_ver   = self._stat_label(right, 'Ver', '—')
        self._lbl_files = self._stat_label(right, 'Files', '0')
        self._lbl_total = self._stat_label(right, 'Total', '0')
        self._lbl_pack  = self._stat_label(right, 'Packed', '0')

    def _stat_label(self, parent, title, val):
        f = tk.Frame(parent, bg=BG2)
        f.pack(side='left', padx=8)
        tk.Label(f, text=title, bg=BG2, fg=TEXT3, font=FONT_SM).pack(side='left', padx=(0, 4))
        lbl = tk.Label(f, text=val, bg=BG2, fg=ACCENT, font=FONT_MB)
        lbl.pack(side='left')
        return lbl

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BG3, height=42)
        bar.pack(fill='x')
        bar.pack_propagate(False)

        def btn(text, cmd, accent=False, danger=False):
            fg = ACCENT if accent else (RED if danger else TEXT)
            bg_n = BG4
            b = tk.Button(bar, text=text, command=cmd, bg=bg_n, fg=fg,
                           font=FONT_UI, relief='flat', padx=10, pady=4,
                           activebackground=BG3, activeforeground=ACCENT,
                           cursor='hand2', bd=0)
            b.pack(side='left', padx=3, pady=5)
            return b

        self._btn_open    = btn('📂 Open XFS',       self._open_file, accent=True)
        self._sep(bar)
        self._btn_ex_sel  = btn('⬇ Extract Sel',    self._extract_selected)
        self._btn_ex_all  = btn('⬇ Extract All',    self._extract_all)
        self._sep(bar)
        self._btn_add     = btn('➕ Add File',        self._add_file_dialog)
        self._btn_del     = btn('🗑 Delete Selected', self._delete_selected, danger=True)
        self._sep(bar)
        self._btn_save    = btn('💾 Save',            self._save_xfs,    accent=True)
        self._btn_saveas  = btn('💾 Save As...',      self._save_xfs_as)

        # version editor
        self._sep(bar)
        tk.Label(bar, text='Ver:', bg=BG3, fg=TEXT3, font=FONT_SM).pack(side='left')
        self._ver_var = tk.StringVar()
        self._ver_entry = tk.Entry(bar, textvariable=self._ver_var, width=6,
                                   bg=BG2, fg=ACCENT, font=FONT_MB, bd=0,
                                   insertbackground=ACCENT, justify='center')
        self._ver_entry.pack(side='left', padx=2)
        self._ver_entry.bind('<Return>', lambda e: self._change_version())
        self._btn_set_ver = btn('✎ Set Ver', self._change_version)
        self._sep(bar)
        self._btn_edit_str = btn('🔤 Edit String', self._open_edit_string_dialog)

        tk.Frame(bar, bg=BG3).pack(side='left', fill='x', expand=True)

        self._set_controls_enabled(False)

    def _sep(self, parent):
        tk.Frame(parent, bg=BORDER2, width=1).pack(side='left', fill='y',
                                                   pady=6, padx=4)

    def _build_main(self):
        self._main_frame = tk.Frame(self, bg=BG)
        self._main_frame.pack(fill='both', expand=True)

        # Drop zone
        self._drop_frame = tk.Frame(self._main_frame, bg=BG)
        self._drop_frame.pack(fill='both', expand=True)
        tk.Label(self._drop_frame, text='\n\n\n⬛\n\nDrop .xfs file here\nor click Open XFS',
                 bg=BG, fg=TEXT3, font=('Segoe UI', 14), justify='center').pack(expand=True)

        # Bind drag-in ke drop zone (setelah XFS dibuka, tree juga accept)
        self._drop_frame.bind('<ButtonRelease-1>', lambda e: None)  # placeholder, wire di _setup_drag_in

        # File table
        self._table_frame = tk.Frame(self._main_frame, bg=BG)

        # Info banner (shown when edited string detected)
        self._info_banner = tk.Frame(self._table_frame, bg='#1a1200', pady=4)
        self._info_lbl = tk.Label(self._info_banner, text='', bg='#1a1200',
                                  fg=YELLOW, font=FONT_SM, anchor='w', padx=12)
        self._info_lbl.pack(fill='x')

        cols = ('check', 'name', 'size', 'packed', 'ratio', 'offset', 'actions')
        self._tree = ttk.Treeview(self._table_frame, columns=cols, show='headings',
                                  selectmode='extended')

        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('Treeview', background=BG, foreground=TEXT,
                        fieldbackground=BG, bordercolor=BORDER, rowheight=26,
                        font=FONT_SM)
        style.configure('Treeview.Heading', background=BG3, foreground=TEXT3,
                        bordercolor=BORDER2, font=('Courier New', 9, 'bold'))
        style.map('Treeview', background=[('selected', BG4)],
                  foreground=[('selected', ACCENT)])

        self._tree.heading('check',   text='✓', anchor='center')
        self._tree.heading('name',    text='Name ↕',   command=lambda: self._sort('name'))
        self._tree.heading('size',    text='Size ↕',   command=lambda: self._sort('size'))
        self._tree.heading('packed',  text='Packed ↕', command=lambda: self._sort('packed'))
        self._tree.heading('ratio',   text='Ratio',    anchor='center')
        self._tree.heading('offset',  text='Offset ↕', command=lambda: self._sort('offset'))
        self._tree.heading('actions', text='',         anchor='center')

        self._tree.column('check',   width=30,  stretch=False, anchor='center')
        self._tree.column('name',    width=260, stretch=True)
        self._tree.column('size',    width=110, stretch=False, anchor='e')
        self._tree.column('packed',  width=110, stretch=False, anchor='e')
        self._tree.column('ratio',   width=70,  stretch=False, anchor='center')
        self._tree.column('offset',  width=100, stretch=False)
        self._tree.column('actions', width=80,  stretch=False, anchor='center')

        sb = ttk.Scrollbar(self._table_frame, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self._tree.pack(fill='both', expand=True)

        # Hint drag-in ke table area
        self._drag_hint = tk.Label(
            self._table_frame,
            text='💡 Drag file ke sini untuk add/replace  •  Drag baris ke luar window untuk extract',
            bg=BG3, fg=TEXT3, font=('Courier New', 8), pady=3, anchor='center')
        self._drag_hint.pack(fill='x')

        # Drag-in hint bar (shown at bottom of table)
        self._drag_hint = tk.Label(
            self._table_frame,
            text='⬆  Drop files here to inject into XFS  •  Drag rows out to extract',
            bg='#0a1a0a', fg='#2a6a2a', font=('Courier New', 8),
            pady=3, anchor='center')
        self._drag_hint.pack(fill='x', side='bottom')

        self._tree.bind('<Double-1>', self._on_double_click)
        self._tree.bind('<Button-1>', self._on_click)

        # ── Drag-out: press → move → release ──────────────────────────────
        self._tree.bind('<ButtonPress-1>',   self._start_drag_out)
        self._tree.bind('<B1-Motion>',        self._motion_drag_out)
        self._tree.bind('<ButtonRelease-1>', self._end_drag)

        # ── Drag-in: accept file drops onto the table ──────────────────────
        # (tkinterdnd2 bindings set up later in _setup_drag_in)

    def _build_statusbar(self):
        bar = tk.Frame(self, bg=BG2, height=32)
        bar.pack(fill='x', side='bottom')
        bar.pack_propagate(False)

        # ── Search bar (kiri bawah)
        tk.Label(bar, text='🔍', bg=BG2, fg=TEXT3, font=FONT_SM).pack(side='left', padx=(8, 0))
        self._search_var = tk.StringVar()
        self._search_var.trace_add('write', lambda *_: self._render_list())
        self._search_entry = tk.Entry(
            bar, textvariable=self._search_var, width=22,
            bg=BG4, fg=TEXT, font=FONT_SM, bd=0,
            insertbackground=ACCENT, relief='flat')
        self._search_entry.pack(side='left', padx=(4, 0), ipady=3)

        # Clear button untuk search
        def _clear_search():
            self._search_var.set('')
            self._search_entry.focus_set()
        self._btn_clear_search = tk.Button(
            bar, text='✕', command=_clear_search,
            bg=BG2, fg=TEXT3, font=('Courier New', 8),
            relief='flat', bd=0, padx=4,
            activebackground=BG3, activeforeground=RED,
            cursor='hand2')
        self._btn_clear_search.pack(side='left', padx=(0, 8))

        tk.Frame(bar, bg=BORDER2, width=1).pack(side='left', fill='y', pady=4)

        # ── Status label
        self._lbl_status = tk.Label(bar, text='Ready', bg=BG2, fg=TEXT2,
                                    font=FONT_SM, anchor='w')
        self._lbl_status.pack(side='left', padx=10)

        # ── Kanan
        self._lbl_sel = tk.Label(bar, text='0 selected', bg=BG2, fg=TEXT3,
                                 font=FONT_SM)
        self._lbl_sel.pack(side='right', padx=12)

        self._lbl_mod = tk.Label(bar, text='● Modified (unsaved)', bg=BG2,
                                 fg=YELLOW, font=FONT_SM)

    # ── ENABLE / DISABLE CONTROLS ─────────────

    def _set_controls_enabled(self, enabled: bool):
        state = 'normal' if enabled else 'disabled'
        for b in (self._btn_ex_sel, self._btn_ex_all, self._btn_add,
                  self._btn_del, self._btn_save, self._btn_saveas,
                  self._btn_set_ver, self._btn_edit_str):
            b.config(state=state)
        self._ver_entry.config(state=state)

    # ── DRAG-IN (drop files FROM Explorer INTO XFS) ───────────────────────

    def _setup_drag_in(self):
        """
        Register drop target on the treeview so dragging files from
        Windows Explorer onto the table adds them to the XFS.
        Uses tkinter's built-in TkDND if available, otherwise falls back
        to a Ctrl+V / paste-path workaround hint.
        We use the Windows-specific DnD via the 'tkinterdnd2' package when
        available, with a graceful fallback.
        """
        try:
            # Try tkinterdnd2 (optional dependency)
            import tkinterdnd2  # noqa
            self._dnd_available = True
        except ImportError:
            self._dnd_available = False

        if self._dnd_available:
            try:
                self._tree.drop_target_register('DND_Files')
                self._tree.dnd_bind('<<Drop>>', self._on_dnd_drop)
                self._drop_frame_lbl = None  # will be set after first XFS open
            except Exception:
                self._dnd_available = False

    def _on_dnd_drop(self, event):
        """Handle files dropped via tkinterdnd2."""
        # Parse the dropped paths — tkinterdnd2 gives them as a Tcl list
        raw = event.data
        paths = self.tk.splitlist(raw)
        self._add_files_batch(list(paths))

    def _on_drop_zone_drop(self, event):
        """Handle drop on the initial drop zone (before XFS opened)."""
        raw = event.data
        paths = self.tk.splitlist(raw)
        # Filter for .xfs files first
        xfs_files = [p for p in paths if p.lower().endswith('.xfs')]
        other_files = [p for p in paths if not p.lower().endswith('.xfs')]
        if xfs_files:
            self._load_xfs(xfs_files[0])
            if other_files:
                self._add_files_batch(other_files)
        elif other_files:
            messagebox.showinfo('Drop', 'Drop a .xfs file first to open it,\nthen drop other files to inject them.')

    def _add_files_batch(self, paths: list):
        """Add/replace multiple files into the open XFS from a list of paths."""
        if not self.xfs_info:
            messagebox.showinfo('No XFS', 'Open an XFS file first before dropping files into it.')
            return
        added = replaced = 0
        for path in paths:
            if not os.path.isfile(path):
                continue
            fname = os.path.basename(path)
            try:
                with open(path, 'rb') as fh:
                    raw = fh.read()
            except Exception as e:
                messagebox.showerror('Read Error', f'Could not read {fname}:\n{e}')
                continue
            chunk = compress_new_file(raw)
            existing = next((f for f in self.files if f['name'] == fname and not f['deleted']), None)
            if existing:
                existing['new_data'] = chunk
                existing['unpacked'] = len(raw)
                existing['packed']   = len(chunk)
                existing['modified'] = True
                replaced += 1
            else:
                self.files.append({
                    'name': fname, 'pos': 0, 'status': 1,
                    'unpacked': len(raw), 'packed': len(chunk),
                    'new_data': chunk, 'modified': True, 'deleted': False
                })
                added += 1
        if added + replaced:
            self._mark_modified()
            self._update_stats()
            self._render_list()
            parts = []
            if added:    parts.append(f'{added} added')
            if replaced: parts.append(f'{replaced} replaced')
            self._set_status(f'Drop inject: {", ".join(parts)}  — save to apply')

    # ── DRAG-OUT (drag files FROM table TO Explorer) ──────────────────────

    def _start_drag_out(self, event):
        """
        Called on ButtonPress on the treeview.
        Stores the pressed iid so _motion_drag_out can start the drag.
        """
        iid = self._tree.identify_row(event.y)
        self._drag_start_iid  = iid
        self._drag_start_xy   = (event.x, event.y)
        self._drag_active     = False

    def _motion_drag_out(self, event):
        """Start actual OS-level drag when mouse moves > threshold."""
        if not self._drag_start_iid:
            return
        dx = abs(event.x - self._drag_start_xy[0])
        dy = abs(event.y - self._drag_start_xy[1])
        if dx < 6 and dy < 6:
            return
        if self._drag_active:
            return
        self._drag_active = True
        iid = self._drag_start_iid

        # Collect files: if the dragged row is in a multi-selection, drag all selected
        sel = list(self._tree.selection())
        if iid not in sel:
            sel = [iid]

        names = set()
        for s in sel:
            v = self._tree.item(s, 'values')
            if v:
                names.add(v[1].lstrip('★ '))
        files = [f for f in self.files if f['name'] in names and not f['deleted']]
        if not files:
            self._drag_active = False
            return

        # Extract to a temp dir and hand off to OS DnD
        self._do_drag_out(files)

    def _do_drag_out(self, files: list):
        """Extract files to temp dir then initiate OS drag."""
        tmp_dir = tempfile.mkdtemp(prefix='ixfs_drag_')
        extracted_paths = []
        for f in files:
            try:
                if f['new_data']:
                    raw = decompress_file_data(bytes(f['new_data']), 0,
                                               f['unpacked'], len(f['new_data']))
                else:
                    raw = decompress_file_data(self.xfs_data, f['pos'],
                                               f['unpacked'], f['packed'])
                dest = os.path.join(tmp_dir, f['name'])
                with open(dest, 'wb') as fh:
                    fh.write(raw)
                extracted_paths.append(dest)
            except Exception as ex:
                print(f'Drag-out skip {f["name"]}: {ex}')

        if not extracted_paths:
            self._drag_active = False
            return

        self._set_status(f'Dragging out {len(extracted_paths)} file(s) — drop to a folder')

        # Use platform-specific mechanism to initiate OS file drag
        if sys.platform == 'win32':
            self._win32_drag_files(extracted_paths)
        else:
            # Fallback: open temp folder so user can copy manually
            self._open_folder(tmp_dir)
            self._set_status(f'Extracted {len(extracted_paths)} file(s) to temp folder — {tmp_dir}')

        self._drag_active = False

    def _win32_drag_files(self, paths: list):
        """
        Initiate a Windows OLE shell drag-drop for a list of file paths.
        Uses a small inline PowerShell script to avoid needing pywin32.
        Falls back to opening the folder if PS is unavailable.
        """
        try:
            import ctypes, ctypes.wintypes

            # Build a null-separated file list for the PS script
            ps_paths = '","'.join(p.replace('\\', '\\\\') for p in paths)
            ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
$files = @("{ps_paths}")
$data = New-Object System.Windows.Forms.DataObject
$col  = New-Object System.Collections.Specialized.StringCollection
foreach ($f in $files) {{ $col.Add($f) | Out-Null }}
$data.SetFileDropList($col)
[System.Windows.Forms.Clipboard]::SetDataObject($data, $true)
"""
            # Put files on clipboard as file drop (Ctrl+V in Explorer pastes them)
            subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps_script],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                timeout=8
            )
            names = ', '.join(os.path.basename(p) for p in paths)
            self._set_status(f'Files on clipboard — paste in Explorer (Ctrl+V): {names}')
            messagebox.showinfo(
                'Files ready',
                f'{len(paths)} file(s) copied to clipboard as file drop.\n'
                f'Go to your destination folder in Windows Explorer and press Ctrl+V to paste:\n\n'
                + '\n'.join(os.path.basename(p) for p in paths)
            )
        except Exception as e:
            print(f'Win32 drag fallback: {e}')
            self._open_folder(os.path.dirname(paths[0]))

    def _open_folder(self, folder: str):
        if sys.platform == 'win32':
            os.startfile(folder)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        else:
            subprocess.Popen(['xdg-open', folder])

    def _end_drag(self, event):
        self._drag_start_iid = None
        self._drag_active    = False

    # ── OPEN ──────────────────────────────────

    def _open_file(self):
        path = filedialog.askopenfilename(
            title='Open XFS File',
            filetypes=[('XFS Files', '*.xfs'), ('All Files', '*.*')]
        )
        if not path:
            return
        self._load_xfs(path)

    def _load_xfs(self, path: str):
        try:
            with open(path, 'rb') as f:
                data = f.read()
            info = parse_xfs(data)
        except Exception as e:
            messagebox.showerror('Error', f'Failed to parse XFS:\n{e}')
            return

        self.xfs_data = data
        self.xfs_info = info
        self.files    = info['files']
        self.file_path = path
        self.modified  = False

        # Show table
        self._drop_frame.pack_forget()
        self._table_frame.pack(fill='both', expand=True)

        # Info banner for edited / non-standard string
        magic = info['magic_bytes']
        magic_hex = info['magic_hex']
        magic_str = info['magic_str']
        is_printable = info['magic_is_printable']

        if is_printable:
            banner_txt = f'ℹ️  XFS String: "{magic_str}"  (hex: {magic_hex})'
        else:
            banner_txt = f'⚠️  Edited/non-standard XFS String detected — hex: {magic_hex}  (raw bytes, not printable ASCII)'

        self._info_lbl.config(text=banner_txt)
        self._info_banner.pack(fill='x', before=self._tree)

        name = os.path.basename(path)
        self._lbl_file.config(text=f'— {name}')
        ver = info['version']
        self._lbl_ver.config(text=str(ver) if ver is not None else '—')
        self._ver_var.set(str(ver) if ver is not None else '0')

        self._set_controls_enabled(True)
        self._update_stats()
        self._render_list()
        self._set_status(f'Loaded: {name} — {len(self.files)} files')

        # Show modified label hidden initially
        self._lbl_mod.pack_forget()

    # ── RENDER LIST ───────────────────────────

    def _render_list(self):
        q = self._search_var.get().lower() if hasattr(self, '_search_var') else ''
        files = [f for f in self.files if not f['deleted'] and q in f['name'].lower()]

        key_map = {'name': 'name', 'size': 'unpacked',
                   'packed': 'packed', 'offset': 'pos'}
        col = key_map.get(self._sort_col, 'name')
        files.sort(key=lambda f: f[col], reverse=not self._sort_asc)

        self._tree.delete(*self._tree.get_children())
        for f in files:
            ratio = int((1 - f['packed'] / max(f['unpacked'], 1)) * 100)
            star  = '★ ' if f['modified'] else ''
            tag   = 'mod' if f['modified'] else ''
            self._tree.insert('', 'end', iid=str(f['pos']) + f['name'],
                              values=(
                                  '☐',
                                  star + f['name'],
                                  f"{f['unpacked']:,} B",
                                  f"{f['packed']:,} B",
                                  f'{ratio}%',
                                  f"0x{f['pos']:05X}",
                                  '[Extract]'
                              ), tags=(tag,))

        self._tree.tag_configure('mod', foreground=YELLOW)
        self._update_sel_label()

    def _on_click(self, event):
        region = self._tree.identify_region(event.x, event.y)
        col    = self._tree.identify_column(event.x)
        iid    = self._tree.identify_row(event.y)
        if region == 'cell' and col == '#7' and iid:  # actions column
            self._extract_by_iid(iid)

    def _on_double_click(self, event):
        iid = self._tree.identify_row(event.y)
        if iid:
            self._extract_by_iid(iid)

    def _extract_by_iid(self, iid):
        vals = self._tree.item(iid, 'values')
        name = vals[1].lstrip('★ ')
        f = next((x for x in self.files if x['name'] == name and not x['deleted']), None)
        if f:
            self._do_extract([f])

    def _sort(self, col):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._render_list()

    # ── STATS ─────────────────────────────────

    def _update_stats(self):
        active = [f for f in self.files if not f['deleted']]
        total  = sum(f['unpacked'] for f in active)
        packed = sum(f['packed']   for f in active)
        self._lbl_files.config(text=str(len(active)))
        self._lbl_total.config(text=self._fmt_bytes(total))
        self._lbl_pack.config(text=self._fmt_bytes(packed))

    def _update_sel_label(self):
        n = len(self._tree.selection())
        self._lbl_sel.config(text=f'{n} selected')

    # ── EXTRACT ───────────────────────────────

    def _extract_selected(self):
        sel_iids = self._tree.selection()
        names = set()
        for iid in sel_iids:
            v = self._tree.item(iid, 'values')
            names.add(v[1].lstrip('★ '))
        files = [f for f in self.files if f['name'] in names and not f['deleted']]
        self._do_extract(files)

    def _extract_all(self):
        self._do_extract([f for f in self.files if not f['deleted']])

    def _do_extract(self, files: list):
        if not files:
            return
        if len(files) == 1:
            f = files[0]
            dest = filedialog.asksaveasfilename(
                initialfile=f['name'],
                title='Save extracted file',
                filetypes=[('All Files', '*.*')]
            )
            if not dest:
                return
            try:
                if f['new_data']:
                    # re-decompress from our chunk
                    raw = decompress_file_data(
                        bytes(f['new_data']), 0, f['unpacked'], len(f['new_data']))
                else:
                    raw = decompress_file_data(self.xfs_data, f['pos'],
                                               f['unpacked'], f['packed'])
                with open(dest, 'wb') as fh:
                    fh.write(raw)
                self._set_status(f'Extracted: {f["name"]}')
            except Exception as e:
                messagebox.showerror('Error', f'Extract failed:\n{e}')
        else:
            dest = filedialog.asksaveasfilename(
                initialfile='extracted.zip',
                title='Save as ZIP',
                filetypes=[('ZIP Files', '*.zip')]
            )
            if not dest:
                return
            try:
                with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in files:
                        try:
                            if f['new_data']:
                                raw = decompress_file_data(
                                    bytes(f['new_data']), 0, f['unpacked'], len(f['new_data']))
                            else:
                                raw = decompress_file_data(self.xfs_data, f['pos'],
                                                           f['unpacked'], f['packed'])
                            zf.writestr(f['name'], raw)
                        except Exception as ex:
                            print(f'Skip {f["name"]}: {ex}')
                self._set_status(f'Extracted {len(files)} files → {os.path.basename(dest)}')
            except Exception as e:
                messagebox.showerror('Error', f'ZIP creation failed:\n{e}')

    # ── ADD FILE ──────────────────────────────

    def _add_file_dialog(self):
        src = filedialog.askopenfilename(title='Select file to add to XFS')
        if not src:
            return

        default_name = os.path.basename(src)
        target_name = simpledialog.askstring(
            'Target filename in XFS',
            'Enter the filename as it will appear inside the XFS:\n'
            '(If it matches an existing entry, it replaces it)',
            initialvalue=default_name,
            parent=self
        )
        if not target_name:
            return
        target_name = target_name.strip()

        try:
            with open(src, 'rb') as fh:
                raw = fh.read()
        except Exception as e:
            messagebox.showerror('Error', str(e))
            return

        chunk = compress_new_file(raw)

        existing = next((f for f in self.files if f['name'] == target_name
                         and not f['deleted']), None)
        if existing:
            existing['new_data'] = chunk
            existing['unpacked'] = len(raw)
            existing['packed']   = len(chunk)
            existing['modified'] = True
            action = 'Replaced'
        else:
            self.files.append({
                'name': target_name, 'pos': 0, 'status': 1,
                'unpacked': len(raw), 'packed': len(chunk),
                'new_data': chunk, 'modified': True, 'deleted': False
            })
            action = 'Added'

        self._mark_modified()
        self._update_stats()
        self._render_list()
        self._set_status(f'{action}: {target_name}')

    # ── DELETE ────────────────────────────────

    def _delete_selected(self):
        sel_iids = self._tree.selection()
        if not sel_iids:
            messagebox.showinfo('Delete', 'No files selected.')
            return

        names = set()
        for iid in sel_iids:
            v = self._tree.item(iid, 'values')
            names.add(v[1].lstrip('★ '))

        if not messagebox.askyesno('Confirm Delete',
            f'Delete {len(names)} file(s) from XFS?\n\n' + '\n'.join(sorted(names))):
            return

        for f in self.files:
            if f['name'] in names:
                f['deleted'] = True

        self._mark_modified()
        self._update_stats()
        self._render_list()
        self._set_status(f'Deleted {len(names)} file(s) — save to apply')

    # ── SAVE XFS ──────────────────────────────

    def _save_xfs(self):
        """Save — overwrite the original file directly (no dialog)."""
        if not self.file_path:
            # No path yet (shouldn't normally happen), fall back to Save As
            self._save_xfs_as()
            return
        self._do_save(self.file_path)

    def _save_xfs_as(self):
        """Save As — pick a new destination."""
        initial = os.path.basename(self.file_path) if self.file_path else 'modified.xfs'
        dest = filedialog.asksaveasfilename(
            title='Save XFS As',
            initialfile=initial,
            filetypes=[('XFS Files', '*.xfs'), ('All Files', '*.*')]
        )
        if not dest:
            return
        self._do_save(dest)

    def _do_save(self, dest: str):
        """Shared save logic — builds XFS in background thread with progress dialog."""

        # Snapshot state for the worker thread
        xfs_data  = self.xfs_data
        xfs_info  = self.xfs_info
        files     = self.files

        # ── Progress dialog
        dlg = SaveProgressDialog(self)

        result = {'out': None, 'error': None}

        def worker():
            try:
                def on_progress(pct, label):
                    # Schedule UI update on main thread
                    self.after(0, dlg.update, pct, label)

                out = build_xfs(xfs_data, xfs_info, files, progress_cb=on_progress)
                result['out'] = out
            except Exception as e:
                result['error'] = str(e)
            finally:
                self.after(0, _on_done)

        def _on_done():
            dlg.destroy()
            if result['error']:
                messagebox.showerror('Save Error', result['error'])
                self._set_status('Save failed.')
                return
            out = result['out']
            try:
                with open(dest, 'wb') as fh:
                    fh.write(out)
                # Update current path (important after Save As)
                self.file_path = dest
                self._lbl_file.config(text=f'— {os.path.basename(dest)}')
                self.modified = False
                self._lbl_mod.pack_forget()
                size_str = self._fmt_bytes(len(out))
                self._set_status(f'Saved: {os.path.basename(dest)}  ({size_str})')
                messagebox.showinfo('Saved',
                    f'XFS saved successfully!\n\n'
                    f'{dest}\n'
                    f'Size: {size_str}')
            except Exception as e:
                messagebox.showerror('Write Error', str(e))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        # Block until dialog closed (worker calls dlg.destroy via after())
        self.wait_window(dlg)

    # ── CHANGE VERSION ────────────────────────

    def _change_version(self):
        try:
            new_ver = int(self._ver_var.get())
        except ValueError:
            messagebox.showerror('Error', 'Invalid version number')
            return

        info = self.xfs_info
        sign_dec = info.get('sign_dec', b'')
        if len(sign_dec) < 8:
            messagebox.showerror('Error', 'No version info in this XFS')
            return

        old_ver = info['version']

        # Patch decompressed sign block: uint32 at offset 4
        raw = bytearray(sign_dec)
        struct.pack_into('<I', raw, 4, new_ver)

        # Re-compress and rebuild sign_raw
        recompressed = zlib_compress(bytes(raw))
        new_sign_raw = bytes([len(recompressed)]) + recompressed

        info['sign_raw'] = new_sign_raw
        info['sign_dec'] = bytes(raw)
        info['version']  = new_ver

        self._lbl_ver.config(text=str(new_ver))
        self._mark_modified()
        self._set_status(f'Version changed: {old_ver} → {new_ver}')

    # ── EDIT STRING ───────────────────────────

    def _open_edit_string_dialog(self):
        if not self.xfs_info:
            return
        dlg = EditStringDialog(self, self.xfs_info)
        self.wait_window(dlg)
        if dlg.result_sign_raw is not None:
            self.xfs_info['sign_raw']  = dlg.result_sign_raw
            self.xfs_info['sign_dec']  = dlg.result_sign_dec
            self.xfs_info['magic_bytes'] = dlg.result_magic
            mb = dlg.result_magic
            self.xfs_info['magic_hex'] = ' '.join(f'{b:02X}' for b in mb)
            try:
                self.xfs_info['magic_str'] = mb.decode('ascii')
                self.xfs_info['magic_is_printable'] = all(0x20 <= b < 0x7F for b in mb)
            except Exception:
                self.xfs_info['magic_str'] = repr(mb)[2:-1]
                self.xfs_info['magic_is_printable'] = False

            # Refresh banner
            is_p = self.xfs_info['magic_is_printable']
            mhex = self.xfs_info['magic_hex']
            mstr = self.xfs_info['magic_str']
            if is_p:
                self._info_lbl.config(
                    text=f'ℹ️  XFS String: "{mstr}"  (hex: {mhex})',
                    fg=ACCENT)
                self._info_banner.config(bg='#0a1018')
                self._info_lbl.config(bg='#0a1018')
            else:
                self._info_lbl.config(
                    text=f'⚠️  Edited/non-standard XFS String — hex: {mhex}  (non-printable)',
                    fg=YELLOW)
                self._info_banner.config(bg='#1a1200')
                self._info_lbl.config(bg='#1a1200')

            self._mark_modified()
            self._set_status(f'String updated → {mhex}')

    # ── DRAG-IN SETUP ─────────────────────────

    def _setup_drag_in(self):
        """
        Setup drag-and-drop masuk menggunakan tkinterdnd2 jika tersedia,
        fallback ke override Windows drag message via Tk internals.
        """
        try:
            # Coba tkinterdnd2 dulu (jika user sudah install)
            from tkinterdnd2 import DND_FILES
            self._drop_frame.drop_target_register(DND_FILES)
            self._drop_frame.dnd_bind('<<Drop>>', self._on_dnd_drop_xfs)
            self._tree.drop_target_register(DND_FILES)
            self._tree.dnd_bind('<<Drop>>', self._on_dnd_drop_files)
            self._dnd_available = True
        except Exception:
            # Fallback: gunakan tk.dnd internal + Windows HWND message hook
            self._dnd_available = False
            self._setup_win32_drop()

    def _setup_win32_drop(self):
        """Win32 drag-drop fallback tanpa tkinterdnd2."""
        try:
            import ctypes
            hwnd = self.winfo_id()
            ctypes.windll.shell32.DragAcceptFiles(hwnd, True)
            self.bind('<Configure>', lambda e: None)  # keep hwnd alive
            # Subclass WndProc untuk tangkap WM_DROPFILES (0x0233)
            WndProcType = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_long,
                                             ctypes.c_uint, ctypes.c_long, ctypes.c_long)
            old_wnd_proc_ptr = ctypes.windll.user32.GetWindowLongW(hwnd, -4)
            def new_wnd_proc(hwnd_, msg, wparam, lparam):
                if msg == 0x0233:  # WM_DROPFILES
                    self._handle_wm_dropfiles(wparam)
                    return 0
                return ctypes.windll.user32.CallWindowProcW(
                    old_wnd_proc_ptr, hwnd_, msg, wparam, lparam)
            self._wnd_proc_cb = WndProcType(new_wnd_proc)
            ctypes.windll.user32.SetWindowLongW(hwnd, -4,
                self._wnd_proc_cb)
        except Exception:
            pass  # Linux/Mac — drag-in via toolbar button saja

    def _handle_wm_dropfiles(self, hDrop):
        """Proses WM_DROPFILES dari Win32."""
        try:
            import ctypes
            nFiles = ctypes.windll.shell32.DragQueryFileW(hDrop, 0xFFFFFFFF, None, 0)
            paths = []
            for i in range(nFiles):
                buf = ctypes.create_unicode_buffer(260)
                ctypes.windll.shell32.DragQueryFileW(hDrop, i, buf, 260)
                paths.append(buf.value)
            ctypes.windll.shell32.DragFinish(hDrop)
            if not self.xfs_data:
                # Drop ke drop zone — coba buka sebagai XFS
                xfs_paths = [p for p in paths if p.lower().endswith('.xfs')]
                if xfs_paths:
                    self.after(0, self._load_xfs, xfs_paths[0])
            else:
                self.after(0, self._inject_file_paths, paths)
        except Exception as e:
            self.after(0, self._set_status, f'Drop error: {e}')

    def _on_dnd_drop_xfs(self, event):
        """tkinterdnd2: drop ke drop zone — buka XFS."""
        paths = self._parse_dnd_data(event.data)
        xfs_paths = [p for p in paths if p.lower().endswith('.xfs')]
        if xfs_paths:
            self._load_xfs(xfs_paths[0])
        else:
            messagebox.showinfo('Drag & Drop', 'Drop file .xfs untuk dibuka.')

    def _on_dnd_drop_files(self, event):
        """tkinterdnd2: drop ke tree — inject file ke XFS."""
        if not self.xfs_data:
            return
        paths = self._parse_dnd_data(event.data)
        self._inject_file_paths(paths)

    @staticmethod
    def _parse_dnd_data(data: str) -> list:
        """Parse dnd data string jadi list path."""
        import re
        # tkinterdnd2 format: '{path with spaces}' atau 'path1 path2'
        paths = re.findall(r'\{([^}]+)\}|(\S+)', data)
        return [a or b for a, b in paths]

    def _inject_file_paths(self, paths: list):
        """Inject daftar file path ke XFS (replace jika sudah ada)."""
        if not paths:
            return
        added = replaced = 0
        for fpath in paths:
            if not os.path.isfile(fpath):
                continue
            fname = os.path.basename(fpath)
            try:
                with open(fpath, 'rb') as fh:
                    raw = fh.read()
            except Exception as e:
                self._set_status(f'Gagal baca {fname}: {e}')
                continue
            chunk = compress_new_file(raw)
            existing = next((f for f in self.files
                             if f['name'] == fname and not f['deleted']), None)
            if existing:
                existing['new_data'] = chunk
                existing['unpacked'] = len(raw)
                existing['packed']   = len(chunk)
                existing['modified'] = True
                replaced += 1
            else:
                self.files.append({
                    'name': fname, 'pos': 0, 'status': 1,
                    'unpacked': len(raw), 'packed': len(chunk),
                    'new_data': chunk, 'modified': True, 'deleted': False
                })
                added += 1
        if added + replaced:
            self._mark_modified()
            self._update_stats()
            self._render_list()
            parts = []
            if added:    parts.append(f'+{added} added')
            if replaced: parts.append(f'{replaced} replaced')
            self._set_status(f'Drag-in: {", ".join(parts)}')

    # ── DRAG-OUT ──────────────────────────────

    def _drag_out_start(self, event):
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self._dragging_out = False
        self._drag_ghost   = None

    def _drag_out_motion(self, event):
        if not self.xfs_data:
            return
        dx = abs(event.x_root - self._drag_start_x)
        dy = abs(event.y_root - self._drag_start_y)
        if dx > 8 or dy > 8:
            if not self._dragging_out:
                # Pastikan ada item yang terpilih
                sel = self._tree.selection()
                if sel:
                    self._dragging_out = True
                    self._show_drag_ghost(event)
        if self._dragging_out and self._drag_ghost:
            self._drag_ghost.geometry(
                f'+{event.x_root + 12}+{event.y_root + 8}')

    def _drag_out_release(self, event):
        if self._drag_ghost:
            self._drag_ghost.destroy()
            self._drag_ghost = None
        if not self._dragging_out:
            return
        self._dragging_out = False

        # Cek apakah release di LUAR window
        wx = self.winfo_rootx()
        wy = self.winfo_rooty()
        ww = self.winfo_width()
        wh = self.winfo_height()
        x, y = event.x_root, event.y_root
        if wx <= x <= wx + ww and wy <= y <= wy + wh:
            return  # masih di dalam window — batal

        # Extract file yang dipilih ke folder tujuan
        sel_iids = self._tree.selection()
        if not sel_iids:
            return
        names = set()
        for iid in sel_iids:
            v = self._tree.item(iid, 'values')
            names.add(v[1].lstrip('★ '))
        files = [f for f in self.files if f['name'] in names and not f['deleted']]
        if not files:
            return
        if len(files) == 1:
            dest = filedialog.asksaveasfilename(
                initialfile=files[0]['name'],
                title='Extract file ke...',
                filetypes=[('All Files', '*.*')]
            )
            if dest:
                self._extract_to_path(files[0], dest)
        else:
            dest_dir = filedialog.askdirectory(title='Extract ke folder...')
            if dest_dir:
                ok = err = 0
                for f in files:
                    try:
                        self._extract_to_path(f, os.path.join(dest_dir, f['name']))
                        ok += 1
                    except Exception:
                        err += 1
                self._set_status(f'Drag-out: {ok} file extracted{f", {err} gagal" if err else ""}')

    def _show_drag_ghost(self, event):
        """Tampilkan ghost window kecil saat drag."""
        sel = self._tree.selection()
        count = len(sel)
        label = f'📄 {count} file{"s" if count > 1 else ""}' if count > 1 else '📄 drag to extract'
        ghost = tk.Toplevel(self)
        ghost.overrideredirect(True)
        ghost.attributes('-alpha', 0.75)
        ghost.configure(bg=BG4)
        tk.Label(ghost, text=label, bg=BG4, fg=ACCENT,
                 font=FONT_SM, padx=10, pady=4).pack()
        ghost.geometry(f'+{event.x_root + 12}+{event.y_root + 8}')
        ghost.lift()
        self._drag_ghost = ghost

    def _extract_to_path(self, f: dict, dest: str):
        """Extract satu file XFS ke path tujuan."""
        if f['new_data']:
            raw = decompress_file_data(
                bytes(f['new_data']), 0, f['unpacked'], len(f['new_data']))
        else:
            raw = decompress_file_data(self.xfs_data, f['pos'],
                                        f['unpacked'], f['packed'])
        with open(dest, 'wb') as fh:
            fh.write(raw)
        self._set_status(f'Extracted: {f["name"]} → {os.path.basename(dest)}')

    # ── HELPERS ───────────────────────────────

    def _mark_modified(self):
        self.modified = True
        self._lbl_mod.pack(side='left', padx=12)

    def _set_status(self, msg: str):
        self._lbl_status.config(text=msg)

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        if n < 1024:       return f'{n} B'
        if n < 1024**2:    return f'{n/1024:.1f} KB'
        return f'{n/1024**2:.2f} MB'




# ─────────────────────────────────────────────
#  SAVE PROGRESS DIALOG
# ─────────────────────────────────────────────

class SaveProgressDialog(tk.Toplevel):
    """
    Non-closable modal progress dialog shown during XFS save.
    Worker thread calls update(pct, label) via app.after(0, ...).
    Worker destroys this dialog when done.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.title('Saving XFS...')
        self.configure(bg=BG)
        self.resizable(False, False)
        self.protocol('WM_DELETE_WINDOW', lambda: None)   # disable X button
        self.grab_set()
        self.transient(parent)

        W = 440

        # ── Header
        tk.Label(self, text='💾  Saving XFS Archive', bg=BG, fg=ACCENT,
                 font=('Courier New', 12, 'bold')).pack(padx=28, pady=(18, 4), anchor='w')
        tk.Frame(self, bg=BORDER2, height=1).pack(fill='x', padx=28)

        # ── Progress bar (canvas-drawn for full colour control)
        bar_frame = tk.Frame(self, bg=BG, width=W)
        bar_frame.pack(padx=28, pady=(14, 0), fill='x')

        self._bar_canvas = tk.Canvas(bar_frame, height=10, bg=BG4,
                                     highlightthickness=0, bd=0)
        self._bar_canvas.pack(fill='x')
        self._bar_rect = self._bar_canvas.create_rectangle(
            0, 0, 0, 10, fill=ACCENT, outline='')

        # ── Percentage label
        self._pct_var = tk.StringVar(value='0%')
        tk.Label(self, textvariable=self._pct_var, bg=BG, fg=ACCENT,
                 font=('Courier New', 20, 'bold')).pack(pady=(8, 0))

        # ── Detail label (filename / stage)
        self._detail_var = tk.StringVar(value='Starting...')
        tk.Label(self, textvariable=self._detail_var, bg=BG, fg=TEXT3,
                 font=('Courier New', 9), wraplength=W - 30,
                 justify='left').pack(padx=28, pady=(4, 18), anchor='w')

        self.update_idletasks()
        self._bar_canvas.update_idletasks()

        # Centre on parent
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f'{w}x{h}+{pw - w//2}+{ph - h//2}')

    def update(self, pct: int, label: str):
        """Called from main thread via after(0, ...)."""
        pct = max(0, min(100, pct))
        self._pct_var.set(f'{pct}%')
        self._detail_var.set(label)

        # Resize filled rectangle to match percentage
        self._bar_canvas.update_idletasks()
        total_w = self._bar_canvas.winfo_width()
        filled  = int(total_w * pct / 100)

        # Colour: green when done, accent otherwise
        colour = GREEN if pct == 100 else ACCENT
        self._bar_canvas.coords(self._bar_rect, 0, 0, filled, 10)
        self._bar_canvas.itemconfig(self._bar_rect, fill=colour)
        self._bar_canvas.update_idletasks()


# ─────────────────────────────────────────────
#  EDIT STRING DIALOG
# ─────────────────────────────────────────────

class EditStringDialog(tk.Toplevel):
    """
    Modal dialog to edit the 4-byte magic string inside the XFS sign block.
    Two input modes:
      • Text  — type up to 4 ASCII chars (auto-padded with \\x00)
      • Hex   — type exactly 8 hex digits, e.g. "15040500"
    Preview updates live. Apply patches sign_dec offset 0..3, recompresses,
    and returns result_sign_raw / result_sign_dec / result_magic.
    """

    def __init__(self, parent, xfs_info: dict):
        super().__init__(parent)
        self.xfs_info = xfs_info
        self.result_sign_raw = None
        self.result_sign_dec = None
        self.result_magic    = None

        self.title('Edit XFS String')
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        # Current magic
        magic = xfs_info.get('magic_bytes', b'\x00\x00\x00\x00')
        self._orig_magic = magic

        self._mode = tk.StringVar(value='text')  # 'text' or 'hex'

        self._build(magic)
        self._center(parent)

    def _center(self, parent):
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f'+{pw - w//2}+{ph - h//2}')

    def _build(self, magic: bytes):
        pad = dict(padx=18, pady=8)

        # ── Title
        tk.Label(self, text='✎  Edit XFS String', bg=BG, fg=ACCENT,
                 font=('Courier New', 12, 'bold')).pack(fill='x', padx=18, pady=(14, 4))
        tk.Frame(self, bg=BORDER2, height=1).pack(fill='x', padx=18)

        # ── Current value display
        cur_frame = tk.Frame(self, bg=BG3, pady=8)
        cur_frame.pack(fill='x', padx=18, pady=(10, 0))

        cur_hex = ' '.join(f'{b:02X}' for b in magic)
        try:
            cur_str = magic.decode('ascii') if all(0x20 <= b < 0x7F for b in magic) else '(non-printable)'
        except Exception:
            cur_str = '(non-printable)'

        tk.Label(cur_frame, text='Current string:', bg=BG3, fg=TEXT3,
                 font=FONT_SM).grid(row=0, column=0, sticky='w', padx=12)
        tk.Label(cur_frame, text=f'HEX: {cur_hex}', bg=BG3, fg=TEXT2,
                 font=FONT_MB).grid(row=0, column=1, sticky='w', padx=12)
        tk.Label(cur_frame, text=f'ASCII: {cur_str}', bg=BG3, fg=TEXT2,
                 font=FONT_MB).grid(row=0, column=2, sticky='w', padx=12)

        # ── Mode selector
        mode_frame = tk.Frame(self, bg=BG, pady=4)
        mode_frame.pack(fill='x', **pad)
        tk.Label(mode_frame, text='Input mode:', bg=BG, fg=TEXT3,
                 font=FONT_SM).pack(side='left', padx=(0, 10))
        for val, lbl in (('text', 'Text (ASCII)'), ('hex', 'Hex bytes')):
            tk.Radiobutton(mode_frame, text=lbl, variable=self._mode, value=val,
                           bg=BG, fg=TEXT, selectcolor=BG3, activebackground=BG,
                           activeforeground=ACCENT, font=FONT_SM,
                           command=self._on_mode_change).pack(side='left', padx=6)

        # ── Input area
        input_frame = tk.Frame(self, bg=BG)
        input_frame.pack(fill='x', padx=18, pady=4)

        # --- Text mode
        self._text_frame = tk.Frame(input_frame, bg=BG)
        self._text_frame.pack(fill='x')
        tk.Label(self._text_frame, text='String (max 4 chars):', bg=BG, fg=TEXT3,
                 font=FONT_SM).pack(anchor='w')
        self._text_var = tk.StringVar()
        try:
            init_txt = magic.decode('latin-1').rstrip('\x00')
        except Exception:
            init_txt = ''
        self._text_var.set(init_txt)
        self._text_entry = tk.Entry(
            self._text_frame, textvariable=self._text_var, width=10,
            bg=BG2, fg=ACCENT, font=('Courier New', 16, 'bold'), bd=0,
            insertbackground=ACCENT, justify='center')
        self._text_entry.pack(pady=(4, 0))
        self._text_var.trace_add('write', self._on_text_change)
        tk.Label(self._text_frame,
                 text='Shorter strings are zero-padded on the right.',
                 bg=BG, fg=TEXT3, font=('Courier New', 8)).pack(anchor='w', pady=(2,0))

        # --- Hex mode
        self._hex_frame = tk.Frame(input_frame, bg=BG)
        tk.Label(self._hex_frame, text='Hex bytes (exactly 8 hex digits):', bg=BG, fg=TEXT3,
                 font=FONT_SM).pack(anchor='w')
        self._hex_var = tk.StringVar()
        self._hex_var.set(''.join(f'{b:02X}' for b in magic))
        self._hex_entry = tk.Entry(
            self._hex_frame, textvariable=self._hex_var, width=12,
            bg=BG2, fg=GREEN, font=('Courier New', 16, 'bold'), bd=0,
            insertbackground=GREEN, justify='center')
        self._hex_entry.pack(pady=(4, 0))
        self._hex_var.trace_add('write', self._on_hex_change)
        tk.Label(self._hex_frame,
                 text='e.g. "58465332" = XFS2,  "15040500" = edited bytes',
                 bg=BG, fg=TEXT3, font=('Courier New', 8)).pack(anchor='w', pady=(2,0))

        # ── Preview
        tk.Frame(self, bg=BORDER2, height=1).pack(fill='x', padx=18, pady=(8, 0))
        prev_frame = tk.Frame(self, bg=BG3, pady=6)
        prev_frame.pack(fill='x', padx=18, pady=4)
        tk.Label(prev_frame, text='Preview →', bg=BG3, fg=TEXT3,
                 font=FONT_SM).grid(row=0, column=0, padx=12, sticky='w')
        self._prev_hex = tk.Label(prev_frame, text='', bg=BG3, fg=ACCENT, font=FONT_MB)
        self._prev_hex.grid(row=0, column=1, padx=8)
        self._prev_str = tk.Label(prev_frame, text='', bg=BG3, fg=GREEN, font=FONT_MB)
        self._prev_str.grid(row=0, column=2, padx=8)
        self._prev_warn = tk.Label(prev_frame, text='', bg=BG3, fg=RED, font=FONT_SM)
        self._prev_warn.grid(row=1, column=0, columnspan=3, padx=12, sticky='w')

        # ── Buttons
        btn_frame = tk.Frame(self, bg=BG, pady=10)
        btn_frame.pack(fill='x', padx=18, pady=(4, 14))

        def mkbtn(text, cmd, fg=TEXT, accent=False):
            b = tk.Button(btn_frame, text=text, command=cmd,
                          bg=BG4, fg=ACCENT if accent else fg,
                          font=FONT_UI, relief='flat', padx=12, pady=5,
                          activebackground=BG3, activeforeground=ACCENT,
                          cursor='hand2', bd=0)
            b.pack(side='right', padx=4)
            return b

        mkbtn('Cancel', self.destroy)
        self._btn_apply = mkbtn('✔ Apply', self._apply, accent=True)

        # Preset buttons row
        pre_frame = tk.Frame(self, bg=BG, pady=2)
        pre_frame.pack(fill='x', padx=18, pady=(0, 6))
        tk.Label(pre_frame, text='Presets:', bg=BG, fg=TEXT3, font=FONT_SM).pack(side='left')
        for label, val in [('XFS2','XFS2'), ('TSG2','TSG2'), ('\\x00×4','00000000')]:
            is_hex = val.startswith('0') and len(val) == 8
            def make_preset(v, h=is_hex):
                def _set():
                    if h:
                        self._mode.set('hex')
                        self._on_mode_change()
                        self._hex_var.set(v)
                    else:
                        self._mode.set('text')
                        self._on_mode_change()
                        self._text_var.set(v)
                return _set
            tk.Button(pre_frame, text=label, command=make_preset(val, is_hex),
                      bg=BG4, fg=TEXT2, font=('Courier New', 9), relief='flat',
                      padx=8, pady=2, activebackground=BG3, activeforeground=ACCENT,
                      cursor='hand2', bd=0).pack(side='left', padx=4)

        self._on_mode_change()
        self._refresh_preview()

    def _on_mode_change(self):
        mode = self._mode.get()
        if mode == 'text':
            self._hex_frame.pack_forget()
            self._text_frame.pack(fill='x')
            # Sync hex → text when switching
            try:
                hv = self._hex_var.get().strip()
                if len(hv) == 8:
                    b = bytes.fromhex(hv)
                    txt = b.decode('latin-1').rstrip('\x00')
                    if all(0x20 <= x < 0x7F for x in b if x != 0):
                        self._text_var.set(txt)
            except Exception:
                pass
        else:
            self._text_frame.pack_forget()
            self._hex_frame.pack(fill='x')
            # Sync text → hex when switching
            try:
                tv = self._text_var.get()[:4].encode('latin-1')
                tv = tv + b'\x00' * (4 - len(tv))
                self._hex_var.set(''.join(f'{b:02X}' for b in tv))
            except Exception:
                pass
        self._refresh_preview()

    def _on_text_change(self, *_):
        # Clamp to 4 chars
        v = self._text_var.get()
        if len(v) > 4:
            self._text_var.set(v[:4])
            return
        self._refresh_preview()

    def _on_hex_change(self, *_):
        v = self._hex_var.get()
        # Allow only hex chars
        clean = ''.join(c for c in v.upper() if c in '0123456789ABCDEF')
        if len(clean) > 8:
            clean = clean[:8]
        if clean != v.upper():
            self._hex_var.set(clean)
            return
        self._refresh_preview()

    def _get_new_magic(self):
        """Returns (bytes4, error_str_or_None)"""
        mode = self._mode.get()
        if mode == 'text':
            try:
                tv = self._text_var.get()[:4]
                b = tv.encode('latin-1')
                b = b + b'\x00' * (4 - len(b))
                return b, None
            except Exception as e:
                return None, str(e)
        else:
            hv = self._hex_var.get().strip()
            if len(hv) != 8:
                return None, f'Need exactly 8 hex digits (got {len(hv)})'
            try:
                return bytes.fromhex(hv), None
            except Exception as e:
                return None, str(e)

    def _refresh_preview(self):
        magic, err = self._get_new_magic()
        if err or magic is None:
            self._prev_hex.config(text='—')
            self._prev_str.config(text='')
            self._prev_warn.config(text=f'⚠ {err}' if err else '')
            self._btn_apply.config(state='disabled')
            return

        hex_str = ' '.join(f'{b:02X}' for b in magic)
        is_p = all(0x20 <= b < 0x7F for b in magic)
        try:
            asc = magic.decode('ascii') if is_p else '(non-printable bytes)'
        except Exception:
            asc = '(non-printable bytes)'

        self._prev_hex.config(text=f'HEX: {hex_str}', fg=ACCENT)
        self._prev_str.config(text=f'ASCII: {asc}', fg=GREEN if is_p else YELLOW)
        changed = (magic != self._orig_magic)
        if changed:
            ohex = ' '.join(f'{b:02X}' for b in self._orig_magic)
            self._prev_warn.config(
                text=f'Will change from  {ohex}  →  {hex_str}', fg=YELLOW)
        else:
            self._prev_warn.config(text='(same as current — no change)', fg=TEXT3)
        self._btn_apply.config(state='normal')

    def _apply(self):
        magic, err = self._get_new_magic()
        if err or magic is None:
            messagebox.showerror('Error', err or 'Invalid input', parent=self)
            return

        info = self.xfs_info
        sign_dec = info.get('sign_dec', b'')
        if len(sign_dec) < 4:
            messagebox.showerror('Error', 'Sign block too short to patch', parent=self)
            return

        # Patch bytes 0..3 of decompressed sign block
        raw = bytearray(sign_dec)
        raw[0:4] = magic

        # Recompress → new sign_raw
        recompressed = zlib_compress(bytes(raw))
        new_sign_raw = bytes([len(recompressed)]) + recompressed

        self.result_sign_raw = new_sign_raw
        self.result_sign_dec = bytes(raw)
        self.result_magic    = magic

        self.destroy()


if __name__ == '__main__':
    app = IXFSApp()
    app.mainloop()
