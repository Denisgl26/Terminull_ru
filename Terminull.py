#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Простой графический терминал для работы с COM-портом.

Возможности:
  - выбор COM-порта из списка доступных (с обновлением списка);
  - выбор скорости (baudrate);
  - подключение / отключение;
  - отправка данных в ASCII;
  - приём данных в отдельном потоке (не блокирует интерфейс);
  - вывод принятых/отправленных данных в окно;
  - сохранение содержимого окна (лога) в файл;
  - потоковая запись всех принятых данных сразу на диск (не через GUI),
    что не зависит от того, успевает ли отрисовываться окно;
  - история отправленных строк с автодополнением при вводе.

Требуется библиотека pyserial:
    pip install pyserial
"""

import json
import threading
import queue
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise SystemExit(
        "Не найдена библиотека pyserial.\n"
        "Установите её командой:  pip install pyserial"
    )


BAUDRATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400]

# Интервал обновления окна вывода (мс). Фиксированный, НЕ адаптивный -
# именно попытка опрашивать очередь чаще при большом потоке данных и
# приводила к тому, что GUI-поток переставал успевать обрабатывать
# события окна (клики, перерисовку) и программа выглядела "зависшей".
UI_POLL_INTERVAL_MS = 100

# Сколько символов текста вставляем в окно за один цикл обновления.
# Если данных пришло больше - лишнее (самое старое из накопленного за
# этот цикл) отбрасывается с пометкой, а вся "сырая" информация всё
# равно попадает в файл, если включена потоковая запись.
MAX_CHARS_PER_UI_UPDATE = 20_000

# Максимум символов, которые вообще хранятся в окне (виджет Text
# начинает заметно тормозить, если в нём миллионы символов).
MAX_LOG_CHARS = 300_000

# Файл истории отправленных строк (сохраняется между запусками программы).
HISTORY_FILE = Path.home() / ".serial_terminal_history.json"
MAX_HISTORY_ITEMS = 200
MAX_SUGGESTIONS_SHOWN = 8


class SerialApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Terminull - загрузит все, но это не точно...")
        self.geometry("700x600")
        self.minsize(560, 400)

        # --- состояние ---
        self.serial_port: serial.Serial | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.rx_queue: queue.Queue = queue.Queue()

        # потоковая запись принятых данных в файл (пишет поток чтения
        # напрямую на диск, независимо от GUI и от того, успевает ли
        # отрисовываться окно)
        self.record_file = None
        self.record_lock = threading.Lock()

        # режим отображения принятых данных: "ascii" или "hex"
        self.display_mode_var = tk.StringVar(value="ascii")
        # счётчик байт для переноса строк в HEX-режиме (16 байт на строку)
        self.hex_col = 0

        # история отправленных строк (самые новые - в начале списка)
        self.send_history: list[str] = self._load_history()
        # индекс текущей подсвеченной подсказки в popup-списке (-1 = нет)
        self.suggest_index = -1
        # текст, который был в поле ДО начала навигации по подсказкам -
        # нужен, чтобы восстановить его по Escape
        self.pre_nav_text = ""

        # --- поиск по окну вывода ---
        self.search_var = tk.StringVar()
        self.search_matches: list[tuple] = []  # список (start_index, end_index)
        self.search_current = -1

        self._build_ui()
        self._refresh_ports()

        # периодическая проверка очереди принятых данных - фиксированный
        # интервал, без "разгона" при большом потоке (см. UI_POLL_INTERVAL_MS)
        self.after(UI_POLL_INTERVAL_MS, self._poll_rx_queue)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Ctrl+F - быстрый переход в строку поиска, как в браузере
        self.bind_all("<Control-f>", self._focus_search)

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)

        # --- Порт ---
        ttk.Label(top, text="Порт:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(
            top, textvariable=self.port_var, width=20, state="readonly"
        )
        self.port_combo.grid(row=0, column=1, padx=4)

        self.refresh_btn = ttk.Button(top, text="Обновить", command=self._refresh_ports)
        self.refresh_btn.grid(row=0, column=2, padx=4)

        # --- Скорость ---
        ttk.Label(top, text="Скорость:").grid(row=0, column=3, sticky="w", padx=(12, 0))
        self.baud_var = tk.IntVar(value=9600)
        self.baud_combo = ttk.Combobox(
            top,
            textvariable=self.baud_var,
            values=BAUDRATES,
            width=10,
            state="readonly",
        )
        self.baud_combo.grid(row=0, column=4, padx=4)

        # --- Подключение / отключение ---
        self.connect_btn = ttk.Button(top, text="Подключиться", command=self._toggle_connection)
        self.connect_btn.grid(row=0, column=5, padx=(12, 0))

        # --- Режим отображения принятых данных ---
        ttk.Label(top, text="Вид:").grid(row=0, column=6, sticky="w", padx=(12, 0))
        mode_frame = ttk.Frame(top)
        mode_frame.grid(row=0, column=7, padx=4)
        ttk.Radiobutton(
            mode_frame, text="ASCII", value="ascii",
            variable=self.display_mode_var, command=self._on_display_mode_change,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_frame, text="HEX", value="hex",
            variable=self.display_mode_var, command=self._on_display_mode_change,
        ).pack(side=tk.LEFT)

        # --- Статус ---
        self.status_var = tk.StringVar(value="Не подключено")
        self.status_label = ttk.Label(top, textvariable=self.status_var, foreground="red")
        self.status_label.grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        # --- Строка поиска по окну вывода ---
        search_frame = ttk.Frame(self, padding=(8, 4))
        search_frame.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(search_frame, text="Поиск:").pack(side=tk.LEFT)

        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.search_entry.bind("<KeyRelease>", self._on_search_key_release)
        self.search_entry.bind("<Return>", self._on_search_return)
        self.search_entry.bind("<Shift-Return>", self._on_search_shift_return)
        self.search_entry.bind("<Escape>", self._on_search_escape)

        self.search_count_var = tk.StringVar(value="")
        ttk.Label(search_frame, textvariable=self.search_count_var, width=8, anchor="center").pack(side=tk.LEFT)

        ttk.Button(search_frame, text="▲", width=3, command=self._search_prev).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(search_frame, text="▼", width=3, command=self._search_next).pack(side=tk.LEFT, padx=2)
        ttk.Button(search_frame, text="✕", width=3, command=self._clear_search).pack(side=tk.LEFT)

        # --- Окно вывода ---
        mid = ttk.Frame(self, padding=(8, 0))
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.output_text = tk.Text(mid, wrap="word", state="disabled")
        self.output_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(mid, command=self.output_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.output_text["yscrollcommand"] = scrollbar.set

        # теги подсветки результатов поиска. "search_current" настроен
        # позже, чем "search_match", поэтому имеет более высокий приоритет
        # и виден поверх обычной подсветки при пересечении диапазонов
        self.output_text.tag_configure("search_match", background="#fff59d")
        self.output_text.tag_configure("search_current", background="#ffa726")

        # --- Панель отправки ---
        bottom = ttk.Frame(self, padding=8)
        bottom.pack(side=tk.TOP, fill=tk.X)

        self.send_var = tk.StringVar()
        self.send_entry = ttk.Entry(bottom, textvariable=self.send_var)
        self.send_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.send_entry.bind("<Return>", self._on_entry_return)
        self.send_entry.bind("<KeyRelease>", self._on_entry_key_release)
        self.send_entry.bind("<Down>", self._on_entry_down)
        self.send_entry.bind("<Up>", self._on_entry_up)
        self.send_entry.bind("<Escape>", self._on_entry_escape)
        self.send_entry.bind("<FocusOut>", self._on_entry_focus_out)

        self.append_newline_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bottom, text="добавлять \\n", variable=self.append_newline_var
        ).pack(side=tk.LEFT, padx=6)

        self.send_btn = ttk.Button(bottom, text="Отправить", command=self._send_data, state="disabled")
        self.send_btn.pack(side=tk.LEFT, padx=4)

        # --- Popup со списком автодополнения (создаётся скрытым) ---
        self.suggest_popup = tk.Toplevel(self)
        self.suggest_popup.withdraw()
        self.suggest_popup.overrideredirect(True)
        self.suggest_listbox = tk.Listbox(self.suggest_popup, activestyle="dotbox", exportselection=False)
        self.suggest_listbox.pack(fill=tk.BOTH, expand=True)
        self.suggest_listbox.bind("<Button-1>", self._on_suggest_click)
        self.suggest_listbox.bind("<ButtonRelease-1>", self._on_suggest_click)

        # --- Нижняя панель: сохранение / очистка ---
        bottom2 = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom2.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(bottom2, text="Сохранить лог в файл", command=self._save_log).pack(side=tk.LEFT)
        ttk.Button(bottom2, text="Очистить окно", command=self._clear_log).pack(side=tk.LEFT, padx=6)

        self.pause_display_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bottom2,
            text="Пауза отображения (данные продолжают приниматься)",
            variable=self.pause_display_var,
        ).pack(side=tk.LEFT, padx=(16, 0))

        # --- Панель потоковой записи в файл ---
        bottom3 = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom3.pack(side=tk.TOP, fill=tk.X)

        self.record_btn = ttk.Button(
            bottom3, text="Начать запись всех данных в файл...", command=self._toggle_recording
        )
        self.record_btn.pack(side=tk.LEFT)

        self.record_status_var = tk.StringVar(value="Запись выключена")
        ttk.Label(bottom3, textvariable=self.record_status_var).pack(side=tk.LEFT, padx=8)

        ttk.Button(
            bottom3, text="Очистить историю ввода", command=self._clear_history
        ).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    # История отправленных строк и автодополнение
    # ------------------------------------------------------------------
    def _load_history(self) -> list:
        try:
            if HISTORY_FILE.exists():
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [str(x) for x in data][:MAX_HISTORY_ITEMS]
        except Exception:
            pass
        return []

    def _save_history(self):
        try:
            HISTORY_FILE.write_text(
                json.dumps(self.send_history, ensure_ascii=False, indent=0),
                encoding="utf-8",
            )
        except Exception:
            pass  # сохранение истории не критично - не мешаем работе программы

    def _add_to_history(self, text: str):
        text = text.rstrip("\n")
        if not text:
            return
        if text in self.send_history:
            self.send_history.remove(text)
        self.send_history.insert(0, text)
        del self.send_history[MAX_HISTORY_ITEMS:]
        self._save_history()

    def _clear_history(self):
        if not self.send_history:
            return
        if messagebox.askyesno("Очистить историю", "Удалить всю сохранённую историю ввода?"):
            self.send_history = []
            self._save_history()
            self._hide_suggestions()

    # --- popup автодополнения ---
    def _current_matches(self, typed: str) -> list:
        if not typed:
            return list(self.send_history[:MAX_SUGGESTIONS_SHOWN])
        typed_low = typed.lower()
        return [h for h in self.send_history if typed_low in h.lower()][:MAX_SUGGESTIONS_SHOWN]

    def _show_suggestions(self, matches: list):
        if not matches:
            self._hide_suggestions()
            return

        self.suggest_listbox.delete(0, tk.END)
        for item in matches:
            self.suggest_listbox.insert(tk.END, item)

        x = self.send_entry.winfo_rootx()
        y = self.send_entry.winfo_rooty() + self.send_entry.winfo_height()
        width = self.send_entry.winfo_width()
        row_h = 18
        height = row_h * len(matches) + 4
        self.suggest_popup.geometry(f"{width}x{height}+{x}+{y}")
        self.suggest_popup.deiconify()
        self.suggest_popup.lift()

    def _hide_suggestions(self):
        self.suggest_popup.withdraw()
        self.suggest_index = -1

    def _popup_visible(self) -> bool:
        return self.suggest_popup.winfo_ismapped()

    def _on_entry_key_release(self, event):
        # навигационные и служебные клавиши обрабатываются отдельными
        # хендлерами - здесь их пропускаем, чтобы не сбивать навигацию
        if event.keysym in ("Down", "Up", "Return", "Escape"):
            return

        self.suggest_index = -1
        typed = self.send_var.get()
        matches = self._current_matches(typed)
        if typed and matches:
            self._show_suggestions(matches)
        else:
            self._hide_suggestions()

    def _on_entry_down(self, event):
        if not self._popup_visible():
            matches = self._current_matches(self.send_var.get())
            if not matches:
                return "break"
            self.pre_nav_text = self.send_var.get()
            self._show_suggestions(matches)
            self.suggest_index = 0
        else:
            count = self.suggest_listbox.size()
            if count == 0:
                return "break"
            self.suggest_index = min(self.suggest_index + 1, count - 1)

        self._apply_suggest_highlight()
        return "break"

    def _on_entry_up(self, event):
        if not self._popup_visible():
            return "break"
        self.suggest_index -= 1
        if self.suggest_index < 0:
            # вышли за верх списка - возвращаем то, что было набрано вручную
            self.suggest_index = -1
            self.suggest_listbox.selection_clear(0, tk.END)
            self.send_var.set(self.pre_nav_text)
            self.send_entry.icursor(tk.END)
            return "break"
        self._apply_suggest_highlight()
        return "break"

    def _apply_suggest_highlight(self):
        self.suggest_listbox.selection_clear(0, tk.END)
        self.suggest_listbox.selection_set(self.suggest_index)
        self.suggest_listbox.activate(self.suggest_index)
        self.suggest_listbox.see(self.suggest_index)
        value = self.suggest_listbox.get(self.suggest_index)
        self.send_var.set(value)
        self.send_entry.icursor(tk.END)

    def _on_entry_escape(self, event):
        if self._popup_visible():
            was_navigating = self.suggest_index != -1
            self._hide_suggestions()
            if was_navigating:
                self.send_var.set(self.pre_nav_text)
                self.send_entry.icursor(tk.END)
        return "break"

    def _on_entry_focus_out(self, event):
        # небольшая задержка, чтобы успел обработаться клик по списку
        # подсказок (иначе popup скрывается раньше, чем засчитается клик)
        self.after(150, self._hide_suggestions)

    def _on_suggest_click(self, event):
        index = self.suggest_listbox.nearest(event.y)
        if index < 0 or index >= self.suggest_listbox.size():
            return
        value = self.suggest_listbox.get(index)
        self.send_var.set(value)
        self.send_entry.icursor(tk.END)
        self._hide_suggestions()
        self.send_entry.focus_set()

    def _on_entry_return(self, event):
        if self._popup_visible() and self.suggest_index != -1:
            # подсказка уже подставлена в поле при навигации - просто
            # закрываем список и отправляем выбранную строку
            self._hide_suggestions()
        self._send_data()
        return "break"

    # ------------------------------------------------------------------
    # Поиск по окну вывода (в реальном времени)
    # ------------------------------------------------------------------
    def _focus_search(self, event=None):
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, tk.END)
        return "break"

    def _on_search_key_release(self, event):
        if event.keysym in ("Return", "Escape"):
            return
        self._run_search(auto=False)

    def _on_search_return(self, event):
        self._search_next()
        return "break"

    def _on_search_shift_return(self, event):
        self._search_prev()
        return "break"

    def _on_search_escape(self, event):
        self._clear_search()
        return "break"

    def _run_search(self, auto: bool):
        """Ищет все вхождения в окне вывода и подсвечивает их.

        auto=False - поиск запущен пользователем (новый текст в строке
        поиска, кнопка навигации) - сбрасываем текущий индекс на первое
        совпадение и прокручиваем к нему.
        auto=True  - вызывается автоматически при появлении новых данных
        в окне, пока строка поиска не пуста - список совпадений
        пересчитывается, но текущая позиция и прокрутка не трогаются,
        чтобы не "дёргать" вид, пока пользователь читает.
        """
        query = self.search_var.get()

        self.output_text.tag_remove("search_match", "1.0", tk.END)
        self.output_text.tag_remove("search_current", "1.0", tk.END)

        if not query:
            self.search_matches = []
            self.search_current = -1
            self.search_count_var.set("")
            return

        matches = []
        idx = "1.0"
        while True:
            idx = self.output_text.search(query, idx, stopindex=tk.END, nocase=True)
            if not idx:
                break
            end_idx = f"{idx}+{len(query)}c"
            matches.append((idx, end_idx))
            idx = end_idx

        self.search_matches = matches
        for start, end in matches:
            self.output_text.tag_add("search_match", start, end)

        if not matches:
            self.search_current = -1
        elif auto and 0 <= self.search_current < len(matches):
            pass  # сохраняем текущую позицию при автообновлении
        else:
            self.search_current = 0

        self._apply_current_highlight(jump=not auto)

    def _apply_current_highlight(self, jump: bool):
        self.output_text.tag_remove("search_current", "1.0", tk.END)
        if 0 <= self.search_current < len(self.search_matches):
            start, end = self.search_matches[self.search_current]
            self.output_text.tag_add("search_current", start, end)
            if jump:
                self.output_text.see(start)
            self.search_count_var.set(f"{self.search_current + 1}/{len(self.search_matches)}")
        else:
            self.search_count_var.set("0/0" if self.search_var.get() else "")

    def _search_next(self):
        if not self.search_matches:
            return
        self.search_current = (self.search_current + 1) % len(self.search_matches)
        self._apply_current_highlight(jump=True)

    def _search_prev(self):
        if not self.search_matches:
            return
        self.search_current = (self.search_current - 1) % len(self.search_matches)
        self._apply_current_highlight(jump=True)

    def _clear_search(self):
        self.search_var.set("")
        self._run_search(auto=False)

    # ------------------------------------------------------------------
    # Работа с портами
    # ------------------------------------------------------------------
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports:
            if self.port_var.get() not in ports:
                self.port_var.set(ports[0])
        else:
            self.port_var.set("")

    def _toggle_connection(self):
        if self.serial_port and self.serial_port.is_open:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("Внимание", "Не выбран COM-порт")
            return
        baud = self.baud_var.get()

        try:
            self.serial_port = serial.Serial(port=port, baudrate=baud, timeout=0.1)
        except Exception as exc:
            messagebox.showerror("Ошибка подключения", str(exc))
            self.serial_port = None
            return

        self.stop_event.clear()
        self.hex_col = 0
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()

        self.status_var.set(f"Подключено: {port} @ {baud}")
        self.status_label.configure(foreground="green")
        self.connect_btn.configure(text="Отключиться")
        self.send_btn.configure(state="normal")
        self.port_combo.configure(state="disabled")
        self.baud_combo.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")

        self._log(f"--- Подключено к {port} на {baud} бод ---")

    def _disconnect(self):
        self.stop_event.set()
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=1.0)
            self.reader_thread = None

        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except Exception:
                pass
            self._log("--- Соединение закрыто ---")
            self.serial_port = None

        self._stop_recording()

        self.status_var.set("Не подключено")
        self.status_label.configure(foreground="red")
        self.connect_btn.configure(text="Подключиться")
        self.send_btn.configure(state="disabled")
        self.port_combo.configure(state="readonly")
        self.baud_combo.configure(state="readonly")
        self.refresh_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # Поток чтения данных из порта
    # ------------------------------------------------------------------
    def _read_loop(self):
        """Работает в отдельном потоке. Читает данные из порта,
        сразу пишет их на диск (если включена запись) и кладёт
        в очередь для отображения в окне.

        В очередь всегда кладутся СЫРЫЕ байты (bytes) - форматирование
        под ASCII или HEX происходит уже в GUI-потоке, в момент вывода.
        Так переключение режима отображения применяется сразу, без
        необходимости помнить, в каком виде что было прочитано.

        ВАЖНО: даже когда данные идут непрерывным потоком, поток обязан
        периодически отдавать GIL основному (GUI) потоку, иначе Tkinter
        не успевает обрабатывать события и всё окно выглядит "зависшим".
        Поэтому короткий time.sleep() стоит на КАЖДОЙ итерации, а не
        только когда данных нет.
        """
        while not self.stop_event.is_set():
            try:
                if self.serial_port and self.serial_port.is_open:
                    waiting = self.serial_port.in_waiting
                    data = self.serial_port.read(waiting if waiting else 1)
                    if data:
                        # пишем на диск сразу, в сыром виде - это не
                        # зависит от скорости отрисовки GUI
                        with self.record_lock:
                            if self.record_file is not None:
                                self.record_file.write(data)

                        # для отображения - тоже кладём в очередь (сырые
                        # байты), но если отображение на паузе или в
                        # очереди и так уже много - не раздуваем её
                        if not self.pause_display_var.get():
                            if self.rx_queue.qsize() < 2000:
                                self.rx_queue.put(data)
                else:
                    data = None
            except (serial.SerialException, OSError) as exc:
                self.rx_queue.put(f"\n[Ошибка чтения: {exc}]\n")
                break

            # отдаём управление GUI-потоку. 1-2 мс достаточно, чтобы не
            # "заморозить" интерфейс, и почти не влияет на пропускную
            # способность даже при высоких скоростях порта.
            time.sleep(0.001 if data else 0.02)

    def _format_bytes(self, data: bytes) -> str:
        """Форматирует сырые байты для вывода в окно согласно
        выбранному режиму (ASCII или HEX)."""
        if self.display_mode_var.get() == "hex":
            parts = []
            for b in data:
                parts.append(f"{b:02X}")
                self.hex_col += 1
                parts.append("\n" if self.hex_col % 16 == 0 else " ")
            return "".join(parts)
        return data.decode("ascii", errors="replace")

    def _on_display_mode_change(self):
        # начинаем новую строку в HEX, чтобы не путать данные разных
        # режимов на одной строке
        self.hex_col = 0
        self._log(f"--- Режим отображения: {self.display_mode_var.get().upper()} ---")

    def _poll_rx_queue(self):
        """Вызывается периодически (раз в UI_POLL_INTERVAL_MS) из
        главного потока. Забирает НЕ БОЛЬШЕ фиксированного объёма
        данных за один раз, чтобы не заблокировать GUI надолго, даже
        если реально накопилось гораздо больше."""
        raw_chunks = []
        total_len = 0
        dropped = 0
        control_messages = []
        try:
            while True:
                item = self.rx_queue.get_nowait()
                if isinstance(item, bytes):
                    if total_len < MAX_CHARS_PER_UI_UPDATE:
                        raw_chunks.append(item)
                        total_len += len(item)
                    else:
                        dropped += len(item)
                else:
                    # служебное текстовое сообщение (например, об ошибке)
                    control_messages.append(item)
        except queue.Empty:
            pass

        if raw_chunks:
            formatted = self._format_bytes(b"".join(raw_chunks))
            self._log(formatted, prefix="", newline=False)
        if dropped:
            self._log(
                f"\n[... пропущено {dropped} байт в окне (слишком быстрый поток); "
                f"полные данные см. в файле записи, если она включена ...]\n"
            )
        for msg in control_messages:
            self._log(msg, prefix="", newline=False)

        # если строка поиска не пуста и в окно что-то добавилось -
        # пересчитываем совпадения (без прокрутки, см. _run_search)
        if self.search_var.get() and (raw_chunks or dropped or control_messages):
            self._run_search(auto=True)

        self.after(UI_POLL_INTERVAL_MS, self._poll_rx_queue)

    # ------------------------------------------------------------------
    # Отправка данных
    # ------------------------------------------------------------------
    def _send_data(self):
        if not (self.serial_port and self.serial_port.is_open):
            messagebox.showwarning("Внимание", "Порт не подключен")
            return

        original = self.send_var.get()
        if not original:
            return

        text = original
        if self.append_newline_var.get():
            text += "\n"

        try:
            self.serial_port.write(text.encode("ascii", errors="replace"))
        except Exception as exc:
            messagebox.showerror("Ошибка отправки", str(exc))
            return

        self._log(text, prefix="TX: ")
        self._add_to_history(original)
        self.send_var.set("")
        self._hide_suggestions()

        if self.search_var.get():
            self._run_search(auto=True)

    # ------------------------------------------------------------------
    # Лог / вывод
    # ------------------------------------------------------------------
    def _log(self, text, prefix="", newline=True):
        self.output_text.configure(state="normal")
        if prefix:
            self.output_text.insert(tk.END, prefix)
        self.output_text.insert(tk.END, text)
        if newline and not text.endswith("\n"):
            self.output_text.insert(tk.END, "\n")

        # защита от разрастания виджета: если текста накопилось больше лимита,
        # обрезаем самые старые символы, иначе Text начинает заметно тормозить
        full_len = len(self.output_text.get("1.0", tk.END))
        if full_len > MAX_LOG_CHARS:
            cut_to = full_len - MAX_LOG_CHARS
            self.output_text.delete("1.0", f"1.0+{cut_to}c")

        self.output_text.see(tk.END)
        self.output_text.configure(state="disabled")

    def _clear_log(self):
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", tk.END)
        self.output_text.configure(state="disabled")
        self.hex_col = 0
        self._run_search(auto=False)

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Текстовый файл", "*.txt"), ("Все файлы", "*.*")],
            title="Сохранить лог",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.output_text.get("1.0", tk.END))
        except Exception as exc:
            messagebox.showerror("Ошибка сохранения", str(exc))
            return
        messagebox.showinfo("Сохранено", f"Лог сохранён в файл:\n{path}")

    # ------------------------------------------------------------------
    # Потоковая запись всех принятых данных в файл (без участия GUI)
    # ------------------------------------------------------------------
    def _toggle_recording(self):
        if self.record_file is not None:
            self._stop_recording()
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Лог", "*.log"), ("Все файлы", "*.*")],
            title="Файл для потоковой записи принятых данных",
        )
        if not path:
            return

        try:
            f = open(path, "ab", buffering=0)  # без буферизации - пишем сразу
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return

        with self.record_lock:
            self.record_file = f
        self.record_status_var.set(f"Запись идёт: {path}")
        self.record_btn.configure(text="Остановить запись")

    def _stop_recording(self):
        with self.record_lock:
            if self.record_file is not None:
                try:
                    self.record_file.close()
                except Exception:
                    pass
                self.record_file = None
        self.record_status_var.set("Запись выключена")
        self.record_btn.configure(text="Начать запись всех данных в файл...")

    # ------------------------------------------------------------------
    def _on_close(self):
        self._disconnect()
        self._save_history()
        self.destroy()


if __name__ == "__main__":
    app = SerialApp()
    app.mainloop()