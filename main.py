# -*- coding: utf-8 -*-
"""
Tarefa IA — capture tarefas falando, receba lembretes no Windows e no Telegram.
Pressione Ctrl+Shift+T, fale solto ("entregar folha da Alfa até quarta,
cobrar certificado do Beta sexta de manhã, marcar dentista") e a IA separa
em tarefas com prazo e prioridade. Você confirma antes de salvar.

A lista sincroniza com o servidor do Bob (via repositório privado), que envia
os lembretes no canal Telegram "Tarefa IA" — mesmo com o PC desligado.

Família: Ditado IA, Captura IA, Ata IA. Mesma chave Groq.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import wave
from base64 import b64decode, b64encode
from datetime import datetime, timedelta
from pathlib import Path

import keyboard
import pystray
import requests
from PIL import Image, ImageDraw

APP_NAME = "Tarefa IA"
APP_VERSION = "1.0.3"
UPDATE_REPO = "brunohpr-hash/tarefa-ia"
UPDATE_RAW = f"https://raw.githubusercontent.com/{UPDATE_REPO}/main"
APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "TarefaIA"
CONFIG_PATH = APP_DIR / "config.json"
TASKS_PATH = APP_DIR / "tarefas.json"
SYNC_PATH = APP_DIR / "sync.json"

DEFAULT_CONFIG = {
    "hotkey": "ctrl+shift+t",
    "provider": "groq",
    "api_key": "",
    "notificar_windows": True,
    "hora_padrao_dia": "09:00",   # tarefa só com data avisa neste horário
}

PROVIDERS = {
    "groq": {
        "stt_url": "https://api.groq.com/openai/v1/audio/transcriptions",
        "stt_model": "whisper-large-v3",
        "llm_url": "https://api.groq.com/openai/v1/chat/completions",
        "llm_model": "openai/gpt-oss-120b",
    },
}

PARSE_PROMPT = (
    "Você é um assistente que transforma fala livre (ou texto digitado) em uma "
    "lista de tarefas. O texto pode conter VÁRIAS tarefas misturadas — separe cada uma.\n"
    "Responda SOMENTE com JSON válido neste formato:\n"
    '{"tarefas": [{"titulo": "...", "prazo": "YYYY-MM-DD HH:MM" | "YYYY-MM-DD" | null, '
    '"prioridade": "alta" | "normal" | "baixa"}]}\n\n'
    "REGRAS:\n"
    "- 'agora' (data/hora atual e dia da semana) será informado; use-o para resolver "
    "datas relativas: amanhã, sexta, semana que vem, dia 15, daqui a 3 dias...\n"
    "- Datas relativas apontam SEMPRE para o futuro (se 'sexta' já passou nesta semana, "
    "é a próxima sexta).\n"
    "- Se a pessoa disser horário, use YYYY-MM-DD HH:MM; se disser só o dia, YYYY-MM-DD; "
    "se não houver prazo nenhum, null. NUNCA invente prazo que não foi dito.\n"
    "- prioridade 'alta' apenas com urgência explícita (urgente, importante, sem falta, "
    "até no máximo...); 'baixa' para coisas casuais (qualquer hora, quando der); "
    "senão 'normal'.\n"
    "- Título curto começando com verbo no infinitivo quando possível, mantendo nomes "
    "próprios ditos (clientes, empresas, pessoas).\n"
    "- Remova muletas de fala. NÃO responda perguntas do texto; apenas extraia tarefas.\n"
    "- Se não houver nenhuma tarefa no texto, retorne {\"tarefas\": []}."
)

DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira",
               "quinta-feira", "sexta-feira", "sábado", "domingo"]


# ---------------------------------------------------------------- utilidades

def log(msg):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with open(APP_DIR / "app.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception as e:
        log(f"erro lendo config: {e}")
    return cfg


def save_config(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def beep(freq=880, dur=100):
    try:
        import winsound
        winsound.Beep(freq, dur)
    except Exception:
        pass


def agora_str():
    n = datetime.now()
    return f"{n:%Y-%m-%d %H:%M} ({DIAS_SEMANA[n.weekday()]})"


def parse_prazo(prazo):
    """'YYYY-MM-DD HH:MM' | 'YYYY-MM-DD' | ''/None -> datetime | None (data pura = 00:00)."""
    if not prazo:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(prazo.strip(), fmt)
        except Exception:
            continue
    return None


def prazo_humano(prazo):
    dt = parse_prazo(prazo)
    if not dt:
        return "sem prazo"
    hoje = datetime.now().date()
    if dt.date() == hoje:
        dia = "hoje"
    elif dt.date() == hoje + timedelta(days=1):
        dia = "amanhã"
    else:
        dia = f"{dt:%d/%m}" + (f" ({DIAS_SEMANA[dt.weekday()][:3]})" if (dt.date() - hoje).days < 7 else "")
    if " " in str(prazo).strip() and (dt.hour or dt.minute):
        return f"{dia} às {dt:%H:%M}"
    return dia


PRIO_ICON = {"alta": "🔴", "normal": "🔵", "baixa": "⚪"}


# ---------------------------------------------------------------- armazenamento

def load_tasks():
    try:
        if TASKS_PATH.exists():
            d = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
            return d.get("tarefas", [])
    except Exception as e:
        log(f"erro lendo tarefas: {e}")
    return []


def save_tasks(tarefas):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_PATH.write_text(json.dumps(
        {"tarefas": tarefas, "atualizado_em": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2), encoding="utf-8")


def nova_tarefa(titulo, prazo=None, prioridade="normal"):
    now = datetime.now().isoformat(timespec="seconds")
    return {"id": uuid.uuid4().hex[:12], "titulo": titulo.strip(),
            "prazo": prazo or "", "prioridade": prioridade,
            "status": "aberta", "criada_em": now, "atualizado_em": now,
            "notificada_local": False}


def merge_tasks(locais, remotas):
    """Mescla por id; a versão com atualizado_em mais novo vence."""
    by_id = {}
    for t in list(remotas) + list(locais):
        cur = by_id.get(t["id"])
        if cur is None or str(t.get("atualizado_em", "")) >= str(cur.get("atualizado_em", "")):
            by_id[t["id"]] = t
    return sorted(by_id.values(), key=lambda t: t.get("criada_em", ""))


# ---------------------------------------------------------------- sincronização

class Sync:
    """Sincroniza tarefas.json com um repositório PRIVADO no GitHub.
    O servidor do Bob lê de lá e dispara os lembretes no Telegram."""

    def __init__(self):
        self.cfg = None
        try:
            if SYNC_PATH.exists():
                self.cfg = json.loads(SYNC_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"sync: erro lendo sync.json: {e}")

    @property
    def ativo(self):
        return bool(self.cfg and self.cfg.get("github_token") and self.cfg.get("repo"))

    def _url(self):
        return f"https://api.github.com/repos/{self.cfg['repo']}/contents/tarefas.json"

    def _headers(self):
        return {"Authorization": f"token {self.cfg['github_token']}",
                "Accept": "application/vnd.github+json"}

    def pull(self):
        """Retorna (tarefas_remotas, sha) ou ([], None)."""
        if not self.ativo:
            return [], None
        try:
            r = requests.get(self._url(), headers=self._headers(), timeout=20)
            if r.status_code == 404:
                return [], None
            r.raise_for_status()
            d = r.json()
            data = json.loads(b64decode(d["content"]).decode("utf-8"))
            return data.get("tarefas", []), d.get("sha")
        except Exception as e:
            log(f"sync pull falhou: {e}")
            return [], None

    def push(self, tarefas):
        if not self.ativo:
            return False
        try:
            remotas, sha = self.pull()
            final = merge_tasks(tarefas, remotas)
            body = json.dumps({"tarefas": final,
                               "atualizado_em": datetime.now().isoformat(timespec="seconds")},
                              ensure_ascii=False, indent=2)
            payload = {"message": f"tarefas {datetime.now():%d/%m %H:%M}",
                       "content": b64encode(body.encode("utf-8")).decode("ascii")}
            if sha:
                payload["sha"] = sha
            r = requests.put(self._url(), headers=self._headers(),
                             json=payload, timeout=20)
            r.raise_for_status()
            return True
        except Exception as e:
            log(f"sync push falhou: {e}")
            return False


# ---------------------------------------------------------------- áudio

class Recorder:
    def __init__(self):
        self.stream = None
        self.chunks = []
        self.recording = False

    def start(self):
        import sounddevice as sd
        self.chunks = []
        self.recording = True
        self.stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                                     callback=self._cb)
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        if self.recording:
            self.chunks.append(indata.copy())

    def stop(self):
        import numpy as np
        self.recording = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        if not self.chunks:
            return None
        return np.concatenate(self.chunks)


def float_to_wav_bytes(samples, samplerate=16000):
    import numpy as np
    arr = np.asarray(samples, dtype="float32").flatten()
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- pipeline IA

class Pipeline:
    def __init__(self, cfg):
        self.cfg = cfg

    def _p(self):
        return PROVIDERS[self.cfg["provider"]]

    def transcribe(self, wav_bytes):
        p = self._p()
        r = requests.post(
            p["stt_url"],
            headers={"Authorization": f"Bearer {self.cfg['api_key']}"},
            files={"file": ("audio.wav", wav_bytes)},
            data={"model": p["stt_model"], "language": "pt",
                  "response_format": "text", "temperature": 0},
            timeout=120,
        )
        r.raise_for_status()
        return r.text.strip()

    def parse_tasks(self, texto):
        p = self._p()
        r = requests.post(
            p["llm_url"],
            headers={"Authorization": f"Bearer {self.cfg['api_key']}",
                     "Content-Type": "application/json"},
            json={
                "model": p["llm_model"],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": PARSE_PROMPT},
                    {"role": "user",
                     "content": f"agora: {agora_str()}\n<texto>\n{texto}\n</texto>"},
                ],
            },
            timeout=90,
        )
        r.raise_for_status()
        out = r.json()["choices"][0]["message"]["content"]
        try:
            data = json.loads(out)
        except Exception:
            ini, fim = out.find("{"), out.rfind("}")
            data = json.loads(out[ini:fim + 1]) if ini >= 0 and fim > ini else {"tarefas": []}
        tarefas = []
        for t in data.get("tarefas", []):
            titulo = str(t.get("titulo", "")).strip()
            if not titulo:
                continue
            prazo = t.get("prazo") or ""
            if prazo and parse_prazo(prazo) is None:
                prazo = ""
            prio = t.get("prioridade", "normal")
            if prio not in ("alta", "normal", "baixa"):
                prio = "normal"
            tarefas.append({"titulo": titulo, "prazo": prazo, "prioridade": prio})
        return tarefas


# ---------------------------------------------------------------- update

UPDATE_FILES = ["main.py", "requirements.txt", "LEIA-ME.md", "iniciar.bat", "instalar.bat"]


def check_remote_version(timeout=10):
    try:
        r = requests.get(f"{UPDATE_RAW}/version.json", timeout=timeout)
        r.raise_for_status()
        d = r.json()
        return d.get("version"), d.get("changelog", "")
    except Exception as e:
        log(f"update check falhou: {e}")
        return None, None


def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


def download_update(app_dir):
    updated = []
    for fname in UPDATE_FILES:
        try:
            r = requests.get(f"{UPDATE_RAW}/{fname}", timeout=30)
            if r.status_code != 200:
                continue
            dest = app_dir / fname
            tmp = dest.with_suffix(dest.suffix + ".new")
            tmp.write_bytes(r.content)
            tmp.replace(dest)
            updated.append(fname)
        except Exception as e:
            log(f"update: falha baixando {fname}: {e}")
    return updated


def restart_app(icon=None):
    script = Path(__file__).resolve()
    exe = sys.executable
    pyw = Path(exe).with_name("pythonw.exe")
    if pyw.exists():
        exe = str(pyw)
    try:
        if os.name == "nt":
            boot = ("import time,subprocess; time.sleep(3); "
                    f"subprocess.Popen([{exe!r}, {str(script)!r}], "
                    f"cwd={str(script.parent)!r})")
            flags = 0x00000008 | 0x00000200
            subprocess.Popen([exe, "-c", boot], creationflags=flags, close_fds=True)
            log("restart: bootstrap lançado")
        else:
            subprocess.Popen([exe, str(script)], cwd=str(script.parent))
    except Exception as e:
        log(f"restart falhou: {e}")
    try:
        if icon is not None:
            icon.stop()
    except Exception:
        pass
    os._exit(0)


# ---------------------------------------------------------------- app

class TarefaApp:
    def __init__(self):
        self.cfg = load_config()
        self.sync = Sync()
        self.tarefas = load_tasks()
        self.busy = False
        self._recovering = False
        self._last_key_evt = 0.0  # pulso da escuta global (prova de vida)
        self.gravando = False
        self.recorder = Recorder()
        self.icon = None
        self._lock = threading.Lock()

    # ---------- captura por voz (toggle)
    def on_hotkey(self):
        if self.busy:
            return
        if self.gravando:
            threading.Thread(target=self._stop_and_process, daemon=True).start()
        else:
            try:
                self.recorder.start()
                self.gravando = True
                beep(880, 90)
                self.notify("Gravando... fale as tarefas e pressione o atalho de novo.")
            except Exception as e:
                log(f"mic: {e}")
                self.notify("Não consegui acessar o microfone.")

    def _stop_and_process(self):
        self.gravando = False
        self.busy = True
        try:
            beep(660, 90)
            samples = self.recorder.stop()
            if samples is None or len(samples) < 8000:
                self.notify("Áudio muito curto.")
                return
            if not self.cfg.get("api_key"):
                self.notify("Configure a chave de API primeiro.")
                return
            pipe = Pipeline(self.cfg)
            try:
                texto = pipe.transcribe(float_to_wav_bytes(samples))
            except Exception as e:
                body = getattr(getattr(e, "response", None), "text", "")
                log(f"transcrição: {e} | {body[:300]}")
                self.notify("Erro na transcrição. Veja o app.log.")
                return
            if not texto:
                self.notify("Não entendi nenhuma fala.")
                return
            self._parse_and_confirm(texto)
        finally:
            self.busy = False

    def _parse_and_confirm(self, texto):
        pipe = Pipeline(self.cfg)
        try:
            sugestoes = pipe.parse_tasks(texto)
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", "")
            log(f"parse: {e} | {body[:300]}")
            self.notify("Erro ao interpretar as tarefas. Veja o app.log.")
            return
        if not sugestoes:
            self.notify("Nenhuma tarefa identificada no que você disse.")
            return
        self._confirm_window(texto, sugestoes)

    # ---------- janela de confirmação (nada entra sem o seu OK)
    def _confirm_window(self, texto, sugestoes):
        def run():
            import tkinter as tk
            from tkinter import ttk

            win = tk.Tk()
            win.title(f"{APP_NAME} — Confirmar tarefas")
            win.geometry("640x420")
            win.attributes("-topmost", True)

            tk.Label(win, text="Entendi isto — desmarque o que não quiser e ajuste à vontade:",
                     font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
            tk.Label(win, text=f'Você disse: "{texto[:160]}{"..." if len(texto) > 160 else ""}"',
                     fg="#666", wraplength=600, justify="left").pack(anchor="w", padx=12)

            frame = tk.Frame(win)
            frame.pack(fill="both", expand=True, padx=12, pady=8)

            linhas = []
            for i, s in enumerate(sugestoes):
                var_on = tk.BooleanVar(master=win, value=True)
                var_tit = tk.StringVar(master=win, value=s["titulo"])
                var_prazo = tk.StringVar(master=win, value=s["prazo"])
                var_prio = tk.StringVar(master=win, value=s["prioridade"])
                row = tk.Frame(frame)
                row.pack(fill="x", pady=3)
                tk.Checkbutton(row, variable=var_on).pack(side="left")
                tk.Entry(row, textvariable=var_tit, width=42,
                         font=("Segoe UI", 10)).pack(side="left", padx=(0, 6))
                tk.Entry(row, textvariable=var_prazo, width=16).pack(side="left", padx=(0, 6))
                ttk.Combobox(row, textvariable=var_prio, width=7, state="readonly",
                             values=["alta", "normal", "baixa"]).pack(side="left")
                linhas.append((var_on, var_tit, var_prazo, var_prio))

            tk.Label(win, text="Prazo: AAAA-MM-DD ou AAAA-MM-DD HH:MM (vazio = sem prazo)",
                     fg="#666").pack(anchor="w", padx=12)

            def salvar():
                novas = []
                for von, vtit, vprazo, vprio in linhas:
                    if not von.get() or not vtit.get().strip():
                        continue
                    prazo = vprazo.get().strip()
                    if prazo and parse_prazo(prazo) is None:
                        prazo = ""
                    novas.append(nova_tarefa(vtit.get(), prazo, vprio.get()))
                win.destroy()
                if novas:
                    self.add_tasks(novas)

            btns = tk.Frame(win)
            btns.pack(pady=10)
            tk.Button(btns, text=f"✅ Adicionar tarefas", width=18,
                      command=salvar).pack(side="left", padx=4)
            tk.Button(btns, text="Cancelar", width=12,
                      command=win.destroy).pack(side="left", padx=4)
            win.mainloop()

        threading.Thread(target=run, daemon=True).start()

    # ---------- operações
    def add_tasks(self, novas):
        with self._lock:
            self.tarefas.extend(novas)
            save_tasks(self.tarefas)
        beep(990, 90)
        self.notify(f"{len(novas)} tarefa(s) adicionada(s)!")
        self._sync_async()

    def update_task(self, task_id, **changes):
        with self._lock:
            for t in self.tarefas:
                if t["id"] == task_id:
                    t.update(changes)
                    t["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
                    break
            save_tasks(self.tarefas)
        self._sync_async()

    def _sync_async(self):
        def go():
            if self.sync.ativo:
                ok = self.sync.push(list(self.tarefas))
                if ok:
                    remotas, _ = self.sync.pull()
                    with self._lock:
                        self.tarefas = merge_tasks(self.tarefas, remotas)
                        save_tasks(self.tarefas)
                    log("sync: ok")
        threading.Thread(target=go, daemon=True).start()

    def sync_startup(self):
        if not self.sync.ativo:
            log("sync: inativo (sem sync.json)")
            return
        remotas, _ = self.sync.pull()
        with self._lock:
            self.tarefas = merge_tasks(self.tarefas, remotas)
            save_tasks(self.tarefas)
        log(f"sync inicial: {len(self.tarefas)} tarefas")

    # ---------- lembretes locais (Windows)
    def _reminder_loop(self):
        while True:
            try:
                if self.cfg.get("notificar_windows", True):
                    agora = datetime.now()
                    hora_padrao = self.cfg.get("hora_padrao_dia", "09:00")
                    mudou = False
                    with self._lock:
                        for t in self.tarefas:
                            if t["status"] != "aberta" or t.get("notificada_local"):
                                continue
                            dt = parse_prazo(t.get("prazo"))
                            if dt is None:
                                continue
                            if " " not in str(t["prazo"]).strip():
                                try:
                                    h, m = hora_padrao.split(":")
                                    dt = dt.replace(hour=int(h), minute=int(m))
                                except Exception:
                                    dt = dt.replace(hour=9)
                            if agora >= dt:
                                self.notify(f"⏰ {PRIO_ICON.get(t['prioridade'],'')} "
                                            f"{t['titulo']} ({prazo_humano(t['prazo'])})")
                                beep(1200, 250)
                                t["notificada_local"] = True
                                t["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
                                mudou = True
                        if mudou:
                            save_tasks(self.tarefas)
                    if mudou:
                        self._sync_async()
            except Exception as e:
                log(f"lembretes: {e}")
            time.sleep(30)

    # ---------- janela principal
    def open_main(self):
        threading.Thread(target=self._main_window, daemon=True).start()

    def _main_window(self):
        import tkinter as tk
        from tkinter import ttk

        win = tk.Tk()
        win.title(f"{APP_NAME} — Minhas tarefas")
        win.geometry("760x520")
        win.attributes("-topmost", True)

        top = tk.Frame(win)
        top.pack(fill="x", padx=10, pady=8)
        add_var = tk.StringVar(master=win)
        ent = tk.Entry(top, textvariable=add_var, font=("Segoe UI", 10))
        ent.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ent.insert(0, "")

        def add_digitada(_=None):
            texto = add_var.get().strip()
            if not texto:
                return
            add_var.set("")
            threading.Thread(target=lambda: self._parse_and_confirm(texto),
                             daemon=True).start()

        ent.bind("<Return>", add_digitada)
        tk.Button(top, text="➕ Adicionar", command=add_digitada).pack(side="left")
        tk.Button(top, text="🎙 Falar", command=self.on_hotkey).pack(side="left", padx=(6, 0))

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        abas = {}
        for nome in ("Hoje", "Em Breve", "Todas", "Concluídas"):
            fr = tk.Frame(nb)
            tree = ttk.Treeview(fr, columns=("p", "titulo", "prazo"), show="headings",
                                selectmode="browse")
            tree.heading("p", text="")
            tree.heading("titulo", text="Tarefa")
            tree.heading("prazo", text="Prazo")
            tree.column("p", width=34, anchor="center")
            tree.column("titulo", width=480)
            tree.column("prazo", width=150)
            tree.pack(fill="both", expand=True)
            nb.add(fr, text=nome)
            abas[nome] = tree

        id_map = {}

        def refresh():
            agora = datetime.now()
            hoje = agora.date()
            for nome, tree in abas.items():
                tree.delete(*tree.get_children())
            id_map.clear()
            with self._lock:
                abertas = [t for t in self.tarefas if t["status"] == "aberta"]
                feitas = [t for t in self.tarefas if t["status"] == "concluida"]

            def sort_key(t):
                dt = parse_prazo(t.get("prazo"))
                return (0 if dt else 1, dt or datetime.max,
                        {"alta": 0, "normal": 1, "baixa": 2}.get(t["prioridade"], 1))

            abertas.sort(key=sort_key)
            for t in abertas:
                dt = parse_prazo(t.get("prazo"))
                linhas = [("Todas", t)]
                if dt:
                    if dt.date() <= hoje:
                        linhas.append(("Hoje", t))
                    elif (dt.date() - hoje).days <= 7:
                        linhas.append(("Em Breve", t))
                atraso = dt and dt.date() < hoje
                for nome, tt in linhas:
                    iid = abas[nome].insert("", "end", values=(
                        PRIO_ICON.get(tt["prioridade"], ""),
                        ("⚠️ " if atraso and nome != "Concluídas" else "") + tt["titulo"],
                        prazo_humano(tt["prazo"])))
                    id_map[(nome, iid)] = tt["id"]
            for t in sorted(feitas, key=lambda x: x.get("atualizado_em", ""), reverse=True)[:100]:
                iid = abas["Concluídas"].insert("", "end", values=(
                    "✅", t["titulo"], prazo_humano(t["prazo"])))
                id_map[("Concluídas", iid)] = t["id"]

        def selected_id():
            nome = nb.tab(nb.select(), "text")
            tree = abas[nome]
            sel = tree.selection()
            if not sel:
                return None
            return id_map.get((nome, sel[0]))

        def concluir():
            tid = selected_id()
            if tid:
                self.update_task(tid, status="concluida")
                refresh()

        def adiar(dias=None, horas=None):
            tid = selected_id()
            if not tid:
                return
            with self._lock:
                t = next((x for x in self.tarefas if x["id"] == tid), None)
            if not t:
                return
            dt = parse_prazo(t.get("prazo")) or datetime.now()
            com_hora = " " in str(t.get("prazo", "")).strip()
            if horas:
                dt = (dt if com_hora else datetime.now()) + timedelta(hours=horas)
                novo = f"{dt:%Y-%m-%d %H:%M}"
            else:
                dt = dt + timedelta(days=dias or 1)
                novo = f"{dt:%Y-%m-%d %H:%M}" if com_hora else f"{dt:%Y-%m-%d}"
            self.update_task(tid, prazo=novo, notificada_local=False)
            refresh()

        def excluir():
            tid = selected_id()
            if not tid:
                return
            with self._lock:
                self.tarefas = [t for t in self.tarefas if t["id"] != tid]
                save_tasks(self.tarefas)
            self._sync_async()
            refresh()

        def reabrir():
            tid = selected_id()
            if tid:
                self.update_task(tid, status="aberta", notificada_local=False)
                refresh()

        btns = tk.Frame(win)
        btns.pack(pady=(0, 10))
        tk.Button(btns, text="✅ Concluir", width=12, command=concluir).pack(side="left", padx=3)
        tk.Button(btns, text="⏰ +1 hora", width=10,
                  command=lambda: adiar(horas=1)).pack(side="left", padx=3)
        tk.Button(btns, text="📅 +1 dia", width=10,
                  command=lambda: adiar(dias=1)).pack(side="left", padx=3)
        tk.Button(btns, text="↩ Reabrir", width=10, command=reabrir).pack(side="left", padx=3)
        tk.Button(btns, text="🗑 Excluir", width=10, command=excluir).pack(side="left", padx=3)
        tk.Button(btns, text="🔄 Atualizar", width=11,
                  command=lambda: (self.sync_startup(), refresh())).pack(side="left", padx=3)

        refresh()
        win.mainloop()

    # ---------- configurações
    def open_settings(self):
        threading.Thread(target=self._settings_window, daemon=True).start()

    def _settings_window(self):
        import tkinter as tk
        from tkinter import ttk

        win = tk.Tk()
        win.title(f"{APP_NAME} — Configurações")
        win.geometry("540x340")
        win.attributes("-topmost", True)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Atalho (falar tarefas):").grid(row=0, column=0, sticky="w")
        hot_var = tk.StringVar(master=win, value=self.cfg["hotkey"])
        ttk.Entry(frm, textvariable=hot_var, width=20).grid(row=0, column=1, sticky="w")

        def detect():
            hot_var.set("pressione as teclas...")
            win.update()
            try:
                hot_var.set(keyboard.read_hotkey(suppress=False))
            except Exception:
                hot_var.set(self.cfg["hotkey"])

        ttk.Button(frm, text="Detectar", command=detect).grid(row=0, column=2, padx=6)

        ttk.Label(frm, text="Chave de API (Groq):").grid(row=1, column=0, sticky="w", pady=(10, 0))
        key_var = tk.StringVar(master=win, value=self.cfg["api_key"])
        ttk.Entry(frm, textvariable=key_var, width=38, show="*").grid(
            row=1, column=1, columnspan=2, sticky="w", pady=(10, 0))

        ttk.Label(frm, text="Avisar tarefa sem horário às:").grid(row=2, column=0, sticky="w", pady=(10, 0))
        hora_var = tk.StringVar(master=win, value=self.cfg.get("hora_padrao_dia", "09:00"))
        ttk.Entry(frm, textvariable=hora_var, width=8).grid(row=2, column=1, sticky="w", pady=(10, 0))

        notif_var = tk.BooleanVar(master=win, value=self.cfg.get("notificar_windows", True))
        ttk.Checkbutton(frm, text="Notificações do Windows",
                        variable=notif_var).grid(row=3, column=0, columnspan=2,
                                                 sticky="w", pady=(10, 0))

        sync_txt = "✅ ativa (lembretes no Telegram pelo servidor)" if self.sync.ativo \
            else "❌ inativa (sync.json não encontrado)"
        info = (f"Versão instalada: {APP_VERSION}\n"
                f"Sincronização: {sync_txt}\n"
                "Pode usar a MESMA chave Groq dos outros apps.")
        ttk.Label(frm, text=info, foreground="#666").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(14, 0))

        ttk.Button(frm, text="🔄 Verificar atualizações",
                   command=lambda: self.do_update_check(silent=False)).grid(
            row=5, column=0, columnspan=3, pady=(10, 0))

        def salvar():
            self.cfg["hotkey"] = hot_var.get().strip() or self.cfg["hotkey"]
            nova = key_var.get().strip()
            if nova:
                self.cfg["api_key"] = nova
            self.cfg["hora_padrao_dia"] = hora_var.get().strip() or "09:00"
            self.cfg["notificar_windows"] = notif_var.get()
            save_config(self.cfg)
            self.register_hotkey()
            win.destroy()
            self.notify(f"Salvo! Atalho: {self.cfg['hotkey']}")

        ttk.Button(frm, text="Salvar", command=salvar).grid(row=6, column=0, columnspan=3, pady=14)
        win.mainloop()

    # ---------- hotkey + ronda
    def register_hotkey(self):
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        # sentinela de vida: escuta TODO evento de tecla para marcar o pulso
        try:
            if getattr(self, "_pulse_hook", None) is not None:
                try:
                    keyboard.unhook(self._pulse_hook)
                except Exception:
                    pass
            self._pulse_hook = keyboard.on_press(self._pulse)
        except Exception:
            self._pulse_hook = None
        keyboard.add_hotkey(self.cfg["hotkey"], self.on_hotkey, suppress=False)


    # ---------- recuperação pós bloqueio/suspensão (padrão validado no Ditado IA)
    def _recover(self, motivo):
        if self._recovering:
            return
        self._recovering = True
        try:
            time.sleep(3)
            espera = 0
            while (self.gravando or self.busy) and espera < 120:
                time.sleep(1)
                espera += 1
            log(f"recuperacao: reiniciando o app apos {motivo}")
            restart_app(self.icon)
        except Exception as e:
            log(f"recuperacao falhou ({motivo}): {e}")
            self._recovering = False

    def _session_listener(self):
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            wtsapi = ctypes.windll.wtsapi32

            WM_WTSSESSION_CHANGE = 0x02B1
            WTS_SESSION_UNLOCK = 0x8
            WM_POWERBROADCAST = 0x218
            PBT_APMRESUMESUSPEND = 0x7
            PBT_APMRESUMEAUTOMATIC = 0x12
            NOTIFY_FOR_THIS_SESSION = 0

            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_longlong, wintypes.HWND, ctypes.c_uint,
                wintypes.WPARAM, wintypes.LPARAM)

            def wnd_proc(hwnd, msg, wparam, lparam):
                try:
                    if msg == WM_WTSSESSION_CHANGE and wparam == WTS_SESSION_UNLOCK:
                        log("evento do Windows: sessao desbloqueada")
                        threading.Thread(target=self._recover,
                                         args=("desbloqueio de sessao",),
                                         daemon=True).start()
                    elif msg == WM_POWERBROADCAST and wparam in (
                            PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                        log("evento do Windows: retomada da suspensao")
                        threading.Thread(target=self._recover,
                                         args=("retomada da suspensao",),
                                         daemon=True).start()
                except Exception as e:
                    log(f"session_listener wnd_proc: {e}")
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            proc = WNDPROC(wnd_proc)
            self._session_wndproc = proc

            class WNDCLASS(ctypes.Structure):
                _fields_ = [
                    ("style", ctypes.c_uint),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASS()
            wc.style = 0
            wc.lpfnWndProc = proc
            wc.hInstance = hinst
            wc.lpszClassName = "TarefaIASessionWatcher"
            if not user32.RegisterClassW(ctypes.byref(wc)):
                log(f"session_listener: RegisterClass falhou ({ctypes.GetLastError()})")
                return

            hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, "TarefaIA Watcher", 0,
                0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                log(f"session_listener: CreateWindow falhou ({ctypes.GetLastError()})")
                return

            if not wtsapi.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION):
                log(f"session_listener: WTSRegister falhou ({ctypes.GetLastError()})")
            else:
                log("session_listener: registrado para eventos de sessao do Windows")

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log(f"session_listener: erro fatal: {e}")

    def _watchdog(self):
        intervalo = 15
        ultimo = time.monotonic()
        while True:
            time.sleep(intervalo)
            agora = time.monotonic()
            saltou = (agora - ultimo) > (intervalo + 25)
            ultimo = agora
            if saltou and not self._recovering:
                log("watchdog reserva: salto de relogio detectado (suspensao)")
                threading.Thread(target=self._recover,
                                 args=("suspensao (reserva)",),
                                 daemon=True).start()

    def _pulse(self, _evt=None):
        # chamado em QUALQUER evento de teclado — batimento cardíaco da escuta
        self._last_key_evt = time.monotonic()

    def _rehook_patrol(self):
        """Ronda com prova de vida: religa o atalho a cada 2 min e TESTA a
        escuta injetando tecla fantasma (F24). Religar nao basta se o motor
        de escuta morreu (caso de campo Ditado 04/11/2026) — 2 falhas
        seguidas confirmam gancho morto e o app se reinicia sozinho."""
        falhas = 0
        while True:
            time.sleep(120)
            try:
                if self.gravando or self.busy or self._recovering:
                    continue
                self.register_hotkey()
                t0 = time.monotonic()
                injetou = False
                if os.name == "nt":
                    # injecao direta no Windows (VK_F24=0x87): nao depende do
                    # mapeamento de nomes da lib keyboard, que pode falhar em
                    # silencio p/ f24 (tempestade de reinicios de 04/11/2026)
                    try:
                        import ctypes
                        ctypes.windll.user32.keybd_event(0x87, 0, 0, 0)
                        ctypes.windll.user32.keybd_event(0x87, 0, 2, 0)
                        injetou = True
                    except Exception:
                        pass
                if not injetou:
                    try:
                        keyboard.press_and_release("f24")
                        injetou = True
                    except Exception:
                        pass
                time.sleep(1.0)
                if not injetou:
                    # sem injecao comprovada NAO ha teste valido — jamais
                    # contar como falha (era isso que reiniciava os 4 apps)
                    log("ronda: nao consegui injetar a tecla de teste — verificacao pulada")
                    continue
                if self._last_key_evt >= t0:
                    falhas = 0
                    log("ronda: atalho religado e escuta VERIFICADA viva")
                else:
                    falhas += 1
                    log(f"ronda: escuta NAO respondeu ao teste ({falhas}x)")
                    if falhas >= 2:
                        log("ronda: gancho morto confirmado — reiniciando o app")
                        falhas = 0
                        try:
                            self.notify("Escuta do teclado travou — reiniciando sozinho…")
                        except Exception:
                            pass
                        restart_app(self.icon)
                        return
            except Exception as e:
                log(f"ronda: falha ao religar ({e})")

    # ---------- update
    def do_update_check(self, silent=False):
        remote, changelog = check_remote_version()
        if not remote:
            if not silent:
                self.notify("Não foi possível verificar atualizações.")
            return
        if version_tuple(remote) <= version_tuple(APP_VERSION):
            if not silent:
                self.notify(f"Você já está na versão mais recente ({APP_VERSION}).")
            return

        def ask_and_apply():
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            ok = messagebox.askyesno(
                f"{APP_NAME} — Atualização",
                f"Nova versão disponível: {remote} (atual: {APP_VERSION})\n\n"
                f"Novidades:\n{changelog or '(sem descrição)'}\n\n"
                "Atualizar agora? O app reinicia sozinho.", parent=root)
            root.destroy()
            if not ok:
                return
            app_dir = Path(__file__).resolve().parent
            updated = download_update(app_dir)
            if "main.py" in updated:
                log(f"update: v{APP_VERSION} -> v{remote}")
                self.notify(f"Atualizado para v{remote}! Reiniciando...")
                time.sleep(1.5)
                restart_app(self.icon)
            else:
                self.notify("Falha ao baixar a atualização. Veja o app.log.")

        threading.Thread(target=ask_and_apply, daemon=True).start()

    # ---------- bandeja
    def notify(self, msg):
        try:
            if self.icon:
                self.icon.notify(msg, APP_NAME)
        except Exception:
            pass

    def _make_icon_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([8, 8, 56, 56], radius=10, fill=(30, 30, 40, 255))
        d.line([(18, 32), (28, 42), (46, 22)], fill=(52, 209, 123, 255), width=7)
        return img

    def run_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("📋 Minhas tarefas", lambda: self.open_main(), default=True),
            pystray.MenuItem("🎙 Falar tarefas", lambda: self.on_hotkey()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Configurações", lambda: self.open_settings()),
            pystray.MenuItem("Verificar atualizações",
                             lambda: self.do_update_check(silent=False)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", lambda: self.icon.stop()),
        )
        self.icon = pystray.Icon(APP_NAME, self._make_icon_image(), APP_NAME, menu)
        self.icon.run()

    def _bootstrap_sync_file(self):
        """Primeira execução: copia sync_config.json (da pasta do app, vindo
        no zip) para o APPDATA. Esse arquivo NUNCA vai para o repositório."""
        try:
            if SYNC_PATH.exists():
                return
            origem = Path(__file__).resolve().parent / "sync_config.json"
            if origem.exists():
                APP_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy(str(origem), str(SYNC_PATH))
                log("sync: sync_config.json instalado no APPDATA")
                self.sync = Sync()
        except Exception as e:
            log(f"sync bootstrap: {e}")

    def run(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._bootstrap_sync_file()
        if not self.cfg.get("api_key"):
            self.open_settings()
        self.register_hotkey()
        threading.Timer(8.0, lambda: self.do_update_check(silent=True)).start()
        threading.Thread(target=self._reminder_loop, daemon=True).start()
        threading.Thread(target=self._rehook_patrol, daemon=True).start()
        threading.Thread(target=self._session_listener, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self.sync_startup, daemon=True).start()
        self.run_tray()


if __name__ == "__main__":
    try:
        TarefaApp().run()
    except Exception as e:
        log(f"fatal: {e}\n{traceback.format_exc()}")
        raise
